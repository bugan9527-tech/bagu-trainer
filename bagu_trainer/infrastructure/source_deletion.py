"""Recoverable, offline repository deletion, including immutable mixed versions.

The journal records no question text or credential. Before cleaning, failures
roll back; after the durable cleaning marker, recovery only finishes deletion.
All entry points run under the build lease and the serial engine boundary.
"""
import hashlib
import json
import os
import shutil
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure.bank_storage import active_bank_dir
from bagu_trainer.infrastructure.bank_storage import atomic_json
from bagu_trainer.infrastructure.bank_build.service import _build_lock
from bagu_trainer.infrastructure.bank_build.service import load_sources
from bagu_trainer.infrastructure.bank_build.source_cache import cache_directory
from bagu_trainer.infrastructure.provenance import bank_index
from bagu_trainer.infrastructure.provenance import normalize
from bagu_trainer.infrastructure.store import Store


@dataclass(frozen=True)
class SourcePlan:
    kind: str
    repositories: tuple
    targets: tuple
    remaining: tuple
    pointer_digest: str
    estimated_bytes: int
    last_source: bool
    requires_switch: bool
    versions: tuple
    database: str
    learning: dict
    evidence: str
    commits: tuple
    legacy: tuple


class SourceDeletion:
    def __init__(self, cleanup):
        self.cleanup = cleanup
        self.root, self.data = cleanup.root, cleanup.data
        self.journal = self.data / "source-deletion.json"

    def _db_path(self):
        path = self.root / "config.json"
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        value = Path(settings.get("db_path") or "data/trainer.db").expanduser()
        path = value if value.is_absolute() else self.root / value
        from bagu_trainer.infrastructure.cleanup_manifest import safe_path
        from bagu_trainer.infrastructure.cleanup_manifest import learning_database
        safe_path(path)
        if path.exists() and not learning_database(path):
            raise ValueError("外置数据库结构不属于本应用，拒绝删除或替换。")
        return path.absolute()

    def _inspect(self, path, selected, index):
        return Store.source_manifest_at(path, selected, index)

    def plan(self, repositories, protected=()):
        from bagu_trainer.infrastructure.bank_cleanup import CleanupTarget
        from bagu_trainer.infrastructure.bank_cleanup import _link
        from bagu_trainer.infrastructure.bank_cleanup import LEGACY_FILES
        self.cleanup._roots()
        if self.journal.exists():
            raise ValueError("存在未完成删除；请先点击“重试未完成清理”或重启恢复。")
        known = load_sources(self.cleanup.service.manifest)
        selected = {r.casefold() for r in repositories}
        if not selected or selected - {r.casefold() for r in known}:
            raise ValueError("请选择来源清单中的仓库。")
        current = active_bank_dir(self.root)
        protected = {Path(p).resolve() for p in protected}
        versions, targets, all_dirs, commits, evidence = [], {}, [], set(), []
        directories = [self.data] if (self.data / "questions.json").exists() else []
        directories += sorted((self.data / "banks").glob("*"))
        active_entries = []
        local_commits = set()
        for directory in directories:
            if directory != self.data:
                self.cleanup._target(directory.relative_to(self.data))
            record = directory / "sources.json"
            if not record.is_file() or _link(record):
                raise ValueError(f"版本 {directory.name} 缺少安全来源记录，无法保证彻底删除。")
            raw = record.read_bytes()
            entries = json.loads(raw)["sources"]
            if len(normalize(entries)) != len(entries):
                raise ValueError("版本来源缺少完整 commit，无法确认归属。")
            evidence.append((str(directory), hashlib.sha256(raw).hexdigest()))
            all_dirs.append(directory)
            commits.update((e["repository"].casefold(), e["commit"].lower()) for e in entries)
            local_commits.update((e["repository"].casefold(), e["commit"].lower()) for e in entries)
            if directory == current:
                active_entries = entries
            if any(e["repository"].casefold() in selected for e in entries):
                keep = tuple((e["repository"], e["commit"]) for e in entries if e["repository"].casefold() not in selected)
                versions.append((str(directory.relative_to(self.data)), keep, directory == current))
                if directory == self.data:
                    for name in LEGACY_FILES:
                        if (directory / name).exists():
                            targets[str(Path(name))] = (directory / name, "旧式题库 " + name, True)
                else:
                    targets[str(directory.relative_to(self.data))] = (directory, "版本 " + directory.name, directory == current or directory.resolve() in protected)
        # Hash-addressed caches can be attributed even after their completion
        # marker was lost, provided that the pair is in retained provenance.
        from dataclasses import replace
        commits.update((s.repository.casefold(), s.commit) for s in known.values())
        expected = {cache_directory(self.data / "source-cache", replace(s, commit=commit)).name: (repo, commit)
                    for repo, commit in commits for s in known.values() if s.repository.casefold() == repo}
        for directory in sorted((self.data / "source-cache").glob("*")):
            self.cleanup._target(directory.relative_to(self.data))
            pair = expected.get(directory.name)
            record = directory / "complete.json"
            if record.exists():
                if _link(record):
                    raise ValueError("缓存来源记录是链接")
                metadata = json.loads(record.read_text(encoding="utf-8"))["source"]
                values = normalize([metadata])
                if not values:
                    raise ValueError("缓存来源无法核对")
                pair = values[0]["repository"], values[0]["commit"]
                source = next((s for s in known.values() if s.repository.casefold() == pair[0]), None)
                if not source or cache_directory(directory.parent, replace(source, commit=pair[1])) != directory:
                    raise ValueError("缓存路径与来源身份不符")
            if pair is None:
                raise ValueError(f"缓存 {directory.name} 来源未知；清理未完成，请修复来源记录。")
            if pair[0] in selected:
                local_commits.add(pair)
                in_use = any((pair[0], pair[1]) == (e["repository"].casefold(), e["commit"].lower()) for e in active_entries)
                targets[str(directory.relative_to(self.data))] = (directory, f"缓存 {pair[0]} @ {pair[1]}", in_use)
        for directory in sorted((self.data / ".bank-builds").glob("*")):
            if directory.name == "owner.lock":
                continue
            self.cleanup._target(directory.relative_to(self.data))
            intent = directory / "build-intent.json"
            if not intent.exists() or _link(intent):
                raise ValueError(f"临时版本 {directory.name} 无法确认归属；请检查后重试。")
            entries = normalize(json.loads(intent.read_text(encoding="utf-8"))["sources"])
            local_commits.update((e["repository"], e["commit"]) for e in entries)
            if any(e["repository"] in selected for e in entries):
                targets[str(directory.relative_to(self.data))] = (directory, "未完成构建 " + directory.name, False)
        # Inventory every byte, including contents, so approval becomes stale if
        # files change without changing the outer directory identity.
        from bagu_trainer.infrastructure.legacy_artifacts import plan_legacy
        index = bank_index(all_dirs)
        legacy = plan_legacy(self.data, repositories, index, known.values(), targets)
        for rewrite in legacy:
            path = self.cleanup._target(rewrite.relative)
            targets[rewrite.relative] = (path, "旧式备份/派生文件 " + rewrite.relative, False)
        total, result, identities = 0, [], {}
        for relative, (path, label, used) in sorted(targets.items()):
            info = path.stat()
            result.append(CleanupTarget(relative, label, used, (info.st_dev, info.st_ino)))
            for file, info in self.cleanup._files(path):
                key = (info.st_dev, info.st_ino)
                count, _, _ = identities.get(key, (0, 0, 0))
                identities[key] = (count + 1, info.st_size, info.st_nlink)
                evidence.append((str(file), hashlib.sha256(file.read_bytes()).hexdigest()))
        total = sum(size for count, size, links in identities.values() if count >= links)
        database = self._db_path()
        learning = self._inspect(database, repositories, index)
        remaining = tuple((e["repository"], e["commit"]) for e in active_entries if e["repository"].casefold() not in selected)
        used = any(e["repository"].casefold() in selected for e in active_entries)
        return SourcePlan("sources", tuple(sorted(repositories)), tuple(result), remaining,
            self.cleanup._pointer()[1], total, used and not remaining, used,
            tuple(versions), str(database), learning,
            hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
            tuple(sorted(pair for pair in local_commits if pair[0] in selected)), legacy)

    def _write(self, state):
        atomic_json(self.journal, state)

    def _register_database(self):
        if Path(config.USER_DATA_ROOT).resolve() == self.root:
            from bagu_trainer.infrastructure.cleanup_manifest import register_profile
            register_profile(global_index=True)

    def _pointer(self, raw):
        path = self.data / "active-bank.json"
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            atomic_json(path, raw)

    def _database_file(self, path, state):
        from bagu_trainer.infrastructure.cleanup_manifest import safe_path
        from bagu_trainer.infrastructure.cleanup_manifest import learning_database
        path = Path(path)
        safe_path(path)
        db = Path(state["database"])
        expected = {db, Path(str(db) + state["suffix"]), Path(str(db) + state["suffix"] + ".new")}
        if path not in expected:
            raise ValueError("恢复记录数据库路径不匹配")
        if path.exists() and not learning_database(path):
            raise ValueError("恢复数据库身份校验失败")
        return path

    def _remove_database(self, path, state):
        path = self._database_file(path, state)
        for suffix in ("-wal", "-shm", "-journal", ""):
            from bagu_trainer.infrastructure.cleanup_manifest import safe_path
            target = Path(str(path) + suffix)
            safe_path(target)
            target.unlink(missing_ok=True)

    def _rollback(self, state):
        for rewrite in state.get("legacy_rewrites", []):
            holder = self.cleanup._target(rewrite["holder"])
            original = self.cleanup._target(rewrite["relative"])
            backup = holder / "original.json"
            if backup.exists():
                list(self.cleanup._files(holder))
                actual = hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
                if actual != rewrite["old_sha"]:
                    if actual not in {None, rewrite.get("new_sha")}:
                        raise ValueError(f"旧式备份在维护期间被修改，已保留并等待核对：{original}")
                    os.replace(backup, original)
        backup = self._database_file(state["backup"], state)
        if backup.exists():
            self._remove_database(state["database"], state)
            os.replace(backup, state["database"])
        self._pointer(state["old_pointer"])
        self._register_database()
        self._remove_database(state["replacement"], state)
        for relative in state["prepared"]:
            path = self.cleanup._target(relative)
            if path.exists():
                list(self.cleanup._files(path))
                shutil.rmtree(path)
        self.journal.unlink()

    def _finish(self, state):
        from bagu_trainer.infrastructure.bank_cleanup import CleanupResult
        result = CleanupResult([], [], state["switched"], state["empty"])
        try:
            self._register_database()
        except Exception as error:
            result.failures.append(f"数据库位置登记：{error}")
        for relative in state["targets"]:
            try:
                path = self.cleanup._target(relative)
                if path == active_bank_dir(self.root):
                    raise ValueError("对象仍是当前版本")
                if path.exists():
                    list(self.cleanup._files(path))
                    if path.is_dir():
                        import time
                        deadline = time.monotonic() + 6
                        while True:
                            try:
                                shutil.rmtree(path)
                                break
                            except OSError as error:
                                if getattr(error, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                                    raise
                                time.sleep(.1)
                    else:
                        path.unlink()
                if path.exists():
                    raise OSError("删除后仍存在")
                result.removed.append(relative)
            except Exception as error:
                result.failures.append(f"{relative}：{error}")
        for key in ("backup", "replacement"):
            try:
                self._remove_database(state[key], state)
            except Exception as error:
                result.failures.append(f"{state[key]}：{error}")
        try:
            remaining_index = bank_index([self.data, *sorted((self.data / "banks").glob("*"))])
            final = self._inspect(Path(state["database"]), state["repositories"], remaining_index)
            if final["affected"] or final["unknown"] or final["unknown_states"]:
                raise ValueError("学习来源关联残留或未知记录仍存在")
        except Exception as error:
            result.failures.append(f"学习数据库核验：{error}")
        if not result.failures:
            self.journal.unlink()
            # Re-enumerate all published/cache/temp objects as a final audit.
            try:
                check = self.plan(state["repositories"])
                if check.targets or check.learning["affected"]:
                    raise ValueError("来源文件仍有残留")
            except Exception as error:
                self._write(state)
                result.failures.append(f"最终残留核验：{error}")
        return result

    def recover(self, engine=None):
        if not self.journal.exists():
            return None
        from bagu_trainer.infrastructure.cleanup_manifest import safe_path
        safe_path(self.journal)
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        if state.get("schema") != 1 or state.get("root") != str(self.root):
            raise ValueError("删除恢复记录无效")
        self.cleanup._roots()
        maintenance = getattr(engine, "content_maintenance", None)
        lock = maintenance() if maintenance is not None else nullcontext()
        with lock if lock is not None else nullcontext():
            with _build_lock(self.data / ".bank-builds"):
                if engine is not None:
                    engine.invalidate_content()
                if state["phase"] == "cleaning":
                    result = self._finish(state)
                else:
                    self._rollback(state)
                    result = None
                if engine is not None:
                    engine.reload_bank()
                return result

    def execute(self, plan, *, engine=None, protected=(), include_unknown=False, progress=None):
        emit = progress or (lambda message: None)
        maintenance = getattr(engine, "content_maintenance", None)
        lock = maintenance() if maintenance is not None else nullcontext()
        with lock if lock is not None else nullcontext():
            with _build_lock(self.data / ".bank-builds") as lease:
                emit("1/5 重新核对文件与学习记录")
                loaded = getattr(getattr(engine, "kb", None), "concepts_path", None)
                if loaded:
                    protected = [*protected, Path(loaded).parent]
                if self.plan(plan.repositories, protected) != plan:
                    raise ValueError("数据已变化，请刷新清单并重新确认。")
                learning_unknown = len(plan.learning["unknown"]) + len(plan.learning["unknown_states"])
                unknown_count = learning_unknown + sum(rewrite.unknown for rewrite in plan.legacy)
                if unknown_count and not include_unknown:
                    raise ValueError(f"有 {unknown_count} 组来源不明旧快照、作答或学习状态，需要额外确认一并删除。")
                database = Path(plan.database)
                suffix = ".bagu-delete-" + uuid.uuid4().hex
                loaded = getattr(getattr(engine, "kb", None), "concepts_path", None)
                switching_engine = bool(loaded and Path(loaded).parent != active_bank_dir(self.root))
                state = {"schema": 1, "root": str(self.root), "phase": "preparing",
                    "repositories": list(plan.repositories), "database": str(database), "suffix": suffix,
                    "backup": str(database) + suffix, "replacement": str(database) + suffix + ".new",
                    "old_pointer": json.loads(self.cleanup._pointer()[0]) if self.cleanup._pointer()[0] else None,
                    "targets": [t.relative for t in plan.targets if t.relative not in {r.relative for r in plan.legacy if r.kept}],
                    "prepared": [], "legacy_rewrites": [],
                    "switched": plan.requires_switch or switching_engine, "empty": plan.last_source}
                self._write(state)
                try:
                    emit("2/5 仅使用本地资料准备保留版本")
                    for rewrite in plan.legacy:
                        if not rewrite.kept:
                            continue
                        holder = self.data / ".bank-builds" / uuid.uuid4().hex
                        relative = str(holder.relative_to(self.data))
                        entry = {"relative": rewrite.relative, "holder": relative, "old_sha": rewrite.digest}
                        state["prepared"].append(relative)
                        state["targets"].append(relative)
                        state["legacy_rewrites"].append(entry)
                        self._write(state)
                        holder.mkdir()
                        original = self.cleanup._target(rewrite.relative)
                        if hashlib.sha256(original.read_bytes()).hexdigest() != rewrite.digest:
                            raise ValueError("旧式备份已变化，请重新确认")
                        shutil.copyfile(original, holder / "original.json")
                        atomic_json(holder / "new.json", json.loads(rewrite.kept))
                        if json.loads((holder / "new.json").read_text(encoding="utf-8")) != json.loads(rewrite.kept):
                            raise ValueError("旧式备份保留内容校验失败")
                        entry["new_sha"] = hashlib.sha256((holder / "new.json").read_bytes()).hexdigest()
                        self._write(state)
                    replacement_pointer = state["old_pointer"]
                    for relative, keep, active in plan.versions:
                        if keep:
                            # Persist the generation intent before creating it.
                            # Build itself is atomic and the startup cleanup also
                            # discovers orphan complete generations by provenance.
                            result = self.cleanup.service.build([r for r, _ in keep], versions=dict(keep),
                                local_only=True, activate=False, _lease=lease)
                            state["prepared"].append(str(result.directory.relative_to(self.data)))
                            self._write(state)
                            if active:
                                replacement_pointer = {"schema_version": 1, "generation": result.generation}
                        elif active:
                            self.cleanup._empty_bank(None)
                            replacement_pointer = json.loads(self.cleanup._pointer()[0])
                            state["prepared"].append(str(active_bank_dir(self.root).relative_to(self.data)))
                            self._pointer(state["old_pointer"])
                            self._write(state)
                    emit("3/5 生成并核验学习数据库替代副本")
                    replacement = Path(state["replacement"])
                    replace_db = database.exists() and bool(plan.learning["affected"] or learning_unknown or plan.learning["affected_states"])
                    if replace_db:
                        Store.backup_database(database, replacement)
                        store = Store(replacement)
                        try:
                            store.remove_sources(plan.learning, include_unknown=include_unknown)
                        finally:
                            store.close()
                    state["phase"] = "switching"
                    self._write(state)
                    if engine is not None and callable(getattr(engine, "invalidate_content", None)):
                        engine.invalidate_content()
                    for rewrite in state["legacy_rewrites"]:
                        original = self.cleanup._target(rewrite["relative"])
                        holder = self.cleanup._target(rewrite["holder"])
                        if hashlib.sha256(original.read_bytes()).hexdigest() != rewrite["old_sha"]:
                            raise ValueError("旧式备份已变化，请重新确认")
                        os.replace(holder / "new.json", original)
                    if replace_db:
                        # Checkpoint the old connection before moving its file.
                        Store.checkpoint_database(database)
                        os.replace(database, state["backup"])
                        for extra in ("-wal", "-shm", "-journal"):
                            Path(str(database) + extra).unlink(missing_ok=True)
                        os.replace(replacement, database)
                    self._pointer(replacement_pointer)
                    if engine is not None:
                        engine.reload_bank()
                    emit("4/5 切换完成，清除原版本、缓存与旧数据库")
                    state["phase"] = "cleaning"
                    self._write(state)
                except BaseException:
                    # Simulated process death can leave the journal for recovery.
                    self._rollback(state)
                    if engine is not None:
                        engine.reload_bank()
                    raise
                emit("5/5 核验残留；失败项可继续清理")
                return self._finish(state)
