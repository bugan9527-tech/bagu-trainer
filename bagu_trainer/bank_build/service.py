"""Desktop-callable, transactional first-use bank initialization."""
import json
import math
import os
import re
import shutil
import uuid
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..bank_storage import active_bank_dir, atomic_json
from .control import BuildCancelled, BuildControl, BuildError, BuildTimeout, SourceBatchError, SourceStatus
from .downloads import ArchiveLimitError, ArchiveLimits, download_archive, safe_extract
from .source_cache import load_archive, publish_archive


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
    sources: tuple = ()


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
    lease = _BuildLease(Path(directory).resolve(), threading.get_ident())
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from ..desktop_instance import OwnerLock
        owner = OwnerLock(directory)
        if not owner.acquire():
            raise BuildError("prepare", "已有题库构建正在运行，请等待后重试。")
        try:
            lease.active = True
            yield lease
        finally:
            lease.active = False
            owner.close()
    else:
        import fcntl
        with (directory / "owner.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BuildError("prepare", "已有题库构建正在运行，请等待后重试。") from error
            try:
                lease.active = True
                yield lease
            finally:
                lease.active = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class _BuildLease:
    directory: Path
    thread: int
    active: bool = False

    def valid_for(self, directory):
        return self.active and self.thread == threading.get_ident() and self.directory == directory.resolve()


def _remove_staging(directory, parent):
    if directory.exists():
        directory.resolve().relative_to(parent.resolve())
        if directory == parent or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            raise ValueError("非法临时目录")
        shutil.rmtree(directory)


def _error_reason(error):
    if isinstance(error, ArchiveLimitError):
        return f"{error} 请检查来源归档大小及资源限制，调整限制或选择较小来源后重试。"
    if isinstance(error, BuildError):
        reason = str(error)
        if error.stage == "unpack":
            reason += " 请检查固定 commit 的归档格式与安全性；损坏归档可重新下载，含不安全路径的来源需修复后重试。"
        return reason
    if isinstance(error, PermissionError):
        return f"无法读写来源文件：{error}。请检查数据目录权限或文件占用后重试。"
    if isinstance(error, OSError):
        return f"本地文件操作失败：{error}。请检查磁盘剩余空间、路径和读写权限后重试。"
    return f"来源处理失败：{str(error) or type(error).__name__}。请检查来源版本与文件格式后重试。"


class BankBuildService:
    def __init__(self, project_root=None, *, manifest=None, opener=None, limits=None):
        self.project_root = Path(project_root or config.USER_DATA_ROOT).resolve()
        self.manifest = Path(manifest) if manifest else config.PROJECT_ROOT / "interview-banks" / "sources.json"
        self.opener = opener
        self.limits = limits or ArchiveLimits()

    def build(self, repositories, *, versions=None, cancel=None, timeout=None,
              progress=None, download_retries=2, request_timeout=5, force_download=False,
              retry_failed=None, _lease=None):
        """Build a fresh generation; failure/cancellation never changes active data.

        Versions defaults to the audited commits. Overrides must be full commit
        IDs. Rebuilds reuse verified archives; force_download requests a refresh.
        Retries use a new staging directory; no partial build is resumed.
        """
        repositories = list(dict.fromkeys(repositories))
        control = BuildControl(cancel, 900 * max(1, len(repositories)) if timeout is None else timeout, progress)
        try:
            result = self._run(repositories, versions, control, download_retries, request_timeout, force_download, retry_failed, _lease)
            control.emit("done", f"题库构建完成：{result.questions} 道题", result.questions, result.questions)
            return result
        except (BuildCancelled, BuildTimeout) as error:
            control.interrupted_sources(error)
            control.emit("cancelled" if isinstance(error, BuildCancelled) else "timeout", str(error))
            raise
        except BuildError as error:
            error.sources = tuple(control.sources.values())
            control.emit("failed", str(error))
            raise
        except Exception as error:
            stage = control.stage
            failure = BuildError(stage, f"{stage} 阶段失败：{_error_reason(error)} 当前题库保持不变。")
            failure.sources = tuple(control.sources.values())
            control.emit("failed", str(failure))
            raise failure from error

    def _run(self, repositories, versions, control, download_retries, request_timeout, force_download, retry_failed, lease):
        if (not isinstance(download_retries, int) or not 0 <= download_retries <= 5
                or not isinstance(request_timeout, (int, float))
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("下载重试次数或请求超时无效")
        control.emit("prepare", "读取所选来源与固定版本")
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
        if retry_failed is not None:
            retry_failed = set(retry_failed)
            if not retry_failed or retry_failed - set(selected):
                raise BuildError("prepare", "仅重试失败来源必须指定本次选择中的失败项。")
        control.sources = {s.repository: SourceStatus(s.repository, s.commit) for s in sources}
        for source in sources:
            control.source_state(source.repository, "waiting")
        control.current_source = None
        control.check()
        staging_root = self.project_root / "data" / ".bank-builds"
        generation = uuid.uuid4().hex
        control.generation = generation
        control.emit("prepare", "准备独立构建目录")
        staging = staging_root / generation
        if lease is not None and (not isinstance(lease, _BuildLease) or not lease.valid_for(staging_root)):
            raise BuildError("prepare", "清理任务的构建锁已失效，请重新操作。")
        with (nullcontext(lease) if lease is not None else _build_lock(staging_root)):
            try:
                staging.mkdir()
                return self._build(sources, staging, generation, control, download_retries, request_timeout, force_download, retry_failed)
            finally:
                _remove_staging(staging, staging_root)

    def _prepare_source(self, source, staging, control, retries, request_timeout, force_download, allow_download):
        destination = staging / "repos" / source.directory
        cache_root = self.project_root / "data" / "source-cache"
        cached = None if force_download else load_archive(cache_root, source, control, self.limits)
        if cached:
            archive, metadata = cached
            control.phase("unpack", f"复用本地缓存，安全解压 {source.repository}")
            try:
                aliases = safe_extract(archive, destination, control, limits=self.limits)
            except (BuildCancelled, BuildTimeout, ArchiveLimitError):
                raise
            except BuildError:
                shutil.rmtree(destination)
                cached = None
        if not cached:
            if not allow_download:
                raise BuildError("cache", "已完成来源的缓存缺失或损坏；仅重试失败项不会重新下载该来源。请使用“重新构建”修复缓存。")
            archive = staging / (source.directory + ".zip")
            metadata = download_archive(source, archive, control, opener=self.opener,
                retries=retries, request_timeout=request_timeout, limits=self.limits)
            control.phase("unpack", f"安全解压 {source.repository}")
            aliases = safe_extract(archive, destination, control, limits=self.limits)
            control.phase("cache", f"保存已校验来源 {source.repository}")
            publish_archive(cache_root, source, archive, metadata, control)
        return {**metadata, "repository": source.repository,
                "observed_license": source.license, "evidence_url": source.evidence_url, "path_aliases": aliases}

    def _build(self, sources, staging, generation, control, retries, request_timeout, force_download, retry_failed):
        from .extraction import extract_sources
        from .importer import build_bank
        from .tagging import tag_questions
        from .profiles import build_profiles
        from ..concepts import validate_profiles

        provenance = []
        path_aliases = {}
        for source in sources:
            control.check()
            control.source_state(source.repository, "downloading")
            try:
                control.check()
                allowed = retry_failed is None or source.repository in retry_failed
                metadata = self._prepare_source(source, staging, control, retries, request_timeout,
                    force_download and allowed, allowed)
                control.source_state(source.repository, "completed", "来源已完整校验并保存在本地缓存。")
                control.check()
            except (BuildCancelled, BuildTimeout):
                raise
            except Exception as error:
                if isinstance(error, BuildError):
                    control.emit(error.stage, str(error))
                control.source_state(source.repository, "failed", _error_reason(error))
                continue
            provenance.append(metadata)
            path_aliases.update({f"{source.directory}/{original}": f"{source.directory}/{stored}"
                                 for original, stored in metadata["path_aliases"].items()})
        control.current_source = None
        control.check()
        if any(s.state == "failed" for s in control.sources.values()):
            raise SourceBatchError(tuple(control.sources.values()))
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
        return BuildResult(generation, final, len(bank), previous, tuple(control.sources.values()))
