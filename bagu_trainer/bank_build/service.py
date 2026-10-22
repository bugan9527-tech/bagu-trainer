"""Desktop-callable, transactional first-use bank initialization."""
import json
import math
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..bank_storage import active_bank_dir, atomic_json
from .control import BuildCancelled, BuildControl, BuildError, BuildTimeout
from .downloads import ArchiveLimits, download_archive, safe_extract


@dataclass(frozen=True)
class BankSource:
    repository: str
    directory: str
    commit: str
    license: str
    evidence_url: str


@dataclass(frozen=True)
class BuildResult:
    generation: str
    directory: Path
    questions: int
    previous_directory: Path


def load_sources(manifest=None):
    manifest = Path(manifest or config.PROJECT_ROOT / "interview-banks" / "sources.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    sources, directories = {}, set()
    for value in data["sources"]:
        repository, directory, commit = (value["repository"], value["directory"], value["audited_commit"])
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", directory)
                or directory in {".", ".."} or directory.endswith(".")
                or not re.fullmatch(r"[0-9a-fA-F]{40}", commit)
                or repository in sources or directory.casefold() in directories):
            raise ValueError("来源清单包含无效或重复的来源/版本")
        directories.add(directory.casefold())
        sources[repository] = BankSource(repository, directory, commit.lower(),
                                         value.get("observed_license", "Unconfirmed"), value.get("evidence_url", ""))
    return sources


@contextmanager
def _build_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from ..desktop_instance import OwnerLock
        owner = OwnerLock(directory)
        if not owner.acquire():
            raise BuildError("prepare", "已有题库构建正在运行，请等待后重试。")
        try:
            yield
        finally:
            owner.close()
    else:
        import fcntl
        with (directory / "owner.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BuildError("prepare", "已有题库构建正在运行，请等待后重试。") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_staging(directory, parent):
    if directory.exists():
        directory.resolve().relative_to(parent.resolve())
        if directory == parent or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            raise ValueError("非法临时目录")
        shutil.rmtree(directory)


class BankBuildService:
    def __init__(self, project_root=None, *, manifest=None, opener=None, limits=None):
        self.project_root = Path(project_root or config.USER_DATA_ROOT).resolve()
        self.manifest = Path(manifest) if manifest else config.PROJECT_ROOT / "interview-banks" / "sources.json"
        self.opener = opener
        self.limits = limits or ArchiveLimits()

    def build(self, repositories, *, versions=None, cancel=None, timeout=900,
              progress=None, download_retries=2, request_timeout=5):
        """Build a fresh generation; failure/cancellation never changes active data.

        Versions defaults to the audited commits. Overrides must be full commit
        IDs. Retries use a new staging directory; no partial build is resumed.
        """
        control = BuildControl(cancel, timeout, progress)
        try:
            result = self._run(repositories, versions, control, download_retries, request_timeout)
            control.emit("done", f"题库构建完成：{result.questions} 道题", result.questions, result.questions)
            return result
        except (BuildCancelled, BuildTimeout) as error:
            control.emit("cancelled" if isinstance(error, BuildCancelled) else "timeout", str(error))
            raise
        except BuildError as error:
            control.emit("failed", str(error))
            raise
        except Exception as error:
            stage = control.stage
            failure = BuildError(stage, f"{stage} 阶段失败（{type(error).__name__}），当前题库保持不变，可重试。")
            control.emit("failed", str(failure))
            raise failure from error

    def _run(self, repositories, versions, control, download_retries, request_timeout):
        if (not isinstance(download_retries, int) or not 0 <= download_retries <= 5
                or not isinstance(request_timeout, (int, float))
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("下载重试次数或请求超时无效")
        control.phase("prepare", "读取所选来源与固定版本")
        known = load_sources(self.manifest)
        selected = list(dict.fromkeys(repositories))
        versions = versions or {}
        if not selected or any(repo not in known for repo in selected) or set(versions) - set(selected):
            raise BuildError("prepare", "请从来源清单中选择至少一个来源。")
        sources = []
        for repository in selected:
            source = known[repository]
            commit = versions.get(repository, source.commit)
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                raise BuildError("prepare", "指定版本必须是完整的 40 位 commit ID。")
            sources.append(replace(source, commit=commit.lower()))
        staging_root = self.project_root / "data" / ".bank-builds"
        generation = uuid.uuid4().hex
        staging = staging_root / generation
        with _build_lock(staging_root):
            try:
                staging.mkdir()
                return self._build(sources, staging, generation, control, download_retries, request_timeout)
            finally:
                _remove_staging(staging, staging_root)

    def _build(self, sources, staging, generation, control, retries, request_timeout):
        from .extraction import extract_sources
        from .importer import build_bank
        from .tagging import tag_questions
        from .profiles import build_profiles
        from ..concepts import validate_profiles

        provenance = []
        path_aliases = {}
        for source in sources:
            archive = staging / (source.directory + ".zip")
            provenance.append(download_archive(source, archive, control, opener=self.opener,
                retries=retries, request_timeout=request_timeout, limits=self.limits))
            control.phase("unpack", f"安全解压 {source.repository}")
            try:
                aliases = safe_extract(archive, staging / "repos" / source.directory,
                                       control, limits=self.limits)
            except (BuildCancelled, BuildTimeout):
                raise
            except BuildError as error:
                raise BuildError(error.stage, f"{source.repository}: {error}") from error
            provenance[-1]["path_aliases"] = aliases
            path_aliases.update({f"{source.directory}/{original}": f"{source.directory}/{stored}"
                                 for original, stored in aliases.items()})
            archive.unlink()
        control.phase("extract", "提取题目并去重")
        export = extract_sources([source.directory for source in sources], staging / "repos",
                                  staging / "extracted", checkpoint=control.check,
                                  path_aliases=path_aliases)
        control.phase("import", "导入题目并提取来源正文", len(export))
        bank, extraction = build_bank(export, staging / "repos", 1500,
                                      checkpoint=control.ticker("import", len(export)), stable_ids=True,
                                      path_aliases=path_aliases)
        if not bank:
            raise BuildError("import", "所选 ZIP 中没有可用题目，请选择包含题目正文的来源后重试。")
        control.phase("tag", "构建知识标签", len(bank))
        tag_report = tag_questions(bank, checkpoint=control.ticker("tag", len(bank)))
        control.phase("concepts", "构建概念档案", len(bank))
        concepts, concept_report = build_profiles(bank, checkpoint=control.ticker("concepts", len(bank)))
        control.phase("validate", "校验完整题库和概念档案")
        ids = [q["id"] for q in bank]
        _, errors = validate_profiles(ids, concepts["profiles"])
        if errors or len(set(ids)) != len(ids):
            raise BuildError("validate", "题库 ID 或概念档案校验失败。")
        for name, value in (("questions.json", bank), ("question_concepts.json", concepts),
                            ("knowledge_tag_report.json", tag_report),
                            ("concept_profile_report.json", concept_report),
                            ("title_cleanup_report.json", extraction),
                            ("sources.json", {"schema_version": 1, "sources": provenance,
                                "built_at": datetime.now(timezone.utc).isoformat()})):
            control.check()
            atomic_json(staging / name, value)
        # Read back the files before publishing, not just their in-memory values.
        written = json.loads((staging / "questions.json").read_text(encoding="utf-8"))
        written_profiles = json.loads((staging / "question_concepts.json").read_text(encoding="utf-8"))
        if written != bank or written_profiles != concepts:
            raise BuildError("validate", "题库落盘校验失败。")
        try:
            previous = active_bank_dir(self.project_root)
        except (OSError, ValueError):
            # A corrupt pointer can be repaired by a new build; the original file
            # is retained for diagnosis before any replacement.
            previous = self.project_root / "data"
        pointer = self.project_root / "data" / "active-bank.json"
        if pointer.exists():
            shutil.copyfile(pointer, staging / "previous-pointer.json")
        control.phase("activate", "切换到完整的新题库，保留旧数据")
        final = self.project_root / "data" / "banks" / generation
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final)
        control.check()
        atomic_json(pointer, {"schema_version": 1, "generation": generation})
        return BuildResult(generation, final, len(bank), previous)
