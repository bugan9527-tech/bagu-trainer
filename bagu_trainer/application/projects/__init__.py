"""Project watch scope, file baselines, scan state and activity detection."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import difflib
import os
import threading
import time

from bagu_trainer.infrastructure.paths import canonical_project_root as canonical_root
from bagu_trainer.infrastructure.diff_reader import DiffReader
from bagu_trainer.infrastructure.diff_reader import DIFF_LIMIT
from bagu_trainer.infrastructure.diff_reader import _file_allowed


@dataclass(frozen=True)
class ProjectScope:
    aliases: dict
    roots: tuple


@dataclass(frozen=True)
class Activity:
    """A redacted, stable project observation ready for learning generation."""
    project_root: str
    context: str
    sources: tuple
    changes: dict

    def learning_input(self):
        return {"project_root": self.project_root, "context": self.context,
                "sources": list(self.sources), "changes": self.changes}


@dataclass(frozen=True)
class ScanResult:
    state: str
    projects: int
    activities: tuple = ()


class ActivityDiffReader(DiffReader):
    """Keep a bounded text baseline so edits beyond file headers are visible."""
    CACHE_BYTES = 8 * 1024 * 1024

    def __init__(self, project_root):
        super().__init__(project_root)
        self.texts = OrderedDict()
        self.text_bytes = 0

    def _remember(self, relative, text):
        previous = self.texts.pop(relative, "")
        self.text_bytes -= len(previous.encode("utf-8"))
        self.texts[relative] = text
        self.text_bytes += len(text.encode("utf-8"))
        while self.text_bytes > self.CACHE_BYTES:
            _, removed = self.texts.popitem(last=False)
            self.text_bytes -= len(removed.encode("utf-8"))

    def _read(self, relative):
        try:
            path = (self.root / relative).resolve()
            path.relative_to(self.root.resolve())
            return path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            return ""

    def changes(self):
        # Git diffs can disappear after commits and truncate away recent edits.
        return self.watch_changes()

    def watch_changes(self, recent_minutes=30):
        current = self._snapshot_files()
        with self._lock:
            if self._snapshot is None:
                budget = 0
                for relative in sorted(current, key=lambda rel: current[rel][0], reverse=True):
                    if budget + current[relative][1] > self.CACHE_BYTES:
                        continue
                    text = self._read(relative)
                    self._remember(relative, text)
                    budget += len(text.encode("utf-8"))
                self._snapshot = current
                return {"source": "watch", "files": [], "diff_text": ""}
            changed = [relative for relative, meta in current.items()
                       if self._snapshot.get(relative) != meta]
            changed += [relative for relative in self._snapshot if relative not in current]
            old_snapshot = self._snapshot
            self._snapshot = current
            parts = []
            for relative in changed:
                before = self.texts.get(relative)
                after = self._read(relative) if relative in current else ""
                self._remember(relative, after)
                if len(parts) >= 20:
                    continue
                if before is None and relative in old_snapshot:
                    piece = f"--- {relative} (baseline unavailable)\n{after[:DIFF_LIMIT]}"
                else:
                    piece = "\n".join(difflib.unified_diff(
                        (before or "").splitlines(), after.splitlines(),
                        fromfile=f"a/{relative}", tofile=f"b/{relative}", n=3))
                if piece:
                    parts.append(piece[:DIFF_LIMIT])
            return {"source": "watch", "files": changed[:20],
                    "diff_text": "\n".join(parts)[:DIFF_LIMIT]}

    def git_changes(self):
        result = super().git_changes()
        try:
            untracked = self._run_git(["ls-files", "--others", "--exclude-standard", "-z"])
            for relative in untracked.stdout.split("\0"):
                if not relative or not _file_allowed(relative):
                    continue
                path = self.root / relative
                if not path.is_file() or path.stat().st_size > 512 * 1024:
                    continue
                result["files"].append(relative)
                result["diff_text"] += f"\n--- new file: {relative}\n" + self._read(relative)[:DIFF_LIMIT]
        except (OSError, TimeoutError):
            pass
        result["files"] = sorted(set(result["files"]))[:20]
        result["diff_text"] = result["diff_text"][:DIFF_LIMIT]
        return result


class ProjectWatch:
    """Own watched-project state and turn observations into debounced Activity."""
    def __init__(self, *, quiet_seconds=15, clock=time.monotonic, lock=None):
        self.lock = lock or threading.RLock()
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.epoch = 0
        self.roots = None
        self.aliases = {}
        self.observed = {}
        self.waiting = {}
        self.initialized = set()
        self.paused = False
        self.ready = False
        self.states = {}
        self.scope_version = 0
        self.sequence = 0
        self._last_snapshot = None
        self._readers = {}

    def prepare_scope(self, paths):
        aliases = {os.path.normcase(os.path.abspath(str(path))): canonical_root(path)
                   for path in paths}
        roots = tuple(dict.fromkeys(aliases.values()))
        return ProjectScope(aliases=aliases, roots=roots)

    def commit_scope(self, scope, publish=None):
        with self.lock:
            old_roots = set(self.roots or self.initialized)
            removed = old_roots - set(scope.roots)
            self.epoch += 1
            self.scope_version += 1
            self.roots = list(scope.roots)
            self.aliases = dict(scope.aliases)
            self.initialized.intersection_update(scope.roots)
            for mapping in (self.observed, self.waiting, self.states):
                for root in set(mapping) - set(scope.roots):
                    mapping.pop(root, None)
            for root in removed:
                self._readers.pop(root, None)
            for root in scope.roots:
                if root not in self.states:
                    self._set_state_locked(root, "not_ready" if not self.ready else
                                           "paused" if self.paused else "initializing")
            self._changed_locked(publish)

    def snapshot(self):
        with self.lock:
            return {"version": self.scope_version, "sequence": self.sequence,
                    "ready": self.ready, "paused": self.paused,
                    "applied_roots": list(self.roots or []),
                    "aliases": dict(self.aliases), "projects": {
                        root: dict(value) for root, value in self.states.items()}}

    def _changed_locked(self, publish=None):
        snapshot = self.snapshot()
        comparable = dict(snapshot)
        comparable.pop("sequence")
        if comparable == self._last_snapshot:
            return
        self._last_snapshot = comparable
        self.sequence += 1
        if publish:
            publish()

    def _set_state_locked(self, root, state, *, success=False, reason="", activity=""):
        old = self.states.get(root, {})
        self.states[root] = {"state": state,
            "last_success_at": time.time() if success else old.get("last_success_at"),
            "reason": reason, "activity": activity}

    def set_activity(self, root, activity, publish=None):
        with self.lock:
            if root in self.states:
                self.states[root]["activity"] = activity
                self._changed_locked(publish)

    def set_ready(self, ready, publish=None):
        with self.lock:
            self.epoch += 1
            self.ready = ready
            for root in self.roots or []:
                if not ready:
                    self._set_state_locked(root, "not_ready")
                elif self.states.get(root, {}).get("state") == "not_ready":
                    self._set_state_locked(root, "paused" if self.paused else "initializing")
            self._changed_locked(publish)

    def reset(self, publish=None):
        with self.lock:
            self.epoch += 1
            self.waiting.clear()
            self.initialized.clear()
            for root in self.roots or []:
                self._set_state_locked(root, "not_ready" if not self.ready else
                                       "paused" if self.paused else "initializing")
            self._changed_locked(publish)

    def set_paused(self, paused, publish=None):
        with self.lock:
            self.epoch += 1
            self.paused = paused
            self.waiting.clear()
            self.initialized.clear()
            for root in self.roots or []:
                self._set_state_locked(root, "paused" if paused else "initializing")
            self._changed_locked(publish)

    def scan(self, observer, *, bank_available, context_limit, stop,
             publish=None):
        """Observe projects outside the state lock and emit stable activities."""
        with self.lock:
            if not self.ready:
                return ScanResult("not_ready", 0)
            if not bank_available:
                self.waiting.clear()
                for root in self.roots or []:
                    self._set_state_locked(root, "not_ready", reason="当前无可用题库，请先完成准备。")
                self._changed_locked(publish)
                return ScanResult("bank_empty", len(self.roots or []))
            if self.paused:
                return ScanResult("paused", 0)
            roots = list(self.roots or [])
            for removed in self.initialized - set(roots):
                self.waiting.pop(removed, None)
                self.observed.pop(removed, None)
                self._readers.pop(removed, None)
            self.initialized.intersection_update(roots)
            result_state = "watching" if roots else "not_ready"
            epoch = self.epoch
        activities = []
        for root in roots:
            with self.lock:
                if stop.is_set() or epoch != self.epoch or self.paused:
                    break
                baseline = root not in self.initialized
                reader = self._readers.get(root)
                if baseline or reader is None:
                    reader = ActivityDiffReader(root)
                    self._readers[root] = reader
            try:
                info = observer(root, reader)
            except Exception as error:
                info = {"error": True,
                        "check_reason": "目录无法检查，请确认权限或连接。" if isinstance(error, OSError)
                        else "项目检查未完成，请稍后重试。"}
            with self.lock:
                if epoch != self.epoch or self.paused:
                    continue
                if info.get("error"):
                    result_state = "project_unavailable"
                    self._set_state_locked(root, "unavailable",
                        reason=info.get("check_reason") or "目录不可用，请确认路径、权限或连接。")
                    self._changed_locked(publish)
                    continue
                self._set_state_locked(root, "watching", success=True)
                self._changed_locked(publish)
                context = info.get("context") or ""
                fingerprint = hashlib.sha256(context.encode("utf-8")).hexdigest()
                if root not in self.initialized:
                    self.initialized.add(root)
                    self.observed[root] = fingerprint
                    continue
                changes = info.get("changes") or {}
                if changes.get("source") == "git" and not context.strip():
                    self.waiting.pop(root, None)
                    self.observed[root] = fingerprint
                elif context.strip() and fingerprint != self.observed.get(root):
                    self.observed[root] = fingerprint
                    previous = self.waiting.get(root)
                    if previous and changes.get("source") == "watch":
                        info = dict(info)
                        info["context"] = (previous[1]["context"] + "\n" + context)[-context_limit:]
                    self.waiting[root] = (self.clock(), info)
                pending = self.waiting.get(root)
                if pending is None or self.clock() - pending[0] < self.quiet_seconds:
                    if pending is not None:
                        self.states[root]["activity"] = "等待文件稳定"
                        self._changed_locked(publish)
                    continue
                self.waiting.pop(root, None)
                ready = pending[1]
                activities.append(Activity(
                    project_root=ready.get("project_root") or root,
                    context=ready.get("context") or "",
                    sources=tuple(ready.get("sources") or ()),
                    changes=ready.get("changes") or {},
                ))
                self.states[root]["activity"] = ""
                self._changed_locked(publish)
        if result_state == "watching":
            return ScanResult(result_state, len(roots), tuple(activities))
        return ScanResult(result_state, len(roots), tuple(activities))
