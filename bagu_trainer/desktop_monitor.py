"""Debounced desktop activity detection; UI-independent and testable offline."""
import hashlib
import copy
import os
import time
import difflib
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from .diff_reader import DiffReader, DIFF_LIMIT, _file_allowed


class ActivityDiffReader(DiffReader):
    """Desktop watcher retains a bounded baseline to capture edits beyond file headers."""
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
        # A Git working-tree diff is not an activity stream: commits can clear
        # it between polls, and old large diffs can hide new edits after truncation.
        # Compare files with our own startup baseline in Git projects as well.
        return self.watch_changes()

    def watch_changes(self, recent_minutes=30):
        current = self._snapshot_files()
        with self._lock:
            if self._snapshot is None:
                # Cache recently edited files first, within a fixed memory budget.
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
                    # Evicted baseline: use a bounded whole-file sample, never claim a diff.
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


class DesktopMonitor:
    def __init__(self, engine, quiet_seconds=15, clock=time.monotonic):
        self._state_lock = threading.RLock()
        self._epoch = 0
        self.engine = engine
        self.capabilities_ready = engine is not None
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.observed = {}
        self.waiting = {}
        self.initialized = set()
        self.paused = False
        self.active = None
        self.delivery_paused = threading.Event()
        self.failed_generations = {}
        self.failure_reasons = {}
        self.generating = False
        self.stop = threading.Event()
        self.ui_post = None
        self.ui_state = {"window_state": "starting"}
        self.applied_roots = None
        self.project_states = {}
        self.project_aliases = {}
        self.scope_version = 0
        self._status_sequence = 0
        self._last_project_snapshot = None

    def project_snapshot(self):
        """Pure data only; safe for immediate page entry without doing any I/O."""
        with self._state_lock:
            return {"version": self.scope_version, "sequence": self._status_sequence,
                    "ready": self.engine is not None and self.capabilities_ready,
                    "paused": self.paused, "applied_roots": list(self.applied_roots or []),
                    "aliases": dict(self.project_aliases),
                    "projects": copy.deepcopy(self.project_states)}

    def _publish_projects_locked(self):
        snapshot = self.project_snapshot()
        snapshot.pop("sequence")
        if snapshot == self._last_project_snapshot:
            return
        self._last_project_snapshot = snapshot
        self._status_sequence += 1
        if self.ui_post:
            self.ui_post(("project_states", self.project_snapshot()))

    def _project_state_locked(self, root, state, *, success=False, reason="", activity=""):
        old = self.project_states.get(root, {})
        self.project_states[root] = {"state": state,
            "last_success_at": time.time() if success else old.get("last_success_at"),
            "reason": reason, "activity": activity}

    def apply_projects(self, paths):
        """Commit only after session application succeeds; retire old scan reports."""
        with self._state_lock:
            engine = self.engine
            if engine is None:
                raise RuntimeError("监听引擎尚未就绪")
            normalize = getattr(engine, "_normalize_project_root", str)
            aliases = {os.path.normcase(os.path.abspath(str(path))): normalize(path) for path in paths}
            roots = list(dict.fromkeys(aliases.values()))
            old_roots = set(self.applied_roots or self.initialized)
            removed = old_roots - set(roots)
            session = getattr(engine, "session", None)
            setter = getattr(session, "set_watch_roots", None)
            if setter is not None:
                setter(roots)
            elif removed and session is not None:
                session.forget_projects(removed)
            self._epoch += 1
            self.scope_version += 1
            self.applied_roots = roots
            self.project_aliases = aliases
            self.initialized.intersection_update(roots)
            for mapping in (self.observed, self.waiting, self.failed_generations,
                            self.failure_reasons, self.project_states):
                for root in set(mapping) - set(roots):
                    mapping.pop(root, None)
            for root in roots:
                if root not in self.project_states:
                    self._project_state_locked(root, "not_ready" if not self.capabilities_ready else
                                               "paused" if self.paused else "initializing")
            self._publish_projects_locked()

    def set_ready(self, ready):
        with self._state_lock:
            self._epoch += 1
            self.capabilities_ready = ready
            for root in self.applied_roots or []:
                if not ready:
                    self._project_state_locked(root, "not_ready")
                elif self.project_states.get(root, {}).get("state") == "not_ready":
                    self._project_state_locked(root, "paused" if self.paused else "initializing")
            self._publish_projects_locked()

    def reset(self):
        """Retire callbacks before a bank cleanup can replace persisted content."""
        with self._state_lock:
            self._epoch += 1
            self.waiting.clear()
            self.initialized.clear()
            self.failed_generations.clear()
            self.failure_reasons.clear()
            for root in self.applied_roots or []:
                self._project_state_locked(root, "not_ready" if not self.capabilities_ready else
                                           "paused" if self.paused else "initializing")
            self._publish_projects_locked()

    def set_paused(self, paused):
        with self._state_lock:
            self._epoch += 1
            self.paused = paused
            self.waiting.clear()
            self.initialized.clear()
            for root in self.applied_roots or []:
                self._project_state_locked(root, "paused" if paused else "initializing")
            self._publish_projects_locked()

    def poll(self):
        if self.stop.is_set():
            return {"state": "paused", "projects": 0}
        tasks = []
        with self._state_lock:
            if (self.engine is not None and self.capabilities_ready and not self.paused
                    and not getattr(getattr(self.engine, "kb", None), "explicit_empty", False)
                    and self.applied_roots is None):
                self.apply_projects(self.engine.load_config().get("project_roots", [str(self.engine.PROJECT_ROOT)]))
            epoch = self._epoch
            token = getattr(getattr(self.engine, "session", None), "_generation_token", None)
            revision = token() if token is not None else None
        result = self._scan(tasks, epoch)
        for root, info in tasks:
            state = self._generate(root, info, epoch=epoch, revision=revision)
            if state != "watching":
                result["state"] = state
        return result

    def _scan(self, tasks, epoch):
        with self._state_lock:
            if epoch != self._epoch:
                return {"state": "watching", "projects": 0}
            if self.engine is None or not self.capabilities_ready:
                self.set_ready(False)
                return {"state": "not_ready", "projects": 0}
            if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
                self.waiting.clear()
                self.failed_generations.clear()
                self.failure_reasons.clear()
                for root in self.applied_roots or []:
                    self._project_state_locked(root, "not_ready", reason="当前无可用题库，请先完成准备。")
                self._publish_projects_locked()
                return {"state": "bank_empty", "projects": 0}
            if self.paused:
                return {"state": "paused", "projects": 0}
            engine = self.engine
            roots = list(self.applied_roots or [])
            for removed in self.initialized - set(roots):
                self.waiting.pop(removed, None)
                self.observed.pop(removed, None)
            self.initialized.intersection_update(roots)
            self.failed_generations = {r: f for r, f in self.failed_generations.items() if r in roots}
            self.failure_reasons = {r: reason for r, reason in self.failure_reasons.items()
                                    if r in self.failed_generations}
            result = {"state": "watching" if roots else "not_ready", "projects": len(roots)}
        for root in roots:
            with self._state_lock:
                if self.stop.is_set() or epoch != self._epoch or self.paused:
                    break
                baseline = root not in self.initialized
            # Filesystem I/O must not hold the metadata or learning session lock.
            try:
                info = engine.observe_project(root, reader_factory=ActivityDiffReader if baseline else None)
            except Exception as error:
                info = {"error": True, "check_reason": "目录无法检查，请确认权限或连接。" if isinstance(error, OSError)
                        else "项目检查未完成，请稍后重试。"}
            with self._state_lock:
                if epoch != self._epoch or self.paused:
                    continue
                if info.get("error"):
                    result["state"] = "project_unavailable"
                    self._project_state_locked(root, "unavailable", reason=info.get("check_reason") or "目录不可用，请确认路径、权限或连接。")
                    self._publish_projects_locked()
                    continue
                self._project_state_locked(root, "watching", success=True)
                self._publish_projects_locked()
                context = info.get("context") or ""
                fingerprint = hashlib.sha256(context.encode("utf-8")).hexdigest()
                if root not in self.initialized:
                    # Login establishes a baseline; old uncommitted work is not a new task.
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
                        limit = int(engine.load_config().get("context_char_limit") or 4000)
                        info["context"] = (previous[1]["context"] + "\n" + context)[-limit:]
                    self.waiting[root] = (self.clock(), info)
                pending = self.waiting.get(root)
                if pending is None or self.clock() - pending[0] < self.quiet_seconds:
                    if pending is not None:
                        self.project_states[root]["activity"] = "等待文件稳定"
                        self._publish_projects_locked()
                    continue
                self.waiting.pop(root, None)
                info = pending[1]
                tasks.append((root, info))
            with self._state_lock:
                if epoch == self._epoch and root in self.project_states:
                    self.project_states[root]["activity"] = "等待文件稳定" if root in self.waiting else ""
                    self._publish_projects_locked()
        with self._state_lock:
            if result["state"] == "watching" and self.failed_generations:
                result["state"] = "generation_unavailable"
        return result

    def _generate(self, root, info, failed_ids=(), *, epoch=None, revision=None):
        with self._state_lock:
            if self.stop.is_set() or (epoch is not None and epoch != self._epoch):
                return "watching"
            epoch = self._epoch
            engine = self.engine
            revision = revision or engine.session._generation_token()
            self.generating = True
            if root in self.project_states:
                self.project_states[root]["activity"] = "正在出题"
                self._publish_projects_locked()
        if self.ui_post:
            self.ui_post(("generation", {"busy": True, "project": root}))
        try:
            matches, generated, skipped, unavailable = engine.generate_activity(
                root, info, retry_failed_ids=failed_ids, revision=revision, stop=self.stop)
            with self._state_lock:
                if (epoch != self._epoch or engine is not self.engine
                        or not engine.session._accepts_result(revision)):
                    if self.ui_post:
                        self.ui_post(("generation", {"busy": False, "retryable": False, "reason": ""}))
                    return "watching"
                service_failures = [item for item in unavailable if item.get("failure_kind") != "quality"]
                if service_failures:
                    self.failed_generations[root] = (info, tuple(item["kp_id"] for item in service_failures))
                    reason = service_failures[0].get("reason") or "暂时无法出题，请检查模型配置后重试。"
                    self.failure_reasons[root] = reason
                    state = "generation_unavailable"
                else:
                    self.failed_generations.pop(root, None)
                    self.failure_reasons.pop(root, None)
                    if generated:
                        reason = "题目已准备好，可开始作答。"
                    elif unavailable:
                        reason = "等待新的代码改动。"
                    elif not matches:
                        reason = (f"已检测到代码改动，但未匹配到题库知识点（当前 {len(engine.kb.all())} 个）；"
                                  "未调用 AI。可更换或扩充题库来源。")
                    else:
                        reason = f"已匹配知识点，其中 {skipped} 个因重复、待答或复习冷却跳过；未调用 AI。"
                    state = "quiz_ready" if generated else "watching"
                # A quiet skip in one project must not replace another project's
                # actionable service diagnostic with a generic waiting message.
                if self.failed_generations and not service_failures:
                    failed_root = next(iter(self.failed_generations))
                    reason = self.failure_reasons.get(failed_root) or "暂时无法出题，请检查模型配置后重试。"
                    state = "generation_unavailable"
                elif self.paused and state == "watching":
                    state = "paused"
                # Counts only: never persist project code, prompts or credentials.
                logging.getLogger("bagu-trainer").info(
                    "[monitor] checked files=%d context_chars=%d bank=%d matched=%d generated=%d skipped=%d unavailable=%d state=%s",
                    len((info.get("changes") or {}).get("files") or []), len(info["context"]),
                    len(engine.kb.all()), len(matches), len(generated), skipped, len(unavailable), state)
                if self.ui_post:
                    self.ui_post(("generation", {"busy": False, "state": state, "reason": reason,
                                                  "retryable": bool(self.failed_generations)}))
                return state
        finally:
            self.generating = False
            with self._state_lock:
                if epoch == self._epoch and root in self.project_states:
                    self.project_states[root]["activity"] = ""
                    self._publish_projects_locked()

    def retry_generation(self):
        """Explicit retry of failed items; preserve scope, dedupe and cooldown."""
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            self.failed_generations.clear()
            self.failure_reasons.clear()
            return {"state": "bank_empty", "projects": 0}
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = self.applied_roots if self.applied_roots is not None else engine.load_config().get("project_roots", [str(engine.PROJECT_ROOT)])
        roots = {engine._normalize_project_root(r) for r in roots}
        result = {"state": "watching" if roots else "not_ready", "projects": len(roots)}
        with self._state_lock:
            epoch = self._epoch
            failures = list(self.failed_generations.items())
        for root, (info, failed_ids) in failures:
            if root not in roots:
                self.failed_generations.pop(root, None)
                self.failure_reasons.pop(root, None)
                continue
            result["state"] = self._generate(root, info, failed_ids, epoch=epoch)
        return result

    def take_quiz(self):
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            return None
        if self.paused or self.active or self.delivery_paused.is_set():
            return None
        quiz = self.engine.session.take_quiz()
        if quiz:
            self.active = quiz["quiz_id"]
        return quiz

    def list_pending(self):
        """Browsing and pause state never deliver a question."""
        if self.engine is None or not self.capabilities_ready:
            return []
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            return []
        return self.engine.session.list_pending()

    def pending_count(self):
        """待答题数：未作答且未过期的 ready+delivered 题数（含人工重练）。"""
        return len(self.list_pending())

    def enter_quiz(self, quiz_id):
        """Explicit practice remains available while monitoring is paused."""
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            return None
        quiz = self.engine.session.enter_quiz(quiz_id)
        if quiz:
            self.active = quiz["quiz_id"]
        return quiz

    def resume_quiz(self):
        """Explicit practice remains available while monitoring is paused."""
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            return None
        quiz = self.engine.session.resume_quiz()
        if quiz:
            self.active = quiz["quiz_id"]
        return quiz

    def window_command(self, name, arguments):
        if name == "_desktop_status":
            return {"ok": True, **self.ui_state}
        if name in {"_show_dashboard", "_navigate"}:
            page = arguments.get("page", "dashboard") if name == "_navigate" else "dashboard"
            if page not in {"dashboard", "practice", "history", "projects", "bank", "model", "settings"}:
                return {"ok": False}
            if self.ui_post:
                self.ui_post(("show", page))
            return {"ok": self.ui_post is not None}
        return {"ok": False, "note": "不支持的窗口操作"}


def add_project(path):
    """Explicit project selection from the desktop's folder picker."""
    from . import config
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("请选择项目文件夹")
    with config.config_transaction():
        configuration = config.read_user_config()
        roots = configuration.get("project_roots") or []
        normalized = {str(Path(r).resolve()).casefold() for r in roots}
        if str(resolved).casefold() not in normalized:
            configuration["project_roots"] = [*roots, str(resolved)]
            config._write_config_atomic(configuration)
