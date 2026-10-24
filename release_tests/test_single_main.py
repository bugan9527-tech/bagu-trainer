"""One native Tk root, real DesktopApp, real engine, synthetic local bank."""
import importlib
import json
import threading
import time

from bagu_trainer import config, secrets
from bagu_trainer.bank_storage import atomic_json
from bagu_trainer.desktop import DesktopApp
from bagu_trainer.desktop_monitor import DesktopMonitor
from bagu_trainer.startup import StartupCoordinator


def test_first_partial_failed_engine_complete_and_all_pages_share_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    monkeypatch.delenv("BAGU_LLM_API_KEY", raising=False)
    coordinator = StartupCoordinator()
    monitor = DesktopMonitor(None)
    app = DesktopApp(monitor, events=coordinator.events, coordinator=coordinator)
    coordinator.monitor = monitor
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    ui_thread = threading.get_ident()
    original_set = app.tk.Variable.set
    def checked_set(variable, value):
        assert threading.get_ident() == ui_thread
        return original_set(variable, value)
    monkeypatch.setattr(app.tk.Variable, "set", checked_set)
    def wait(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            app.root.update()
            time.sleep(.01)
        assert predicate(), app.status.get()
        assert not errors
    try:
        app._start_worker()
        wait(lambda: coordinator.report.checks and "可浏览" in app.status.get())
        assert not app.ready and app.shell.current_page == "dashboard"
        assert len(app.shell.buttons) == 7
        assert app.resume_button.instate(["disabled"])
        for page, _ in app.shell.PAGES:
            app._navigate_page(page)
            app.root.update()
            assert app.shell.current_page == page
        assert not [w for w in app.root.winfo_children() if isinstance(w, app.tk.Toplevel)]
        data = config.USER_DATA_ROOT / "data"
        atomic_json(data / "questions.json", [{"id": "fixture-id", "knowledge_point": "数据库索引", "question": "数据库索引如何缩小扫描？",
            "tags": ["数据库"], "keywords": ["索引"], "difficulty": "easy"}])
        app.jobs.put(("prepare", None))
        wait(lambda: coordinator.report.checks[0].ready)
        assert not app.ready
        project = tmp_path / "synthetic-project"
        project.mkdir()
        atomic_json(config.CONFIG_PATH, {"project_roots": [str(project)], "llm": {
            "base_url": "http://127.0.0.1:9", "model": "synthetic-model", "timeout_sec": 1}})
        monkeypatch.setenv("BAGU_LLM_API_KEY", "synthetic-key-only")
        coordinator.engine_loader = lambda: (_ for _ in ()).throw(RuntimeError("injected engine failure"))
        app.jobs.put(("prepare", None))
        wait(lambda: coordinator.report.error)
        assert not app.ready and app.root.winfo_exists()
        coordinator.engine_loader = lambda: importlib.import_module("bagu_trainer.engine")
        app.jobs.put(("prepare", None))
        wait(lambda: app.ready)
        assert monitor.engine.__name__ == "bagu_trainer.engine"
        assert coordinator.window_command("_navigate", {"page": "model"})["ok"]
        wait(lambda: app.shell.current_page == "model")
        assert not coordinator.window_command("_navigate", {"page": "execute-anything"})["ok"]
        assert not [w for w in app.root.winfo_children() if isinstance(w, app.tk.Toplevel)]
    finally:
        app._close()
        deadline = time.monotonic() + 8
        while not app.closed and time.monotonic() < deadline:
            app.root.update()
            time.sleep(.01)
        assert app.closed
        if monitor.engine is not None and monitor.engine._store is not None:
            monitor.engine._store.close()
            monitor.engine._store = None
