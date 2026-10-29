"""Validate desktop commands and route them to application workflows."""
import os
from pathlib import Path

from bagu_trainer.infrastructure import config
from bagu_trainer.desktop.presenter import public_value


COMMANDS = frozenset({
    "snapshot", "prepare", "projects.save", "monitor.pause", "monitor.retry",
    "practice.enter", "practice.resume", "practice.next", "practice.submit",
    "history.list", "history.detail", "history.repractice", "model.test",
    "model.cancel", "model.save", "bank.build", "bank.cancel", "bank.inventory",
    "bank.plan", "bank.clean", "settings.save", "shutdown",
    "startup.status", "startup.set", "uninstall.preview", "uninstall.launch",
})


def dispatch(service, method, values=None):
    values = values or {}
    if method not in COMMANDS or not isinstance(values, dict):
        raise ValueError("不支持的桌面操作。")
    if service.stop.is_set() and method != "shutdown":
        raise ValueError("应用正在退出，已停止接收新任务。")
    if method == "snapshot":
        return service.snapshot()
    if method == "prepare":
        return service.prepare()
    if method == "projects.save":
        service.settings.save_projects(values.get("projects"))
        return service.prepare()
    if method == "monitor.pause":
        service.monitor.set_paused(bool(values.get("paused")))
    elif method == "monitor.retry":
        service._require_ready()
        service.retry_generation()
    elif method.startswith("practice."):
        return _practice(service, method, values)
    elif method.startswith("history."):
        return _history(service, method, values)
    elif method == "model.test":
        return service.settings.start_model_test(values)
    elif method == "model.cancel":
        service.settings.cancel_model_test()
    elif method == "model.save":
        service.settings.save_verified_model(values)
        return service.prepare()
    elif method == "bank.build":
        return service.banks.start_build(values)
    elif method == "bank.cancel":
        service.banks.cancel_build()
    elif method == "bank.inventory":
        from bagu_trainer.infrastructure.bank_build.service import load_sources
        from bagu_trainer.infrastructure.bank_inventory import BankInventory
        return public_value(BankInventory(config.USER_DATA_ROOT).scan(load_sources()))
    elif method in {"bank.plan", "bank.clean"}:
        return _bank_cleanup(service, method, values)
    elif method == "settings.save":
        service.settings.save_preferences(values,
            learning=service.monitor.engine.learning if service.monitor.engine is not None else None)
    elif method in {"startup.status", "startup.set"}:
        return _startup(method, values)
    elif method == "uninstall.preview":
        from bagu_trainer.desktop.cleanup_cli import preview
        return {"detail": preview()}
    elif method == "uninstall.launch":
        from bagu_trainer.desktop.cleanup_cli import launch_uninstall
        owner_pid = os.environ.get("BAGU_ELECTRON_OWNER_PID")
        launch_uninstall(wait_pid=int(owner_pid) if owner_pid else None)
        return {"ok": True}
    elif method == "shutdown":
        service.close()
        return {"ok": True}
    service.publish()
    return {"ok": True}


def _practice(service, method, values):
    service._require_ready()
    if method == "practice.submit":
        if values.get("answer") not in tuple("ABCD"):
            raise ValueError("请选择 A–D 中的一个答案。")
        result = service.monitor.engine.learning.submit_answer(
            str(values.get("quiz_id", "")), values["answer"])
        service.publish()
        return result
    if method == "practice.enter":
        quiz = service.monitor.enter_quiz(str(values.get("quiz_id", "")))
    elif method == "practice.next":
        pending = service.monitor.list_pending()
        target = next((item for item in pending if item["quiz_id"] != values.get("quiz_id")), None)
        quiz = service.monitor.enter_quiz(target["quiz_id"]) if target else None
    else:
        quiz = service.monitor.resume_quiz()
    service.publish()
    return {"ok": True, "quiz": quiz}


def _history(service, method, values):
    if method == "history.list":
        filters = dict(page=values.get("page", 0), result=values.get("result", "all"),
                       project=values.get("project"), keyword=str(values.get("keyword", "")))
        if service.ready:
            return service.monitor.engine.learning.history(**filters)
        return service.history.get_answer_history(**filters)
    if method == "history.detail":
        if service.ready:
            return service.monitor.engine.learning.history_detail(int(values["attempt_id"]))
        return service.history.get_answer_detail(int(values["attempt_id"]))
    service._require_ready()
    result = service.monitor.engine.learning.repractice_history(int(values["attempt_id"]))
    service.publish()
    return result


def _bank_cleanup(service, method, values):
    if method == "bank.plan":
        plan = service.maintenance.plan(values.get("kind", "sources"), values.get("repositories") or [])
        return {"token": plan.token, "kind": plan.kind, "repositories": plan.repositories,
                "estimated_bytes": plan.estimated_bytes, "last_source": plan.last_source,
                "learning": getattr(plan, "learning", {}),
                "targets": [item.label for item in plan.targets]}
    result = service.maintenance.clean(str(values.get("token", "")),
                                       include_unknown=bool(values.get("include_unknown")))
    service.prepare()
    return {"ok": not result.failures, "note": result.message}


def _startup(method, values):
    from bagu_trainer.infrastructure import login_startup
    from bagu_trainer.infrastructure import paths
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
