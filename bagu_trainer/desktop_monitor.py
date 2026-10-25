"""Debounced desktop activity detection; UI-independent and testable offline."""
import hashlib
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
        self.ui_post = None
        self.ui_state = {"window_state": "starting"}

    def poll(self):
        if self.engine is None or not self.capabilities_ready:
            return {"state": "not_ready", "projects": 0}
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            self.waiting.clear()
            self.failed_generations.clear()
            self.failure_reasons.clear()
            return {"state": "bank_empty", "projects": 0}
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = engine.load_config().get("project_roots", [str(engine.PROJECT_ROOT)])
        roots = list(dict.fromkeys(engine._normalize_project_root(r) for r in roots))
        for removed in self.initialized - set(roots):
            self.waiting.pop(removed, None)
            self.observed.pop(removed, None)
        self.initialized.intersection_update(roots)
        self.failed_generations = {r: f for r, f in self.failed_generations.items() if r in roots}
        self.failure_reasons = {r: reason for r, reason in self.failure_reasons.items()
                                if r in self.failed_generations}
        result = {"state": "watching" if roots else "not_ready", "projects": len(roots)}
        for root in roots:
            # All desktop activity shares the same scoped context and snapshot pipeline.
            info = engine.observe_project(
                root, reader_factory=ActivityDiffReader if root not in self.initialized else None)
            if info.get("error"):
                result["state"] = "project_unavailable"
                continue
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
                continue
            self.waiting.pop(root, None)
            info = pending[1]
            state = self._generate(root, info)
            if state != "watching":
                result["state"] = state
        if result["state"] == "watching" and self.failed_generations:
            result["state"] = "generation_unavailable"
        return result

    def _generate(self, root, info, failed_ids=()):
        engine = self.engine
        self.generating = True
        if self.ui_post:
            self.ui_post(("generation", {"busy": True, "project": root}))
        try:
            matches, generated, skipped, unavailable = engine.generate_activity(
                root, info, retry_failed_ids=failed_ids)
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

    def retry_generation(self):
        """Explicit retry of failed items; preserve scope, dedupe and cooldown."""
        if getattr(getattr(self.engine, "kb", None), "explicit_empty", False):
            self.failed_generations.clear()
            self.failure_reasons.clear()
            return {"state": "bank_empty", "projects": 0}
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = engine.load_config().get("project_roots", [str(engine.PROJECT_ROOT)])
        roots = {engine._normalize_project_root(r) for r in roots}
        result = {"state": "watching" if roots else "not_ready", "projects": len(roots)}
        for root, (info, failed_ids) in list(self.failed_generations.items()):
            if root not in roots:
                self.failed_generations.pop(root, None)
                self.failure_reasons.pop(root, None)
                continue
            result["state"] = self._generate(root, info, failed_ids)
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
