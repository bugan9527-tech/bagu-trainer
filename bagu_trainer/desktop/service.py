"""Electron application adapter and process-level task coordinator.

It owns startup, event publication, execution lanes, monitor ticks and shutdown;
settings, bank tasks, command routing and presentation live in focused modules.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bagu_trainer.infrastructure import config
from bagu_trainer.application.monitor import DesktopMonitor
from bagu_trainer.desktop.presenter import public_value
from bagu_trainer.desktop.presenter import snapshot as present_snapshot
from bagu_trainer.desktop.presenter import monitor_status
from bagu_trainer.desktop.presenter import monitor_failure
from bagu_trainer.desktop.presenter import readiness_status
from bagu_trainer.desktop.commands import COMMANDS as DESKTOP_COMMANDS
from bagu_trainer.application.settings import SettingsWorkflow
from bagu_trainer.application.banks import BankWorkflow
from bagu_trainer.application.maintenance import ContentMaintenance
from bagu_trainer.application.learning.history import LearningHistory
from bagu_trainer.desktop.startup import StartupCoordinator


class DesktopService:
    COMMANDS = DESKTOP_COMMANDS

    def __init__(self, emit=None):
        self.emit = emit or (lambda event: None)
        self.coordinator = StartupCoordinator()
        self.monitor = DesktopMonitor(None)
        self.monitor.ui_post = self._monitor_event
        self.history = LearningHistory()
        self.stop = threading.Event()
        self._state_lock = threading.RLock()
        self._tasks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bagu-task")
        self._generation = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bagu-generation")
        self._background = None
        self.settings = SettingsWorkflow(executor=self._tasks, lock=self._state_lock, publish=self.publish)
        self.banks = BankWorkflow(executor=self._tasks, lock=self._state_lock,
                                  emit=lambda event: self.emit(event), publish=self.publish)
        self.maintenance = ContentMaintenance(
            engine=lambda: self.monitor.engine or self.coordinator.engine,
            before_change=self.monitor.reset,
            completed=lambda: self.emit({"type": "content_changed", "data": {}}),
            unavailable=lambda: self.monitor.set_ready(False), busy=self.banks.is_busy)
        self.coordinator.maintenance = self.maintenance
        self._status = "请完成题库、模型和监听项目的准备。"
        self._next_prune = time.monotonic() + 30

    @property
    def ready(self):
        return self.monitor.engine is not None and self.monitor.capabilities_ready

    def _monitor_event(self, event):
        name, value = event
        self.emit({"type": name, "data": public_value(value)})

    def publish(self):
        if not self.stop.is_set():
            self.emit({"type": "snapshot", "data": self.snapshot()})

    def snapshot(self):
        from bagu_trainer.infrastructure.bank_build.service import load_sources
        from bagu_trainer.desktop.startup import check_readiness
        from bagu_trainer.version import __version__

        projects, error, model, preferences = self.settings.presentation()
        report = check_readiness()
        pending = self.monitor.list_pending() if self.ready else []
        try:
            stats = self.monitor.engine.learning.learning_stats() if self.ready else self.history.dashboard()["stats"]
        except Exception:
            stats = {"total_attempts": 0, "accuracy": 0, "due_reviews": 0}
        return present_snapshot(
            version=__version__,
            ready=self.ready, error=error, checks=report.checks, status=self._status,
            paused=self.monitor.paused, projects=projects,
            project_states=self.monitor.project_snapshot()["projects"], pending=pending, stats=stats,
            model=model, model_task=self.settings.task_snapshot(), bank_task=self.banks.task_snapshot(),
            sources=list(load_sources().values()), settings=preferences,
            user_data=config.USER_DATA_ROOT,
            bank_count=self.monitor.engine.learning.question_count() if self.monitor.engine else 0)

    def prepare(self):
        recovery = self.maintenance.recover()
        if recovery is not None and recovery.failures:
            raise ValueError(recovery.message)
        # All preparation entry points finish accepted builds on the serial
        # command lane, including first use before an engine can be created.
        self.banks.apply_pending(self.maintenance)
        engine = self.coordinator.prepare()
        if engine is not None:
            self.monitor.engine = engine
        self.monitor.set_ready(engine is not None and self.coordinator.report.ready)
        if self.monitor.engine is not None:
            self.monitor.apply_projects(config.read_user_config().get("project_roots") or [])
        self._status = readiness_status(self.ready, self.coordinator.report.error)
        self.publish()
        return {"ok": True, "ready": self.ready}

    def _require_ready(self):
        if not self.ready:
            raise ValueError("请先在题库管理、模型配置、监听项目中完成准备。")

    def retry_generation(self):
        if self._background is None or self._background.done():
            self._background = self._generation.submit(self.monitor.retry_generation)

    def command(self, method, values=None):
        from bagu_trainer.desktop.commands import dispatch
        return dispatch(self, method, values)

    def tick(self):
        if self.stop.is_set():
            return
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + 30
            removed, remaining = self.coordinator.prune_projects()
            if removed:
                if self.monitor.engine is not None:
                    self.monitor.apply_projects(remaining)
                self.prepare()
        if self._background is not None and self._background.done():
            try:
                result = self._background.result()
                self._status = monitor_status(result, self._status)
            except Exception as error:
                self._status = monitor_failure(error)
            self._background = None
        if self.ready and self._background is None:
            self._background = self._generation.submit(self.monitor.poll)
        self.publish()

    def close(self):
        self.stop.set()
        self.monitor.stop.set()
        self.settings.close()
        self.banks.close()
        self._tasks.shutdown(wait=True, cancel_futures=True)
        self._generation.shutdown(wait=True, cancel_futures=True)
        self.maintenance.close()
        if self.monitor.engine is not None:
            close = getattr(self.monitor.engine, "close", None)
            if close is not None:
                close()
            else:  # Transition adapter for injected legacy engine modules.
                self.monitor.engine.learning.close()
