"""UI-independent desktop commands shared by the Electron windows.

Long-running scans, model tests and bank builds have separate execution lanes.
Learning writes and content maintenance stay serialized on the command lane.
"""
import dataclasses
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, secrets
from .desktop_monitor import DesktopMonitor
from .history_service import LearningHistory
from .startup import StartupCoordinator


def public_value(value):
    if dataclasses.is_dataclass(value):
        return public_value(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): public_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [public_value(item) for item in value]
    return value


class DesktopService:
    COMMANDS = frozenset({
        "snapshot", "prepare", "projects.save", "monitor.pause", "monitor.retry",
        "practice.enter", "practice.resume", "practice.next", "practice.submit",
        "history.list", "history.detail", "history.repractice", "model.test",
        "model.cancel", "model.save", "bank.build", "bank.cancel", "bank.inventory",
        "bank.plan", "bank.clean", "settings.save", "shutdown",
        "startup.status", "startup.set", "uninstall.preview", "uninstall.launch",
    })

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
        self._verified = None
        self._model_cancel = threading.Event()
        self._bank_cancel = threading.Event()
        self._model = {"state": "idle", "message": "测试连接成功后即可保存。"}
        self._bank = {"state": "idle", "message": "选择来源后下载并构建题库。"}
        self._plans = {}
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
        from .bank_build.service import load_sources
        from .startup import check_readiness
        from .version import __version__
        error = ""
        try:
            settings = config.read_user_config()
        except (OSError, ValueError):
            settings = {}
            error = "无法读取 config.json，请修复 JSON 格式或文件权限后重试。"
        llm = settings.get("llm") or {}
        if not isinstance(llm, dict):
            llm = {}
        try:
            key_source = secrets.api_key_source(llm)
        except Exception:
            key_source = "none"
        report = check_readiness()
        pending = self.monitor.list_pending() if self.ready else []
        try:
            stats = self.monitor.engine.get_learning_stats() if self.ready else self.history.dashboard()["stats"]
        except Exception:
            stats = {"total_attempts": 0, "accuracy": 0, "due_reviews": 0}
        with self._state_lock:
            model, bank = dict(self._model), dict(self._bank)
        return {
            "version": __version__, "ready": self.ready, "error": error,
            "checks": [public_value(item) for item in report.checks],
            "status": self._status, "paused": self.monitor.paused,
            "projects": settings.get("project_roots") or [],
            "project_states": self.monitor.project_snapshot()["projects"],
            "pending": pending, "stats": stats,
            "model": {"base_url": llm.get("base_url", ""), "model": llm.get("model", ""),
                      "timeout_sec": llm.get("timeout_sec", 30), "key_source": key_source},
            "model_task": model, "bank_task": bank,
            "sources": [public_value(source) for source in load_sources().values()],
            "settings": {key: settings.get(key, config.DEFAULTS[key]) for key in (
                "max_quizzes_per_hit", "session_ingest", "context_char_limit", "quiz_ttl_seconds")},
            "user_data": str(config.USER_DATA_ROOT),
            "bank_count": len(self.monitor.engine.kb.all()) if self.monitor.engine else 0,
        }

    def prepare(self):
        from .bank_build.service import BankBuildService
        from .bank_cleanup import BankCleanupService
        from .source_deletion import SourceDeletion
        deletion = SourceDeletion(BankCleanupService(BankBuildService()))
        result = deletion.recover(self.monitor.engine)
        if result is not None and result.failures:
            raise ValueError(result.message)
        engine = self.coordinator.prepare()
        if engine is not None:
            self.monitor.engine = engine
        self.monitor.set_ready(engine is not None and self.coordinator.report.ready)
        if self.ready:
            self.monitor.apply_projects(config.read_user_config().get("project_roots") or [])
        self._status = "正在监听项目；文件稳定 15 秒后匹配知识点。" if self.ready else (
            self.coordinator.report.error or "请在侧栏完成使用准备。")
        self.publish()
        return {"ok": True, "ready": self.ready}

    def _require_ready(self):
        if not self.ready:
            raise ValueError("请先在题库管理、模型配置、监听项目中完成准备。")

    def _model_test(self, values):
        from .onboarding import effective_settings, test_connection
        with self._state_lock:
            if self._model["state"] == "testing":
                raise ValueError("连接测试正在进行。")
            settings = effective_settings(values.get("base_url", ""), values.get("model", ""),
                values.get("api_key", ""), values.get("timeout_sec", 30))
            self._verified = None
            self._model_cancel = threading.Event()
            cancel = self._model_cancel
            self._model = {"state": "testing", "message": "正在测试连接…"}

        def work():
            try:
                verified = test_connection(settings, cancel=cancel)
                with self._state_lock:
                    if cancel.is_set():
                        raise ValueError("连接测试已取消。")
                    self._verified = verified
                    self._model = {"state": "verified", "message": "连接成功，可以保存。",
                                   "form_revision": values.get("form_revision")}
            except Exception as error:
                from .onboarding import SetupError
                message = str(error) if isinstance(error, (SetupError, ValueError)) else "连接测试未完成，请重试。"
                with self._state_lock:
                    self._model = {"state": "cancelled" if cancel.is_set() else "failed", "message": message}
            self.publish()

        self._tasks.submit(work)
        self.publish()
        return {"ok": True}

    def _build(self, values):
        from .bank_build.service import BankBuildService
        from .bank_build.control import BuildCancelled, BuildError
        with self._state_lock:
            if self._bank["state"] in {"building", "applying", "cleaning"}:
                raise ValueError("题库任务正在进行。")
            retry_failed = None
            repositories = values.get("repositories") or []
            if values.get("retry_failed"):
                retry_failed = [repo for repo, source in self._bank.get("sources", {}).items() if source["state"] == "failed"]
                repositories = self._bank.get("repositories", [])
                if not retry_failed:
                    raise ValueError("没有需要重试的失败来源。")
            self._bank_cancel = threading.Event()
            cancel = self._bank_cancel
            self._bank = {"state": "building", "stage": "prepare", "message": "正在准备题库…",
                          "repositories": repositories, "sources": {}}

        def progress(event):
            with self._state_lock:
                self._bank.update(stage=event.stage, message=event.message,
                    completed=event.completed, total=event.total)
                if event.source is not None:
                    self._bank["sources"][event.source.repository] = public_value(event.source)
            self.emit({"type": "bank_progress", "data": dict(self._bank)})

        def work():
            try:
                result = BankBuildService().build(repositories, retry_failed=retry_failed,
                    timeout=values.get("timeout", 1800), cancel=cancel, progress=progress,
                    force_download=bool(values.get("force_download")))
                with self._state_lock:
                    for source in getattr(result, "sources", ()):
                        self._bank["sources"][source.repository] = public_value(source)
                    self._bank.update(state="applying", message=f"已构建 {result.questions} 个知识点，正在应用。")
                # All content switches go through the same serialized command lane.
                self.emit({"type": "bank_built", "data": {"questions": result.questions}})
            except Exception as error:
                with self._state_lock:
                    for source in getattr(error, "sources", ()):
                        self._bank["sources"][source.repository] = public_value(source)
                    self._bank.update(state="cancelled" if isinstance(error, BuildCancelled) else "failed",
                        message=str(error) if isinstance(error, BuildError) else "题库构建失败，请检查来源及数据目录后重试。")
                self.publish()

        self._tasks.submit(work)
        self.publish()
        return {"ok": True}

    def command(self, method, values=None):
        values = values or {}
        if method not in self.COMMANDS or not isinstance(values, dict):
            raise ValueError("不支持的桌面操作。")
        if self.stop.is_set() and method != "shutdown":
            raise ValueError("应用正在退出，已停止接收新任务。")
        if method == "snapshot":
            return self.snapshot()
        if method == "prepare":
            if self._bank["state"] == "applying":
                if self.monitor.engine is not None:
                    self.monitor.engine.reload_bank()
                with self._state_lock:
                    self._bank.update(state="applied", message="题库已构建并应用。")
            return self.prepare()
        if method == "projects.save":
            roots = values.get("projects")
            if not isinstance(roots, list) or any(not isinstance(item, str) for item in roots):
                raise ValueError("项目目录必须是路径列表。")
            roots = list(dict.fromkeys(str(Path(item).resolve(strict=True)) for item in roots))
            for root in roots:
                if not Path(root).is_dir():
                    raise ValueError("请选择可读取的项目文件夹。")
                with os.scandir(root):
                    pass
            with config.config_transaction():
                settings = config.read_user_config()
                settings["project_roots"] = roots
                config._write_config_atomic(settings)
            if self.monitor.engine is not None:
                self.monitor.apply_projects(roots)
            return self.prepare()
        if method == "monitor.pause":
            self.monitor.set_paused(bool(values.get("paused")))
        elif method == "monitor.retry":
            self._require_ready()
            if self._background is None or self._background.done():
                self._background = self._generation.submit(self.monitor.retry_generation)
        elif method.startswith("practice."):
            self._require_ready()
            if method == "practice.submit":
                if values.get("answer") not in tuple("ABCD"):
                    raise ValueError("请选择 A–D 中的一个答案。")
                result = self.monitor.engine.submit_answer(str(values.get("quiz_id", "")), values["answer"])
                self.publish()
                return result
            if method == "practice.enter":
                quiz = self.monitor.enter_quiz(str(values.get("quiz_id", "")))
            elif method == "practice.next":
                pending = self.monitor.list_pending()
                target = next((item for item in pending if item["quiz_id"] != values.get("quiz_id")), None)
                quiz = self.monitor.enter_quiz(target["quiz_id"]) if target else None
            else:
                quiz = self.monitor.resume_quiz()
            self.publish()
            return {"ok": True, "quiz": quiz}
        elif method.startswith("history."):
            history = self.monitor.engine if self.ready else self.history
            if method == "history.list":
                return history.get_answer_history(page=values.get("page", 0), result=values.get("result", "all"),
                    project=values.get("project"), keyword=str(values.get("keyword", "")))
            if method == "history.detail":
                return history.get_answer_detail(int(values["attempt_id"]))
            self._require_ready()
            result = self.monitor.engine.repractice_attempt(int(values["attempt_id"]))
            self.publish()
            return result
        elif method == "model.test":
            return self._model_test(values)
        elif method == "model.cancel":
            self._model_cancel.set()
        elif method == "model.save":
            from .onboarding import effective_settings, save_verified_model
            settings = effective_settings(values.get("base_url", ""), values.get("model", ""),
                values.get("api_key", ""), values.get("timeout_sec", 30))
            with self._state_lock:
                verified = self._verified
                if verified is None or settings != verified.settings:
                    raise ValueError("配置已变化，请重新测试连接后保存。")
                save_verified_model(verified)
                self._verified = None
                self._model = {"state": "saved", "message": "模型配置已保存，立即生效。"}
            return self.prepare()
        elif method == "bank.build":
            return self._build(values)
        elif method == "bank.cancel":
            self._bank_cancel.set()
        elif method == "bank.inventory":
            from .bank_build.service import load_sources
            from .bank_inventory import BankInventory
            return public_value(BankInventory(config.USER_DATA_ROOT).scan(load_sources()))
        elif method in {"bank.plan", "bank.clean"}:
            from .bank_build.service import BankBuildService
            from .bank_cleanup import BankCleanupService
            if self._bank["state"] in {"building", "applying", "cleaning"}:
                raise ValueError("请等待当前题库任务结束。")
            cleanup = BankCleanupService(BankBuildService())
            if method == "bank.plan":
                kind = values.get("kind", "sources")
                if kind not in {"sources", "cache", "history", "temporary"}:
                    raise ValueError("不支持的清理范围。")
                plan = cleanup.plan(kind, values.get("repositories") or [])
                token = uuid.uuid4().hex
                self._plans = {token: plan}
                return {"token": token, "kind": plan.kind, "repositories": plan.repositories,
                        "estimated_bytes": plan.estimated_bytes, "last_source": plan.last_source,
                        "learning": getattr(plan, "learning", {}),
                        "targets": [item.label for item in plan.targets]}
            plan = self._plans.pop(str(values.get("token", "")), None)
            if plan is None:
                raise ValueError("清理计划已失效，请重新预览。")
            self.monitor.reset()
            result = cleanup.execute(plan, engine=self.monitor.engine,
                                     include_unknown=bool(values.get("include_unknown")))
            self.prepare()
            self.emit({"type": "content_changed", "data": {}})
            return {"ok": not result.failures, "note": result.message}
        elif method == "settings.save":
            limits = {"max_quizzes_per_hit": (1, 5), "context_char_limit": (500, 12000),
                      "quiz_ttl_seconds": (0, 31536000)}
            updated = {}
            for key, (low, high) in limits.items():
                value = values.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} 超出允许范围。")
                updated[key] = value
            updated["session_ingest"] = bool(values.get("session_ingest"))
            with config.config_transaction():
                settings = config.read_user_config()
                settings.update(updated)
                config._write_config_atomic(settings)
            if self.monitor.engine is not None:
                self.monitor.engine.cfg.update(updated)
        elif method in {"startup.status", "startup.set"}:
            from . import login_startup, paths
            executable = paths.INSTALL_ROOT / "BaguTrainer.exe"
            supported = paths.FROZEN and executable.is_file()
            if method == "startup.set":
                if not supported:
                    raise ValueError("请在打包后的 Electron 应用中设置登录自启动。")
                if values.get("enabled"):
                    login_startup.install(executable=executable)
                else:
                    login_startup.uninstall()
            enabled = False
            shortcut = login_startup.shortcut_directory() / "BaguTrainer.lnk"
            if supported and shortcut.is_file():
                saved = login_startup.read_shortcut(shortcut)
                enabled = Path(saved["target"]).resolve() == executable.resolve() and saved["arguments"] == "--background"
            return {"ok": True, "supported": supported, "enabled": enabled}
        elif method == "uninstall.preview":
            from .cleanup_cli import preview
            return {"detail": preview()}
        elif method == "uninstall.launch":
            from .cleanup_cli import launch_uninstall
            owner_pid = os.environ.get("BAGU_ELECTRON_OWNER_PID")
            launch_uninstall(wait_pid=int(owner_pid) if owner_pid else None)
            return {"ok": True}
        elif method == "shutdown":
            self.close()
            return {"ok": True}
        self.publish()
        return {"ok": True}

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
                states = {"watching": "正在监听项目；文件稳定 15 秒后匹配知识点。",
                          "quiz_ready": "新题已准备好，请在练习中心查看。",
                          "generation_unavailable": "本次出题未完成，可手动重试。",
                          "project_unavailable": "有项目目录不可读取，请检查监听项目。",
                          "paused": "监听已暂停，仍可练习已有题目。"}
                self._status = states.get((result or {}).get("state"), self._status)
            except Exception as error:
                self._status = f"本次检测未完成（{type(error).__name__}），请检查目录或模型配置。"
            self._background = None
        if self.ready and self._background is None:
            self._background = self._generation.submit(self.monitor.poll)
        self.publish()

    def close(self):
        self.stop.set()
        self.monitor.stop.set()
        self._model_cancel.set()
        self._bank_cancel.set()
        self._tasks.shutdown(wait=True, cancel_futures=True)
        self._generation.shutdown(wait=True, cancel_futures=True)
        if self.monitor.engine is not None:
            self.monitor.engine.session.close()
