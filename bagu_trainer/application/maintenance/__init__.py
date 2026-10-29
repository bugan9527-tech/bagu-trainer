"""Content maintenance protocol shared by startup and both desktop adapters.

Plans are single-use approvals, not filesystem instructions supplied by a UI.
The existing cleanup adapters retain path checks, the build lease, SQLite
replacement and the durable source-deletion journal. This module coordinates
those operations with the live learning session and application notifications.
"""
import copy
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MaintenancePreview:
    token: str
    kind: str
    repositories: tuple
    targets: tuple
    estimated_bytes: int
    last_source: bool
    requires_switch: bool
    learning: dict
    commits: tuple
    unknown_legacy: int


class ContentMaintenance:
    def __init__(self, *, cleanup=None, engine=None, before_change=None,
                 completed=None, unavailable=None, busy=None):
        self._cleanup_adapter = cleanup
        self._engine = engine or (lambda: None)
        self._before_change = before_change or (lambda: None)
        self._completed = completed or (lambda: None)
        self._unavailable = unavailable or (lambda: None)
        self._busy = busy or (lambda: False)
        self._lock = threading.RLock()
        self._plans = {}
        self._closed = False
        self._needs_reload = False

    def _cleanup(self):
        if self._cleanup_adapter is None:
            from bagu_trainer.infrastructure.bank_build.service import BankBuildService
            from bagu_trainer.infrastructure.bank_cleanup import BankCleanupService
            self._cleanup_adapter = BankCleanupService(BankBuildService())
        return self._cleanup_adapter

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("应用正在退出，内容维护已关闭。")

    def _ensure_available(self):
        # Called before our lock: bank application can hold its task-state lock
        # while entering maintenance. Never acquire that lock in reverse order.
        if self._busy():
            raise ValueError("请等待当前题库任务结束。")

    def recovery_pending(self):
        return (self._cleanup().data / "source-deletion.json").exists()

    def plan(self, kind="sources", repositories=(), *, protected=()):
        self._ensure_available()
        with self._lock:
            self._ensure_open()
            if kind not in {"sources", "cache", "history", "temporary"}:
                raise ValueError("不支持的清理范围。")
            # Loaded versions are protected here, rather than inspected by UI.
            engine = self._engine()
            loaded = getattr(getattr(engine, "kb", None), "concepts_path", None)
            if loaded:
                protected = (*protected, Path(loaded).parent)
            plan = self._cleanup().plan(kind, repositories, protected=protected)
            token = uuid.uuid4().hex
            self._plans = {token: (plan, tuple(protected))}
            return MaintenancePreview(token, plan.kind, plan.repositories,
                plan.targets, plan.estimated_bytes, plan.last_source,
                plan.requires_switch, copy.deepcopy(getattr(plan, "learning", {})),
                getattr(plan, "commits", ()),
                sum(item.unknown for item in getattr(plan, "legacy", ())))

    def _change(self, operation, *, invalidate, notify=True):
        """Caller holds workflow lock; callbacks never run under learning lock.

        Order: workflow -> monitor reset (released) -> session -> build lease
        -> Store. Model HTTP remains outside all these locks. Invalidation
        retires windows captured before maintenance, including failed attempts.
        No path here initializes or replaces a closed learning session.
        """
        self._ensure_open()
        self._plans.clear()
        self._before_change()
        engine = self._engine()
        lock = engine.content_maintenance() if engine is not None else nullcontext()
        try:
            with lock:
                try:
                    if invalidate and engine is not None:
                        engine.invalidate_content()
                    result = operation(engine)
                    if engine is not None and result is not None and getattr(result, "failures", ()):
                        engine.learning.content_unavailable()
                        self._needs_reload = True
                    else:
                        self._needs_reload = False
                except BaseException:
                    # Fail closed before releasing the session lock: a waiting
                    # generation/submission must not hydrate from an old bank.
                    if engine is not None:
                        engine.learning.content_unavailable()
                        self._needs_reload = True
                    raise
        except BaseException:
            self._unavailable()
            raise
        finally:
            self._before_change()
        if result is not None and getattr(result, "failures", ()):
            self._unavailable()
        if notify:
            self._completed()
        return result

    def clean(self, token, *, include_unknown=False, progress=None):
        self._ensure_available()
        with self._lock:
            self._ensure_open()
            approval = self._plans.pop(str(token), None)
            if approval is None:
                raise ValueError("清理计划已失效，请重新预览。")
            plan, protected = approval
            return self._change(lambda engine: self._cleanup().execute(
                plan, engine=engine, protected=protected,
                include_unknown=include_unknown, progress=progress), invalidate=True)

    def recover(self):
        """Before readiness/engine loading: rollback or finish, never guess.

        No journal is a read-only no-op. Repeated recovery is safe; adapter
        errors and partial failures remain visible and keep their journal.
        """
        with self._lock:
            self._ensure_open()
            if not self.recovery_pending():
                if self._needs_reload:
                    # A reload/rollback may fail after its journal was removed.
                    # Retry loading the on-disk bank before allowing learning.
                    return self._change(lambda engine: engine.learning.replace_question_bank()
                                        if engine is not None else None,
                                        invalidate=True, notify=False)
                return None
            from bagu_trainer.infrastructure.source_deletion import SourceDeletion
            return self._change(lambda engine: SourceDeletion(self._cleanup()).recover(engine),
                                invalidate=True)

    def apply_bank(self):
        """Apply a completed build on the existing serial command lane."""
        with self._lock:
            self._ensure_open()
            if self.recovery_pending():
                raise ValueError("存在未完成删除，请先恢复后再应用题库。")
            return self._change(lambda engine: engine.learning.replace_question_bank()
                                if engine is not None else None, invalidate=False,
                                # Builds already publish bank_built/bank_applied.
                                # Do not discard practice drafts with a deletion event.
                                notify=False)

    def close(self):
        """Wait for accepted maintenance; reject late recovery/application."""
        with self._lock:
            self._closed = True
            self._plans.clear()
