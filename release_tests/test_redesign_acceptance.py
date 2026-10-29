"""Final lifecycle regressions; all inputs, credentials and writes are isolated."""
import queue
import threading
from types import SimpleNamespace

from bagu_trainer.desktop import DesktopApp
from release_tests.test_first_use import installation, wait_ui
from release_tests.test_dashboard_navigation import dashboard, dispose


def test_short_workspace_can_toggle_queue_without_hiding_reading_actions(installation, monkeypatch):
    from release_tests.test_interaction_states import quiz
    app, _ = dashboard(installation, monkeypatch)
    try:
        app._show_quiz(quiz("short-workspace"))
        app._navigate_page("practice")
        app.root.deiconify()
        app.root.geometry("560x560")
        app.root.update()
        page = app._page_frame("practice")
        assert page.pending_list.winfo_ismapped() and not hasattr(page, "submit_button")
        app._continue_practice()
        win = app.practice_window
        win.geometry("520x440")
        app.root.update()
        assert win.view.canvas.winfo_height() >= app.theme.px(96)
        assert win.footer.winfo_ismapped() and win.submit_button.winfo_ismapped()
    finally:
        dispose(app)


def test_project_apply_runs_on_data_only_worker_and_keeps_existing_baseline(tmp_path):
    from bagu_trainer.application.monitor import DesktopMonitor
    from bagu_trainer.application.projects import canonical_root
    from release_tests.fakes import LearningFake
    for name in ('kept', 'removed', 'new'):
        (tmp_path / name).mkdir()
    kept, removed, new = [canonical_root(tmp_path / name) for name in ('kept', 'removed', 'new')]
    events, jobs, stop = queue.Queue(), queue.Queue(), threading.Event()
    baseline = object()
    readers = {kept: baseline, removed: object()}
    engine = SimpleNamespace(_normalize_project_root=str,
                             session=SimpleNamespace(forget_projects=lambda roots: [readers.pop(root, None) for root in roots]),
                             PROJECT_ROOT=kept, load_config=lambda: {"project_roots": [kept, new]},
                             get_learning_stats=lambda: {"ok": False})
    engine.learning = LearningFake(engine)
    monitor = DesktopMonitor(engine)
    monitor.apply_projects([kept, removed])
    monitor.initialized = {kept, removed}
    monitor.observed = {kept: "fingerprint", removed: "old"}
    monitor.waiting = {kept: "change", removed: "old"}
    monitor.failed_generations = {removed: 1}
    def poll():
        stop.set()
        return {"state": "watching", "projects": 2}
    monitor.poll = poll
    context = SimpleNamespace(monitor=monitor, events=events, jobs=jobs, stop=stop, worker_done=threading.Event())
    jobs.put(("apply_projects", [kept, new]))
    DesktopApp._worker(context)
    assert monitor.initialized == {kept}
    assert monitor.waiting == {kept: "change"} and monitor.observed == {kept: "fingerprint"}
    assert readers == {kept: baseline} and not monitor.failed_generations
    assert not any("操作失败" in str(event) for event in list(events.queue))


def test_bank_reload_is_serial_worker_job_not_tk_work(installation, monkeypatch):
    app, _ = dashboard(installation, monkeypatch)
    calls = []
    app.monitor.engine.reload_bank = lambda: calls.append(threading.get_ident())
    app.monitor.list_pending = lambda: []
    app.tasks.bank.state = "built"
    try:
        app._drain()
        assert not calls, "UI thread reloaded the engine"
        assert app.jobs.qsize() == 1
        app._drain()
        assert app.jobs.qsize() == 1, "Repeated drains queued the same reload"
        app._start_worker()
        wait_ui(app, lambda: app.tasks.bank.state == "applied")
        assert len(calls) == 1 and calls[0] != threading.get_ident()
    finally:
        dispose(app)


def test_bank_reload_failure_does_not_break_tk_polling(installation, monkeypatch):
    app, _ = dashboard(installation, monkeypatch)
    def fail():
        raise OSError("private fixture error")
    app.monitor.engine.reload_bank = fail
    app.monitor.list_pending = lambda: []
    app.tasks.bank.state = "built"
    try:
        app._drain()
        app._start_worker()
        wait_ui(app, lambda: app.tasks.bank.state == "failed")
        assert "OSError" in app.tasks.bank.error and "private fixture" not in app.tasks.bank.error
        assert app.poll is not None and app.root.winfo_exists()
    finally:
        dispose(app)


def test_exit_waits_for_hidden_model_save_and_bank_worker(installation, monkeypatch):
    from tkinter import messagebox
    app, _ = dashboard(installation, monkeypatch)
    release_save, release_bank = threading.Event(), threading.Event()
    saved, bank_finished = threading.Event(), threading.Event()
    def save():
        assert release_save.wait(5)
        saved.set()
    def build_worker():
        assert release_bank.wait(5)
        bank_finished.set()
    try:
        app._navigate_page("model")
        panel = app._page_frame("model")
        panel._job("save", save, "isolated save")
        app._navigate_page("projects")
        app.tasks.bank.state = "building"
        app.tasks.bank.worker = threading.Thread(target=build_worker)
        app.tasks.bank.worker.start()
        monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
        app._close()
        assert app.closing and not app.closed and panel.closing
        assert app.tasks.bank.cancel.is_set()
        release_save.set()
        panel.worker.join(2)
        app._drain()
        assert saved.is_set() and not app.closed, "Bank worker was abandoned on exit"
        release_bank.set()
        app.tasks.bank.worker.join(2)
        wait_ui(app, lambda: app.closed)
        assert bank_finished.is_set() and not panel.worker.is_alive()
    finally:
        release_save.set()
        release_bank.set()
        if app.tasks.bank.worker:
            app.tasks.bank.worker.join(2)
        if not app.closed:
            dispose(app)


def test_real_descendant_key_events_are_scoped_to_visible_practice(installation, monkeypatch):
    from release_tests.test_interaction_states import quiz
    app, _ = dashboard(installation, monkeypatch)
    app.root.deiconify()
    try:
        app._show_quiz(quiz("keys"))
        app._navigate_page("practice")
        page = app._answer_window()
        page.show()
        app.root.update()
        page.view.canvas.focus_force()
        app.root.update()
        page.view.canvas.event_generate("<KeyPress-b>")
        app.root.update()
        assert app.answer.get() == "B"
        app._navigate_page("model")
        entry = app._page_frame("model").entries[0]
        entry.focus_force()
        app.root.update()
        entry.event_generate("<KeyPress-a>")
        app.root.update()
        assert app.answer.get() == "B" and app.practice.submit_state == "ready"
        app._navigate_page("practice")
        app.root.update()
        page.option_buttons[1].focus_force()
        app.root.update()
        page.option_buttons[1].event_generate("<Control-Return>")
        app.root.update()
        assert app.jobs.get_nowait() == ("submit", ("keys", "B"))
        page.option_buttons[1].event_generate("<Control-Return>")
        app.root.update()
        assert app.jobs.empty()
    finally:
        dispose(app)
