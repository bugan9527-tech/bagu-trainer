"""Dashboard settings reuse its Tk window and return without a subprocess."""
import sys
import threading
from types import SimpleNamespace

import pytest

from bagu_trainer import config, onboarding, startup
from bagu_trainer.desktop import DesktopApp
from release_tests.test_first_use import (
    installation, model_http, seed_bank, verify_and_save, wait_ui, fill_model, QUESTION, KEY,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk navigation")


def dashboard(installation, monkeypatch):
    seed_bank(installation)
    reloads = []
    engine = SimpleNamespace(
        load_config=config.load_config, PROJECT_ROOT=installation.project,
        get_learning_stats=lambda: {"ok": True, "total_attempts": 0, "due_reviews": 0},
        reload_bank=lambda: reloads.append(1),
        open_config_window=lambda: pytest.fail("Settings opened another process"),
    )
    monitor = SimpleNamespace(
        engine=engine, paused=False, active=None, initialized=set(), waiting={},
        failed_generations={}, ui_state={}, poll=lambda: {"state": "watching", "projects": 1},
        take_quiz=lambda: None, list_pending=lambda: [],
    )
    app = DesktopApp(monitor)
    app.root.withdraw()
    # Configuration pages still read the real UI/service, using this fixture's
    # source manifest rather than any downloaded repositories on the machine.
    from bagu_trainer.bank_build.service import BankBuildService
    monkeypatch.setattr("bagu_trainer.bank_setup.BankBuildService",
                        lambda: BankBuildService(installation.project))
    return app, reloads


def dispose(app):
    app.stop.set()
    if app.worker_thread:
        app.worker_thread.join(3)
    app.root.destroy()


def test_bank_and_model_roundtrips_keep_the_same_window(installation, model_http, monkeypatch):
    verify_and_save(model_http)
    startup.set_projects([installation.watched])
    app, reloads = dashboard(installation, monkeypatch)
    original_root = app.root
    errors = []
    app.root.report_callback_exception = lambda *error: errors.append(error)
    app.monitor.initialized.add("baseline-kept")
    app._start_worker()
    try:
        for button, page_key in ((app.bank_button, "bank"), (app.configure_button, "model"),
                                 (app.bank_button, "bank")):
            button.invoke()
            app.root.update()
            # Real embedded panels: no FirstUseWizard, no Toplevel, worker alive.
            assert app.shell.current_page == page_key
            assert app.shell.current_frame is app._cached_pages[page_key]
            assert app.root is original_root
            assert not [w for w in app.root.winfo_children() if w.winfo_class() == "Toplevel"]
            assert app.worker_thread is not None and app.worker_thread.is_alive() and not app.stop.is_set()
            assert app.monitor.initialized == {"baseline-kept"}
            app.shell.buttons["dashboard"].invoke()
            app.root.update()
            assert app.shell.current_page == "dashboard"
        assert not reloads and not errors
        assert len(model_http.requests) == 1  # Opening pages sends no request.
        assert installation.database.read_bytes() == b"existing learning database"
    finally:
        dispose(app)


def test_model_save_returns_to_dashboard_without_project_steps(installation, model_http, monkeypatch):
    app, _ = dashboard(installation, monkeypatch)
    try:
        app.configure_button.invoke()
        app.root.update()
        panel = app._cached_pages["model"]
        panel.base_url.set(model_http.base_url)
        panel.model_name.set("local-model")
        panel.api_key.set(KEY)
        panel.timeout.set("1")
        panel._test()
        wait_ui(app, lambda: not panel.busy)
        assert panel.verified is not None
        panel._save()
        wait_ui(app, lambda: not panel.busy)
        assert onboarding.saved_model_verified()
        assert len(model_http.requests) == 1
    finally:
        dispose(app)


def test_bank_return_reloads_only_after_build_completion(installation, monkeypatch):
    from bagu_trainer.bank_build.service import BankBuildService
    app, reloads = dashboard(installation, monkeypatch)
    try:
        app.bank_button.invoke()
        app.root.update()
        panel = app._cached_pages["bank"]
        # A built bank reloads at a safe boundary (no generation/submit), then
        # is marked applied; the page distinguishes built vs applied.
        app.tasks.bank.result = SimpleNamespace(questions=1)
        app.tasks.bank.state = "built"
        app._drain()
        assert not reloads and app.tasks.bank.state == "built"
        app._start_worker()
        wait_ui(app, lambda: app.tasks.bank.state == "applied")
        assert reloads == [1]
        assert app.root.winfo_exists()
    finally:
        dispose(app)


def test_navigation_does_not_wait_for_current_poll(installation, monkeypatch):
    app, _ = dashboard(installation, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def poll():
        entered.set()
        assert release.wait(3)
        return {"state": "watching", "projects": 1}
    app.monitor.poll = poll
    app._start_worker()
    try:
        assert entered.wait(2)
        app.bank_button.invoke()
        app.root.update()
        # Page switches no longer stop the monitor or wait for a poll to finish.
        assert app.shell.current_page == "bank" and app.shell.current_frame is app._cached_pages["bank"]
        assert app.worker_thread.is_alive() and not app.stop.is_set()
        release.set()
        wait_ui(app, lambda: app.shell.current_page == "bank")
    finally:
        release.set()
        dispose(app)


def test_engine_reloads_active_bank_without_replacing_learning_state(installation, monkeypatch):
    seed_bank(installation)
    from bagu_trainer import engine
    old_kb, old_matcher = engine.kb, engine.matcher
    monkeypatch.setattr(engine, "kb", old_kb)
    monkeypatch.setattr(engine, "matcher", old_matcher)
    history = engine._state
    pending = history["pending"]
    store = engine._store
    engine.reload_bank()
    assert engine.kb is not old_kb and engine.matcher is not old_matcher
    assert engine.kb.get(QUESTION["id"]) == QUESTION
    assert engine.matcher.kb is engine.kb
    assert engine._state is history and engine._state["pending"] is pending
    assert engine._store is store
