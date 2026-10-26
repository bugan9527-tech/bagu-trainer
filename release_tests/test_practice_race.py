"""Formal regression for review-20261023 race; synthetic quizzes, isolated data."""
import sys
import threading

import pytest

from release_tests.test_first_use import installation, wait_ui
from release_tests.test_dashboard_navigation import dashboard, dispose
from release_tests.test_interaction_states import quiz
from release_tests.test_answer_history import history, SNAP, NOW

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk practice")


@pytest.fixture
def practice_app(installation, monkeypatch):
    app, _ = dashboard(installation, monkeypatch)
    app._show_quiz(quiz("first"))
    app._pending_items = [quiz("first"), quiz("second")]
    app.monitor.list_pending = lambda: app._pending_items
    app.monitor.enter_quiz = lambda qid: next((q for q in app._pending_items if q["quiz_id"] == qid), None)
    app._navigate_page("practice")
    app.practice.choose("A")
    app.root.deiconify()
    app.root.update()
    page = app._page_frame("practice")
    page.pending_list.selection_set("second")
    app.root.update()
    try:
        yield app, page
    finally:
        dispose(app)


def invoke_list_binding(page, sequence):
    # Invoke the registered Tk binding even when the button is disabled. Tk
    # cannot synthesize Double modifiers with event_generate.
    from types import SimpleNamespace
    if sequence == "<Double-1>":
        selected = page.pending_list.selection()
        box = page.pending_list.bbox(selected[0]) if selected else ()
        if box:
            page._double_click(SimpleNamespace(x=box[0] + 30, y=box[1] + box[3] // 2))
    else:
        page._enter_selected()


def test_enter_queued_blocks_all_submission_and_switch_paths(practice_app):
    app, page = practice_app
    page.enter_button.invoke()
    assert app.practice.entering_quiz_id == "second"
    assert app.practice.submit_state == "ready"
    assert app._answer_window().submit_button.instate(["disabled"])
    app._answer_window().submit_button.invoke()
    app._answer_window().view.canvas.focus_force()
    app.root.update()
    app._answer_window().view.canvas.event_generate("<Control-Return>")
    app.root.update()
    app._answer_window()._submit()
    assert app.practice.submit() is False
    assert app._submit_quiz("first", "A") is False
    invoke_list_binding(page, "<Double-1>")
    invoke_list_binding(page, "<Return>")
    assert app._enter_pending("first") is False
    app.practice.enter(quiz("bypass"))
    app.practice.dismiss()
    assert app.practice.quiz_id == "first" and app.answer.get() == "A"
    name, request = app.jobs.get_nowait()
    assert name == "practice_open" and request["target"] == "second"
    assert app.jobs.empty()
    app.events.put(("practice_opened", dict(request=app._practice_request, quiz=quiz("second"), failed=False, note="")))
    app._drain()
    assert not app.practice.busy and app.practice.quiz_id == "second"
    app._start_worker()
    app.practice.dismiss()
    wait_ui(app, lambda: not app.practice.busy)
    assert app.practice.quiz_id == "first" and app.answer.get() == "A"
    assert app.practice.submit() is True


def test_submitting_blocks_enter_but_navigation_keeps_grade(practice_app):
    app, page = practice_app
    assert app.practice.submit()
    assert page.enter_button.instate(["disabled"])
    page.enter_button.invoke()
    invoke_list_binding(page, "<Double-1>")
    invoke_list_binding(page, "<Return>")
    page._enter_selected()
    assert app._enter_pending("second") is False
    app.practice.enter(quiz("second"))
    assert app.practice.quiz_id == "first"
    assert app.jobs.get_nowait() == ("submit", ("first", "A"))
    assert app.jobs.empty()
    app.shell.buttons["model"].invoke()
    assert app.shell.current_page == "model" and app.practice.busy
    app.events.put(("grade", {"quiz_id": "stale", "answer": "B", "grade": {"ok": False}}))
    app.events.put(("grade", {"quiz_id": "first", "answer": "A", "grade": {
        "ok": True, "correct": True, "correct_answer": "A", "analysis": "保留结果"}}))
    app._drain()
    app.shell.buttons["practice"].invoke()
    assert app.practice.quiz_id == "first" and app.practice.submit_state == "finished"
    assert "保留结果" in app.feedback.get() and not app.inflight_submissions
    assert not app.practice.submit()
    assert app._enter_pending("second")


@pytest.mark.parametrize("graded", [False, True])
@pytest.mark.parametrize("outcome", ["missing", "expired", "exception"])
def test_worker_enter_failure_restores_original_and_allows_retry(practice_app, history, graded, outcome):
    app, page = practice_app
    engine, store, _ = history
    from bagu_trainer.desktop_monitor import DesktopMonitor
    monitor = DesktopMonitor(engine)
    if outcome == "expired":
        store.create_quiz("second", "historic-kp", {**SNAP, "review": True}, "ready",
                          engine.load_config()["project_roots"][0], "", NOW - 100, NOW - 1)
    if graded:
        app._apply_grade("first", {"ok": True, "correct": True,
                         "correct_answer": "A", "analysis": "已有判分"}, "A")
    before = (app.practice.current, app.answer.get(), app.practice.grade,
              app.practice.submit_state, app.feedback.get(), app.answer_hint.get())
    entered, release = threading.Event(), threading.Event()
    def enter(_):
        entered.set()
        assert release.wait(3)
        if outcome == "exception":
            raise OSError("synthetic failure")
        return monitor.enter_quiz("second")
    app.monitor.enter_quiz = enter
    assert app._enter_pending("second")
    app._start_worker()
    try:
        assert entered.wait(2)
        assert not app.practice.submit()
        release.set()
        wait_ui(app, lambda: not app.practice.busy)
        assert before == (app.practice.current, app.answer.get(), app.practice.grade,
                          app.practice.submit_state, app.feedback.get(), app.answer_hint.get())
        app.monitor.enter_quiz = lambda _: quiz("second")
        assert app._enter_pending("second")
        wait_ui(app, lambda: app.practice.quiz_id == "second")
        assert app.practice.entering_quiz_id is None and not app._answer_window().submit_button.instate(["disabled"])
    finally:
        release.set()


def test_submit_guard_runs_before_state_or_callback(practice_app):
    app, _ = practice_app
    before = (app.practice.submit_state, app.feedback.get(), app.answer_hint.get())
    # The application callback cannot be used to enqueue outside controller state.
    assert app._submit_quiz("first", "A") is False
    app.inflight_submissions.add("first")
    assert app.practice.submit() is False
    assert before == (app.practice.submit_state, app.feedback.get(), app.answer_hint.get())
    assert app.jobs.empty()
    app.inflight_submissions.clear()
    app.practice.on_submit = lambda *_: False
    assert app.practice.submit() is False
    assert before == (app.practice.submit_state, app.feedback.get(), app.answer_hint.get())
    app.practice.on_submit = app._submit_quiz
    assert app.practice.submit()


def test_delivery_during_empty_switch_keeps_reservation(practice_app):
    app, _ = practice_app
    app.practice.clear()
    assert app._enter_pending("second")
    app._show_quiz(quiz("automatic"))
    app._show_quiz({**quiz("manual"), "manual_review": True}, manual=True)
    app.practice.choose("A")
    assert app.practice.entering_quiz_id == "second"
    assert not app.practice.submit()
    app.events.put(("practice_opened", dict(request=app._practice_request, quiz=quiz("second"), failed=False, note="")))
    app._drain()
    assert not app.practice.busy and app.practice.quiz_id == "second"
    assert [q["quiz_id"] for q in app.practice.deferred] == ["automatic", "manual"]


def test_real_worker_sqlite_submit_survives_navigation(practice_app, history):
    app, page = practice_app
    engine, store, model_calls = history
    store.create_quiz("first", "historic-kp", {**SNAP, "review": True}, "delivered", "", "", NOW, NOW + 3600)
    engine.session._hydrate_pending_locked(store)
    entered, release = threading.Event(), threading.Event()
    def submit(qid, answer):
        entered.set()
        assert release.wait(3)
        return engine.submit_answer(qid, answer)
    app.monitor.engine.submit_answer = submit
    assert app.practice.submit()
    app._start_worker()
    try:
        assert entered.wait(2)
        assert not app._enter_pending("second")
        app.shell.buttons["model"].invoke()
        release.set()
        wait_ui(app, lambda: app.practice.submit_state == "finished")
        app.shell.buttons["practice"].invoke()
        assert "回答正确" in app.feedback.get()
        assert app._answer_window().submit_button.instate(["disabled"])
        assert not app.practice.submit()
        assert not engine.submit_answer("first", "A")["ok"]
        assert store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert not model_calls
    finally:
        release.set()
        app.stop.set()
        app.worker_thread.join(3)
