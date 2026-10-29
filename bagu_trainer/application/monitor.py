"""Application workflow coordinating project activities and learning."""
import time
import logging
import threading
from pathlib import Path
from bagu_trainer.application.projects import Activity
from bagu_trainer.application.projects import ActivityDiffReader
from bagu_trainer.application.projects import ProjectWatch


class DesktopMonitor:
    _PROJECT_FIELDS = {
        "_epoch": "epoch", "capabilities_ready": "ready",
        "quiet_seconds": "quiet_seconds", "clock": "clock",
        "observed": "observed", "waiting": "waiting",
        "initialized": "initialized", "paused": "paused",
        "applied_roots": "roots", "project_states": "states",
        "project_aliases": "aliases", "scope_version": "scope_version",
        "_status_sequence": "sequence",
    }

    def __getattr__(self, name):
        field = self._PROJECT_FIELDS.get(name)
        if field is not None:
            projects = self.__dict__.get("projects")
            if projects is not None:
                return getattr(projects, field)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        field = self._PROJECT_FIELDS.get(name)
        projects = self.__dict__.get("projects")
        if field is not None and projects is not None:
            setattr(projects, field, value)
            return
        object.__setattr__(self, name, value)

    def __init__(self, engine, quiet_seconds=15, clock=time.monotonic):
        self._state_lock = threading.RLock()
        self.projects = ProjectWatch(quiet_seconds=quiet_seconds, clock=clock,
                                     lock=self._state_lock)
        self.engine = engine
        self.projects.ready = engine is not None
        self.active = None
        self.delivery_paused = threading.Event()
        self.failed_generations = {}
        self.failure_reasons = {}
        self.generating = False
        self.stop = threading.Event()
        self.ui_post = None
        self.ui_state = {"window_state": "starting"}

    def project_snapshot(self):
        """Pure data only; safe for immediate page entry without doing any I/O."""
        return self.projects.snapshot()

    def _learning(self):
        return self.engine.learning if self.engine is not None else None

    def _publish_projects_locked(self):
        if self.ui_post:
            self.ui_post(("project_states", self.project_snapshot()))

    def apply_projects(self, paths):
        """Coordinate learning's allowlist before committing the project scope."""
        with self._state_lock:
            if self.engine is None:
                raise RuntimeError("监听引擎尚未就绪")
            scope = self.projects.prepare_scope(paths)
            self._learning().apply_project_scope(scope.roots)
            self.projects.commit_scope(scope, publish=self._publish_projects_locked)
            roots = set(scope.roots)
            self.failed_generations = {root: value for root, value in self.failed_generations.items()
                                       if root in roots}
            self.failure_reasons = {root: value for root, value in self.failure_reasons.items()
                                    if root in self.failed_generations}

    def set_ready(self, ready):
        self.projects.set_ready(ready, publish=self._publish_projects_locked)

    def reset(self):
        """Retire callbacks before a bank cleanup can replace persisted content."""
        self.projects.reset(publish=self._publish_projects_locked)
        self.failed_generations.clear()
        self.failure_reasons.clear()

    def set_paused(self, paused):
        self.projects.set_paused(paused, publish=self._publish_projects_locked)

    def poll(self):
        if self.stop.is_set():
            return {"state": "paused", "projects": 0}
        tasks = []
        with self._state_lock:
            if (self.engine is not None and self.capabilities_ready and not self.paused
                    and self._learning().has_question_bank()
                    and self.applied_roots is None):
                self.apply_projects(self.engine.load_config().get("project_roots", [str(self.engine.PROJECT_ROOT)]))
            epoch = self._epoch
            generation_window = (self._learning().generation_window()
                                 if self.engine is not None else None)
        result = self._scan(tasks, epoch)
        for root, info in tasks:
            state = self._generate(root, info, epoch=epoch,
                                   generation_window=generation_window)
            if state != "watching":
                result["state"] = state
        return result

    def _scan(self, tasks, epoch):
        engine = self.engine
        if engine is None or not self.capabilities_ready:
            self.set_ready(False)
            return {"state": "not_ready", "projects": 0}
        learning = self._learning()
        try:
            context_limit = int(engine.load_config().get("context_char_limit") or 4000)
        except Exception:
            context_limit = 4000
        result = self.projects.scan(
            lambda root, reader: engine.observe_project(root, reader=reader),
            bank_available=learning.has_question_bank(),
            context_limit=context_limit,
            stop=self.stop,
            publish=self._publish_projects_locked)
        if result.state == "bank_empty":
            self.failed_generations.clear()
            self.failure_reasons.clear()
        for activity in result.activities:
            tasks.append((activity.project_root, activity.learning_input()))
        state = result.state
        if state == "watching" and self.failed_generations:
            state = "generation_unavailable"
        return {"state": state, "projects": result.projects}

    def _generate(self, root, info, failed_ids=(), *, epoch=None,
                  generation_window=None):
        with self._state_lock:
            if self.stop.is_set() or (epoch is not None and epoch != self._epoch):
                return "watching"
            epoch = self._epoch
            engine = self.engine
            learning = self._learning()
            generation_window = generation_window or learning.generation_window()
            self.generating = True
            self.projects.set_activity(root, "正在出题", publish=self._publish_projects_locked)
        if self.ui_post:
            self.ui_post(("generation", {"busy": True, "project": root}))
        try:
            generated_result = learning.generate_for_activity(
                root, info, retry_failed_ids=failed_ids, stop=self.stop,
                generation_window=generation_window)
            matches, generated, skipped, unavailable = generated_result["result"]
            result_current = generated_result["current"]
            with self._state_lock:
                if (epoch != self._epoch or engine is not self.engine
                        or not result_current):
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
                        reason = (f"已检测到代码改动，但未匹配到题库知识点（当前 {learning.question_count()} 个）；"
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
                    learning.question_count(),
                    len(matches), len(generated), skipped, len(unavailable), state)
                if self.ui_post:
                    self.ui_post(("generation", {"busy": False, "state": state, "reason": reason,
                                                  "retryable": bool(self.failed_generations)}))
                return state
        finally:
            self.generating = False
            with self._state_lock:
                if epoch == self._epoch:
                    self.projects.set_activity(root, "", publish=self._publish_projects_locked)

    def retry_generation(self):
        """Explicit retry of failed items; preserve scope, dedupe and cooldown."""
        learning = self._learning()
        if not learning.has_question_bank():
            self.failed_generations.clear()
            self.failure_reasons.clear()
            return {"state": "bank_empty", "projects": 0}
        if self.paused:
            return {"state": "paused", "projects": 0}
        engine = self.engine
        roots = self.applied_roots if self.applied_roots is not None else engine.load_config().get("project_roots", [str(engine.PROJECT_ROOT)])
        roots = set(self.projects.prepare_scope(roots).roots)
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
        learning = self._learning()
        if not learning.has_question_bank():
            return None
        if self.paused or self.active or self.delivery_paused.is_set():
            return None
        quiz = learning.take_next_question()
        if quiz:
            self.active = quiz["quiz_id"]
        return quiz

    def list_pending(self):
        """Browsing and pause state never deliver a question."""
        if self.engine is None or not self.capabilities_ready:
            return []
        learning = self._learning()
        if not learning.has_question_bank():
            return []
        return learning.pending_questions()

    def pending_count(self):
        """待答题数：未作答且未过期的 ready+delivered 题数（含人工重练）。"""
        return len(self.list_pending())

    def enter_quiz(self, quiz_id):
        """Explicit practice remains available while monitoring is paused."""
        learning = self._learning()
        if not learning.has_question_bank():
            return None
        quiz = learning.enter_question(quiz_id)
        if quiz:
            self.active = quiz["quiz_id"]
        return quiz

    def resume_quiz(self):
        """Explicit practice remains available while monitoring is paused."""
        learning = self._learning()
        if not learning.has_question_bank():
            return None
        quiz = learning.resume_practice()
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
    from bagu_trainer.infrastructure import config
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

# Explicit compatibility exports used by evaluation/legacy callers.
__all__ = ['DesktopMonitor', 'Activity', 'ActivityDiffReader', 'add_project']
