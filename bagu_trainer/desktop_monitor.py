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
        self.quiet_seconds = quiet_seconds
        self.clock = clock
        self.observed = {}
        self.waiting = {}
        self.initialized = set()
        self.paused = False
        self.active = None
        self.delivery_paused = threading.Event()
        self.failed_generations = {}
        self.generating = False
        self.ui_post = None
        self.ui_state = {"window_state": "starting"}

    def poll(self):
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
        roots = list(dict.fromkeys(engine._normalize_project_root(r) for r in roots))
        for removed in self.initialized - set(roots):
            self.waiting.pop(removed, None)
            self.observed.pop(removed, None)
        self.initialized.intersection_update(roots)
        self.failed_generations = {r: f for r, f in self.failed_generations.items() if r in roots}
        result = {"state": "watching", "projects": len(roots)}
        for root in roots:
            # All desktop activity shares the same scoped context and snapshot pipeline.
            with engine._STATE_LOCK:
                if root not in self.initialized:
                    engine._diff_readers[root] = ActivityDiffReader(root)
                info = engine._collect_context("", root)
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
            with engine._STATE_LOCK:
                caps = engine._caps()
                kwargs = {"retry_failed_ids": failed_ids} if failed_ids else {}
                matches, generated, skipped, unavailable = engine._generate_matches_locked(
                    info["context"], root, engine.cfg["max_quizzes_per_hit"], caps,
                    " / ".join(info.get("sources") or [])[:200], **kwargs)
                engine._cleanup_locked(caps)
            if unavailable:
                self.failed_generations[root] = (info, tuple(item["kp_id"] for item in unavailable))
                reason = unavailable[0].get("reason") or "暂时无法出题，请检查模型配置后重试。"
                state = "generation_unavailable"
            else:
                self.failed_generations.pop(root, None)
                if generated:
                    reason = "题目已准备好，可开始作答。"
                elif not matches:
                    reason = (f"已检测到代码改动，但未匹配到题库知识点（当前 {len(engine.kb.all())} 个）；"
                              "未调用 AI。可更换或扩充题库来源。")
                else:
                    reason = f"已匹配知识点，其中 {skipped} 个因重复、待答或复习冷却跳过；未调用 AI。"
                state = "quiz_ready" if generated else "watching"
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
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
        roots = {engine._normalize_project_root(r) for r in roots}
        result = {"state": "watching", "projects": len(roots)}
        for root, (info, failed_ids) in list(self.failed_generations.items()):
            if root not in roots:
                self.failed_generations.pop(root, None)
                continue
            result["state"] = self._generate(root, info, failed_ids)
        return result

    def take_quiz(self):
        """Only one automatic popup at a time; answered/delivered items are skipped."""
        if self.paused or self.active or self.delivery_paused.is_set():
            return None
        engine = self.engine
        with engine._STATE_LOCK:
            engine._get_store_locked()
            engine._cleanup_locked(engine._caps())
            roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
            scopes = {engine._normalize_project_root(r) for r in roots}
            for pending in sorted(engine._state["pending"], key=lambda p: p["created_at"]):
                if pending["status"] != "ready" or pending["project_root"] not in scopes:
                    continue
                engine._deliver_pending_locked(pending)
                self.active = pending["quiz_id"]
                return engine._public_pending(pending)
        return None

    def list_pending(self):
        """Read-only pending list (待答列表): ready + delivered, unanswered, in
        project scope (or manual), oldest first.  Viewing the list must NOT mark
        anything delivered — it only reads the current state after cleanup.
        Monitoring/automatic-delivery pauses do not hide existing practice.
        Expiry and dedupe/cooldown rules are enforced by _cleanup_locked."""
        engine = self.engine
        with engine._STATE_LOCK:
            engine._get_store_locked()
            engine._cleanup_locked(engine._caps())
            roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
            scopes = {engine._normalize_project_root(r) for r in roots}
            out = []
            for pending in sorted(engine._state["pending"], key=lambda p: (p["created_at"], p["quiz_id"])):
                if pending["status"] not in ("ready", "delivered"):
                    continue
                manual = bool(engine._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review"))
                if pending["project_root"] not in scopes and not manual:
                    continue
                public = engine._public_pending(pending)
                public["status"] = pending["status"]
                public["manual_review"] = manual
                out.append(public)
            return out

    def pending_count(self):
        """待答题数：未作答且未过期的 ready+delivered 题数（含人工重练）。"""
        return len(self.list_pending())

    def enter_quiz(self, quiz_id):
        """按题进入：显式选中一条待答题交付作答。仅当用户主动选择时才把
        ready 题交付；返回公开题目，题不存在/过期/越权时返回 None。
        主动进入不受监听暂停或自动推送暂停限制；自动交付由 take_quiz 控制。"""
        engine = self.engine
        with engine._STATE_LOCK:
            engine._get_store_locked()
            engine._cleanup_locked(engine._caps())
            roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
            scopes = {engine._normalize_project_root(r) for r in roots}
            for pending in sorted(engine._state["pending"], key=lambda p: (p["created_at"], p["quiz_id"])):
                if pending["quiz_id"] != quiz_id or pending["status"] not in ("ready", "delivered"):
                    continue
                manual = bool(engine._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review"))
                if pending["project_root"] not in scopes and not manual:
                    continue
                if pending["status"] == "ready":
                    engine._deliver_pending_locked(pending)
                self.active = pending["quiz_id"]
                public = engine._public_pending(pending)
                if manual:
                    public["manual_review"] = True
                return public
        return None

    def resume_quiz(self):
        engine = self.engine
        with engine._STATE_LOCK:
            engine._get_store_locked()
            engine._cleanup_locked(engine._caps())
            roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
            scopes = {engine._normalize_project_root(r) for r in roots}
            for pending in engine._state["pending"]:
                manual = engine._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review")
                if pending["status"] == "delivered" and (pending["project_root"] in scopes or manual):
                    self.active = pending["quiz_id"]
                    return {**engine._public_pending(pending), **({"manual_review": True} if manual else {})}
        return None

    def window_command(self, name, arguments):
        if name == "_desktop_status":
            return {"ok": True, **self.ui_state}
        if name == "_show_dashboard":
            if self.ui_post:
                self.ui_post(("show", None))
            return {"ok": self.ui_post is not None}
        return {"ok": False, "note": "不支持的窗口操作"}


def add_project(path):
    """Explicit project selection from the desktop's folder picker."""
    from .config import load_config, _write_config_atomic
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("请选择项目文件夹")
    configuration = load_config()
    roots = configuration.get("project_roots") or []
    normalized = {str(Path(r).resolve()).casefold() for r in roots}
    if str(resolved).casefold() not in normalized:
        configuration["project_roots"] = [*roots, str(resolved)]
        _write_config_atomic(configuration)
