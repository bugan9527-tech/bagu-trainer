"""Settings workflows for model verification and ordinary preferences.

The module owns the verification receipt and cancellation state. Callers can
start/cancel a check, save the exact checked form, or save ordinary settings;
credentials and receipts never cross this Interface.
"""
import os
import threading
from pathlib import Path

from bagu_trainer.infrastructure import config


class SettingsWorkflow:
    def __init__(self, *, executor, lock, publish):
        self._executor = executor
        self._lock = lock
        self._publish = publish
        self._verified = None
        self._cancel = threading.Event()
        self._task = {"state": "idle", "message": "测试连接成功后即可保存。"}

    def task_snapshot(self):
        with self._lock:
            return dict(self._task)

    def presentation(self):
        """Return only renderer-safe configuration and an error placeholder."""
        from bagu_trainer.infrastructure import secrets
        try:
            configuration = config.read_user_config()
            error = ""
        except (OSError, ValueError):
            configuration, error = {}, "无法读取 config.json，请修复 JSON 格式或文件权限后重试。"
        llm = configuration.get("llm") or {}
        if not isinstance(llm, dict):
            llm = {}
        try:
            key_source = secrets.api_key_source(llm)
        except Exception:
            key_source = "none"
        model = {"base_url": llm.get("base_url", ""), "model": llm.get("model", ""),
                 "timeout_sec": llm.get("timeout_sec", 30), "key_source": key_source}
        preferences = {key: configuration.get(key, config.DEFAULTS[key]) for key in (
            "max_quizzes_per_hit", "session_ingest", "context_char_limit", "quiz_ttl_seconds")}
        projects = configuration.get("project_roots") or []
        return projects, error, model, preferences

    def start_model_test(self, values):
        from bagu_trainer.application.settings.verification import effective_settings
        from bagu_trainer.application.settings.verification import test_connection

        with self._lock:
            if self._task["state"] == "testing":
                raise ValueError("连接测试正在进行。")
            settings = effective_settings(values.get("base_url", ""), values.get("model", ""),
                values.get("api_key", ""), values.get("timeout_sec", 30))
            self._verified = None
            self._cancel = threading.Event()
            cancel = self._cancel
            self._task = {"state": "testing", "message": "正在测试连接…"}

        def work():
            try:
                verified = test_connection(settings, cancel=cancel)
                with self._lock:
                    if cancel.is_set():
                        raise ValueError("连接测试已取消。")
                    self._verified = verified
                    self._task = {"state": "verified", "message": "连接成功，可以保存。",
                                  "form_revision": values.get("form_revision")}
            except Exception as error:
                from bagu_trainer.application.settings.verification import SetupError
                message = str(error) if isinstance(error, (SetupError, ValueError)) else "连接测试未完成，请重试。"
                with self._lock:
                    self._task = {"state": "cancelled" if cancel.is_set() else "failed",
                                  "message": message}
            self._publish()

        self._executor.submit(work)
        self._publish()
        return {"ok": True}

    def cancel_model_test(self):
        self._cancel.set()

    def save_verified_model(self, values):
        from bagu_trainer.application.settings.verification import effective_settings
        from bagu_trainer.application.settings.verification import save_verified_model

        settings = effective_settings(values.get("base_url", ""), values.get("model", ""),
            values.get("api_key", ""), values.get("timeout_sec", 30))
        with self._lock:
            verified = self._verified
            if verified is None or settings != verified.settings:
                raise ValueError("配置已变化，请重新测试连接后保存。")
            save_verified_model(verified)
            self._verified = None
            self._task = {"state": "saved", "message": "模型配置已保存，立即生效。"}

    def save_preferences(self, values, *, learning=None):
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
        if learning is not None:
            learning.update_settings(updated)

    def save_projects(self, paths):
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise ValueError("项目目录必须是路径列表。")
        roots = list(dict.fromkeys(str(Path(item).resolve(strict=True)) for item in paths))
        for root in roots:
            if not Path(root).is_dir():
                raise ValueError("请选择可读取的项目文件夹。")
            with os.scandir(root):
                pass
        with config.config_transaction():
            settings = config.read_user_config()
            settings["project_roots"] = roots
            config._write_config_atomic(settings)
        return roots

    def close(self):
        self._cancel.set()
