"""Reviewable deletion plans and locked, rebuild-before-delete maintenance."""
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .bank_build.service import _build_lock, load_sources
from .bank_build.source_cache import cache_directory
from .bank_storage import active_bank_dir, atomic_json


LEGACY_FILES = frozenset({"questions.json", "question_concepts.json", "sources.json", "repos", "extracted",
    "knowledge_tag_report.json", "concept_profile_report.json", "title_cleanup_report.json"})


@dataclass(frozen=True)
class CleanupTarget:
    relative: str
    label: str
    used: bool
    identity: tuple


@dataclass(frozen=True)
class CleanupPlan:
    kind: str
    repositories: tuple
    targets: tuple
    remaining: tuple
    pointer_digest: str
    estimated_bytes: int
    last_source: bool
    requires_switch: bool


@dataclass
class CleanupResult:
    removed: list
    failures: list
    switched: bool = False
    empty: bool = False

    @property
    def message(self):
        note = "已切换为空题库；需要重新下载并构建来源后才能练习。" if self.empty else "题库清理已处理。"
        return note + "学习数据库和历史答题快照保留。" + ("\n未完成：" + "\n".join(self.failures) if self.failures else "")


def _link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


class BankCleanupService:
    def __init__(self, service):
        self.service = service
        self.root = Path(service.project_root).resolve()
        self.data = self.root / "data"

    def _roots(self):
        # Validate all ancestors before the lock creates even its owner file.
        for path in (self.data, self.data / "banks", self.data / "source-cache", self.data / ".bank-builds"):
            path.resolve().relative_to(self.root)
            if path.exists() and _link(path):
                raise ValueError(f"管理目录为链接，拒绝清理：{path.name}")
        self._learning_paths = {(self.data / "trainer.db").resolve()}
        configuration = self.root / "config.json"
        if configuration.exists():
            if _link(configuration):
                raise ValueError("配置文件为链接，无法安全确认学习数据库位置。")
            settings = json.loads(configuration.read_text(encoding="utf-8"))
            db_path = settings.get("db_path") if isinstance(settings, dict) else None
            if isinstance(db_path, str) and db_path:
                path = Path(db_path)
                self._learning_paths.add((path if path.is_absolute() else self.root / path).resolve())

    def _target(self, relative):
        relative = Path(relative)
        parts = relative.parts
        allowed = ((len(parts) == 2 and parts[0] in {"banks", "source-cache"} and re.fullmatch(r"[0-9a-f]{32}", parts[1]))
                   or (len(parts) == 1 and parts[0] in LEGACY_FILES))
        if relative.is_absolute() or not allowed:
            raise ValueError(f"不是应用管理的题库清理路径：{relative}")
        path = self.data / relative
        path.resolve().relative_to(self.data.resolve())
        if path.exists() and _link(path):
            raise ValueError(f"删除对象为链接，拒绝清理：{relative}")
        return path

    def _files(self, path):
        if not path.exists():
            return
        if _link(path):
            raise ValueError(f"对象含链接，拒绝递归删除：{path.name}")
        if path.resolve() in self._learning_paths or path.name == ".git" or re.search(r"\.(?:db|sqlite3?)(?:-(?:wal|shm|journal))?$", path.name, re.I):
            raise ValueError(f"对象含项目仓库或学习数据库，保留：{path.name}")
        if path.is_dir():
            for child in sorted(path.iterdir()):
                yield from self._files(child)
        elif stat.S_ISREG(path.stat().st_mode):
            yield path, path.stat()
        else:
            raise ValueError(f"对象含特殊文件，保留：{path.name}")

    def _pointer(self):
        pointer = self.data / "active-bank.json"
        if pointer.exists() and _link(pointer):
            raise ValueError("当前题库指针为链接，拒绝清理。")
        raw = pointer.read_bytes() if pointer.exists() else b""
        return raw, hashlib.sha256(raw).hexdigest()

    def _retire(self, path, retired):
        # Windows scanners can briefly open a ZIP without delete sharing. The
        # serial worker keeps the application lease; never block the Tk thread
        # or force-delete an occupied file. Persistent errors are still reported.
        deadline = time.monotonic() + 6
        delay = .1
        while True:
            self._target(path.relative_to(self.data))
            try:
                os.replace(path, retired)
                return
            except OSError as error:
                remaining = deadline - time.monotonic()
                if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 1)

    def _provenance(self):
        current = active_bank_dir(self.root)
        self._roots()
        if current != self.data:
            self._target(current.relative_to(self.data))
        try:
            path = current / "sources.json"
            if _link(path) or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("来源记录不安全")
            entries = json.loads(path.read_text(encoding="utf-8"))["sources"]
            if not isinstance(entries, list):
                raise ValueError("来源记录无效")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("repository"), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", entry.get("commit", "")):
                    raise ValueError("来源或完整 commit 未记录")
            return current, entries
        except (OSError, ValueError, TypeError, KeyError) as error:
            if (current / "questions.json").exists():
                raise ValueError("当前题库缺少可核对的来源和 commit；请先完整重建，再移除来源。") from error
            return current, []

    def _plan(self, kind, repositories=(), protected=()):
        self._roots()
        raw, digest = self._pointer()
        current, entries = self._provenance() if kind == "sources" else (active_bank_dir(self.root), [])
        known = load_sources(self.service.manifest)
        selected = {repo.casefold() for repo in repositories}
        targets = []
        remaining = []
        used = any(e["repository"].casefold() in selected for e in entries)
        active_pairs = {(entry["repository"].casefold(), entry["commit"].lower()) for entry in entries}
        runtime_pairs = set()
        if kind == "sources":
            if not selected or selected - {repo.casefold() for repo in known}:
                raise ValueError("请选择清单中的来源。")
            for directory in set(Path(path).resolve() for path in protected):
                if directory == current.resolve():
                    continue
                directory.relative_to(self.data.resolve())
                record = directory / "sources.json"
                try:
                    if _link(record) or record.stat().st_size > 16 * 1024 * 1024:
                        raise ValueError("来源记录不安全")
                    runtime_entries = json.loads(record.read_text(encoding="utf-8"))["sources"]
                    if not isinstance(runtime_entries, list) or any(not re.fullmatch(r"[0-9a-fA-F]{40}", entry.get("commit", "")) for entry in runtime_entries):
                        raise ValueError("运行中来源未记录完整 commit")
                    runtime_pairs.update((entry["repository"].casefold(), entry["commit"].lower()) for entry in runtime_entries)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    raise ValueError("运行中引擎的来源记录不完整；请先应用新题库后再清理。") from error
            for entry in entries:
                if entry["repository"].casefold() not in selected:
                    if entry["repository"] not in known:
                        raise ValueError(f"保留来源 {entry['repository']} 已不在清单中，无法安全重建。")
                    remaining.append((entry["repository"], entry["commit"].lower()))
            cache_names = {cache_directory(self.data / "source-cache", source).name: (repo.casefold(), source.commit)
                           for repo, source in known.items() if repo.casefold() in selected}
            cache_root = self.data / "source-cache"
            if cache_root.exists():
                for path in cache_root.iterdir():
                    if not re.fullmatch(r"[0-9a-f]{32}", path.name):
                        continue
                    self._target(path.relative_to(self.data))
                    try:
                        record = path / "complete.json"
                        if _link(record) or record.stat().st_size > 16 * 1024 * 1024:
                            raise ValueError("来源记录不是可读取的安全文件")
                        metadata = json.loads(record.read_text(encoding="utf-8"))["source"]
                        repo, commit = metadata["repository"], metadata["commit"]
                        from dataclasses import replace
                        source = next((s for r, s in known.items() if r.casefold() == str(repo).casefold()), None)
                        if source and re.fullmatch(r"[0-9a-f]{40}", commit) and cache_directory(cache_root, replace(source, commit=commit)) == path:
                            if source.repository.casefold() in selected:
                                cache_names[path.name] = (source.repository.casefold(), commit)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                for name in sorted(cache_names):
                    path = cache_root / name
                    if path.exists():
                        repo, commit = cache_names[name]
                        targets.append((path, f"来源缓存 {repo} @ {commit}", (repo, commit) in active_pairs | runtime_pairs))
            if used:
                if current == self.data:
                    targets.extend((self.data / name, "旧式题库 " + name, True) for name in sorted(LEGACY_FILES) if (self.data / name).exists())
                else:
                    targets.append((current, "当前版本 " + current.name, True))
        elif kind == "history":
            protected = {Path(path).resolve() for path in protected}
            protected.add(current.resolve())
            banks = self.data / "banks"
            for path in sorted(banks.iterdir() if banks.exists() else []):
                if path.resolve() not in protected and re.fullmatch(r"[0-9a-f]{32}", path.name):
                    targets.append((path, "历史版本 " + path.name, False))
        else:
            raise ValueError("未知清理操作")
        result, identities = [], {}
        for path, label, in_use in targets:
            path = self._target(path.relative_to(self.data))
            info = path.stat()
            result.append(CleanupTarget(str(path.relative_to(self.data)), label, in_use, (info.st_dev, info.st_ino)))
            for file, info in self._files(path):
                identity = (info.st_dev, info.st_ino) if info.st_ino else str(file)
                count, size, links = identities.get(identity, (0, info.st_size, info.st_nlink))
                identities[identity] = count + 1, size, max(1, links)
        released = sum(size for count, size, links in identities.values() if count >= links)
        return CleanupPlan(kind, tuple(repositories), tuple(result), tuple(remaining), digest, released,
                           used and not remaining, used)

    def plan(self, kind, repositories=(), *, protected=()):
        self._roots()
        with _build_lock(self.data / ".bank-builds"):
            return self._plan(kind, repositories, protected)

    def _empty_bank(self, previous):
        generation = uuid.uuid4().hex
        directory = self.data / "banks" / generation
        directory.mkdir(parents=True)
        atomic_json(directory / "questions.json", [])
        atomic_json(directory / "question_concepts.json", {"profiles": {}})
        atomic_json(directory / "sources.json", {"schema_version": 1, "sources": []})
        atomic_json(directory / "empty-bank.json", {"schema_version": 1, "reason": "sources_removed"})
        if previous:
            atomic_json(directory / "previous-pointer.json", json.loads(previous))
        if (json.loads((directory / "questions.json").read_text()) != []
                or json.loads((directory / "question_concepts.json").read_text()).get("profiles") != {}
                or json.loads((directory / "sources.json").read_text()).get("sources") != []
                or json.loads((directory / "empty-bank.json").read_text()).get("reason") != "sources_removed"):
            raise ValueError("空题库落盘校验失败")
        atomic_json(self.data / "active-bank.json", {"schema_version": 1, "generation": generation})

    def execute(self, plan, *, engine=None, protected=()):
        """Called on the serial engine worker, never Tk. Revalidate approval first."""
        self._roots()
        state_lock = getattr(engine, "_STATE_LOCK", None)
        with state_lock if state_lock is not None else nullcontext():
            with _build_lock(self.data / ".bank-builds") as lease:
                protected = list(protected)
                loaded = getattr(getattr(engine, "kb", None), "concepts_path", None)
                if loaded:
                    protected.append(Path(loaded).parent)
                fresh = self._plan(plan.kind, plan.repositories, protected)
                if fresh != plan:
                    raise ValueError("题库、删除对象或使用状态已变化，请刷新清理清单并重新确认。")
                if plan.requires_switch and engine is None:
                    raise ValueError("正在使用的来源必须通过主窗口串行工作线程切换引擎后清理。")
                old_raw, _ = self._pointer()
                switched = False
                if plan.requires_switch:
                    try:
                        if plan.last_source:
                            self._empty_bank(old_raw)
                        else:
                            self.service.build([repo for repo, _ in plan.remaining], versions=dict(plan.remaining), _lease=lease)
                        if engine is not None:
                            engine.reload_bank()
                        switched = True
                    except Exception as original_error:
                        pointer = self.data / "active-bank.json"
                        if old_raw:
                            atomic_json(pointer, json.loads(old_raw))
                        else:
                            pointer.unlink(missing_ok=True)
                        if engine is not None:
                            try:
                                engine.reload_bank()
                            except Exception as rollback_error:
                                raise RuntimeError(f"切换失败：{original_error}。旧指针已恢复，文件未清理；引擎重载仍失败：{rollback_error}，请重启后重试。") from original_error
                        raise
                elif plan.kind == "sources" and loaded and Path(loaded).parent.resolve() != active_bank_dir(self.root).resolve():
                    # A previously built version may still await the serial
                    # engine boundary. Apply it before removing a source cache.
                    engine.reload_bank()
                    switched = True
                empty = bool(getattr(getattr(engine, "kb", None), "explicit_empty", False))
                result = CleanupResult([], [], switched, empty or (plan.last_source and switched))
                active = active_bank_dir(self.root).resolve()
                loaded = getattr(getattr(engine, "kb", None), "concepts_path", None)
                loaded = Path(loaded).parent.resolve() if loaded else None
                for target in plan.targets:
                    try:
                        path = self._target(target.relative)
                        if not path.exists():
                            continue
                        if path.resolve() in {active, loaded}:
                            raise ValueError("对象仍被当前题库或运行中引擎使用，已保留")
                        list(self._files(path))  # Recheck links/protected files before any move.
                        retired = self.data / ".bank-builds" / uuid.uuid4().hex
                        retired.resolve().relative_to((self.data / ".bank-builds").resolve())
                        self._retire(path, retired)
                        if retired.is_dir():
                            shutil.rmtree(retired)
                        else:
                            retired.unlink()
                        result.removed.append(target.relative)
                    except (OSError, ValueError) as error:
                        result.failures.append(f"{target.label}：{error}。请检查权限、文件占用后重试。")
                return result
