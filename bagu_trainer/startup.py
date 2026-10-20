"""Desktop startup gate. Importing this module never loads the learning engine.

Readiness is derived from existing files on every launch, rather than a separate
first-run flag. Checks are local and read-only; they never rebuild the bank or DB.
"""
import importlib
import json
import math
import os
import queue
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import config, secrets


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str


@dataclass(frozen=True)
class StartupReport:
    checks: tuple
    error: str = ""

    @property
    def ready(self):
        return bool(self.checks) and all(item.ready for item in self.checks) and not self.error


def read_configuration():
    """Strict read for setup: malformed user files must never become defaults."""
    return config.read_user_config()


def _bank_check():
    try:
        from .bank_storage import active_bank_dir
        path = active_bank_dir() / "questions.json"
        bank = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(bank, list) or not bank:
            raise ValueError("empty bank")
        ids = set()
        for question in bank:
            if not isinstance(question, dict):
                raise ValueError("invalid question")
            for field in ("id", "knowledge_point", "question"):
                if not isinstance(question.get(field), str) or not question[field].strip():
                    raise ValueError("missing question field")
            if question["id"] in ids:
                raise ValueError("duplicate id")
            ids.add(question["id"])
            for field in ("tags", "keywords", "knowledge_tags"):
                values = question.get(field, [])
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError("invalid tags")
        return ReadinessCheck("bank", True, f"题库已就绪：{len(bank)} 个知识点。")
    except FileNotFoundError:
        detail = "尚无本地题库，请点击“下载并构建题库”选择来源。"
    except (OSError, ValueError, TypeError):
        detail = "题库为空、格式不正确或不可读取；可重新构建，旧题库会保留。"
    return ReadinessCheck("bank", False, detail)


def _model_check(configuration):
    llm = configuration.get("llm")
    if not isinstance(llm, dict):
        return ReadinessCheck("model", False, "请配置模型接口地址、模型名和 API Key。")
    try:
        address = urlsplit(llm.get("base_url", ""))
        model = llm.get("model", "")
        timeout = llm.get("timeout_sec", 30)
        valid = (address.scheme in {"http", "https"} and address.hostname
                 and not address.username and not address.password
                 and not address.query and not address.fragment
                 and isinstance(model, str) and model.strip()
                 and isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                 and math.isfinite(timeout) and timeout > 0)
        if valid and secrets.resolve_api_key(llm):
            from .onboarding import state_path, saved_model_verified
            if state_path().exists() and not saved_model_verified(configuration):
                return ReadinessCheck("model", False, "请在向导中主动测试模型连接并保存；接口或 Key 变化后需重新测试。")
            return ReadinessCheck("model", True, "模型配置已就绪（启动检查不发送网络请求）。")
    except (TypeError, ValueError, AttributeError):
        pass
    return ReadinessCheck("model", False, "模型配置不完整：检查接口地址、模型名、有效 Key 和正数超时。")


def _projects_check(configuration):
    roots = configuration.get("project_roots")
    if not isinstance(roots, list) or not roots:
        return ReadinessCheck("projects", False, "请选择至少一个要监听的代码项目文件夹。")
    try:
        # Existing saved absolute paths stay untouched. Relative paths would change
        # meaning when prepare_tk switches the process working directory.
        for root in roots:
            if not isinstance(root, str) or not root.strip():
                raise ValueError("invalid project path")
            path = Path(root)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("unavailable project")
            with os.scandir(path):
                pass
        return ReadinessCheck("projects", True, f"监听项目已就绪：{len(roots)} 个。")
    except (OSError, ValueError):
        return ReadinessCheck("projects", False, "有监听目录不存在、不可读取或不是绝对路径，请重新选择或修复配置。")


def check_readiness():
    """Public, engine-free local probe; the report contains no credentials."""
    bank = _bank_check()
    try:
        configuration = read_configuration()
    except (OSError, ValueError):
        return StartupReport((bank,
            ReadinessCheck("model", False, "无法读取 config.json，请修复 JSON 格式或文件权限。"),
            ReadinessCheck("projects", False, "配置文件修复后才能检查监听项目。")))
    return StartupReport((bank, _model_check(configuration), _projects_check(configuration)))


def set_projects(paths):
    """Setup adapter: replace only the explicitly selected watch list, atomically."""
    roots = list(dict.fromkeys(str(Path(path).resolve(strict=True)) for path in paths))
    check = _projects_check({"project_roots": roots})
    if not check.ready:
        raise ValueError(check.detail)
    configuration = read_configuration()
    configuration["project_roots"] = roots
    config._write_config_atomic(configuration)


class StartupCoordinator:
    def __init__(self, checker=None, engine_loader=None):
        self.checker = checker or check_readiness
        self.engine_loader = engine_loader or (lambda: importlib.import_module("bagu_trainer.engine"))
        self.events = queue.Queue()
        self.state = "checking"
        self.report = StartupReport(())
        self.monitor = None
        self.engine = None
        self.window_state = "starting"

    def prepare(self):
        """Recheck on every retry, and load the engine only after all gates pass."""
        self.state = "checking"
        try:
            self.report = self.checker()
            if not self.report.ready:
                self.state = "initialization"
                return None
            self.state = "loading_engine"
            if self.engine is None:
                self.engine = self.engine_loader()
            self.state = "ready"
            return self.engine
        except Exception as error:
            # Exception messages may contain URLs, config values or credentials.
            self.report = replace(self.report, error=f"初始化失败（{type(error).__name__}），请检查本地文件和依赖后重试。")
            self.state = "failed"
            return None

    def window_command(self, name, arguments):
        if self.monitor is not None:
            result = self.monitor.window_command(name, arguments)
            if name == "_desktop_status":
                result["startup_state"] = self.state
            return result
        if name == "_show_dashboard":
            self.events.put(("show", None))
            return {"ok": True}
        if name == "_desktop_status":
            return {"ok": True, "startup_state": self.state, "window_state": self.window_state,
                    "checks": {item.name: item.ready for item in self.report.checks}}
        return {"ok": False, "note": "不支持的窗口操作"}

    def run(self, background=False):
        engine = self.prepare()
        if engine is None:
            from .initialization import InitializationApp
            engine = InitializationApp(self).run()
        if engine is None:
            self.state = "stopped"
            return
        from .desktop import DesktopApp
        from .desktop_monitor import DesktopMonitor
        monitor = DesktopMonitor(engine)
        app = DesktopApp(monitor, background, events=self.events)
        self.monitor = monitor
        self.state = "running"
        try:
            app.run()
        finally:
            self.state = "stopped"
