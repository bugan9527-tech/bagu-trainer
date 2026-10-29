"""Paused monitoring keeps explicit practice available; isolated SQLite only."""
import sys

import pytest

from bagu_trainer.application.monitor import DesktopMonitor
from release_tests.test_answer_history import history, seed, SNAP, NOW, pump, dispose


@pytest.fixture
def pending(history, monkeypatch):
    engine, store, calls = history
    cfg = engine.load_config()
    monkeypatch.setattr(engine, "load_config", lambda: {
        **cfg, "max_pending_quizzes": 20, "max_quizzes": 40})
    root = engine._normalize_project_root(cfg["project_roots"][0])
    for qid, status in (("ready", "ready"), ("delivered", "delivered")):
        store.create_quiz(qid, "historic-kp", {**SNAP, "review": True}, status,
                          root, "", NOW, NOW + 3600)
    engine.session._hydrate_pending_locked(store)
    attempt = seed(store)  # historical project is outside the current scope
    manual = engine.repractice_attempt(attempt)["quiz"]["quiz_id"]
    return engine, store, calls, DesktopMonitor(engine), manual


@pytest.mark.parametrize("gate", [False, True])
def test_pause_retains_ready_delivered_manual_count(pending, gate):
    _, store, _, monitor, manual = pending
    before = monitor.list_pending()
    assert {q["quiz_id"] for q in before} == {"ready", "delivered", manual}
    monitor.paused = True
    if gate:
        monitor.delivery_paused.set()
    assert monitor.list_pending() == before
    assert monitor.pending_count() == 3
    assert store.get_quiz("ready")["status"] == "ready"  # listing never delivers
    monitor.paused = False
    assert monitor.list_pending() == before


@pytest.mark.parametrize("gate", [False, True])
@pytest.mark.parametrize("kind", ["ready", "delivered", "manual"])
def test_explicit_enter_and_submit_while_paused(pending, gate, kind):
    engine, store, calls, monitor, manual = pending
    monitor.paused = True
    if gate:
        monitor.delivery_paused.set()
    qid = manual if kind == "manual" else kind
    question = monitor.enter_quiz(qid)
    assert question and question["quiz_id"] == qid
    assert bool(question.get("manual_review")) == (kind == "manual")
    assert store.get_quiz(qid)["status"] == "delivered"
    assert monitor.take_quiz() is None
    grade = engine.submit_answer(qid, "A")
    assert grade["ok"] and grade["correct"] and grade["analysis"] == SNAP["analysis"]
    assert store.attempt_count(qid) == 1
    assert monitor.enter_quiz(qid) is None
    monitor.paused = False
    monitor.delivery_paused.clear()
    monitor.active = None
    delivered = []
    while (question := monitor.take_quiz()) is not None:
        delivered.append(question["quiz_id"])
        monitor.active = None
    assert qid not in delivered and len(delivered) == len(set(delivered))
    assert not engine.submit_answer(qid, "A")["ok"]
    assert store.attempt_count(qid) == 1 and not calls


@pytest.mark.parametrize("paused", [False, True])
def test_pause_preserves_expiry_scope_answered_and_ai_admission(pending, paused):
    engine, store, _, monitor, manual = pending
    root = engine.load_config()["project_roots"][0]
    for qid, snapshot, scope, expiry in (
        ("expired", {**SNAP, "review": True}, root, NOW - 1),
        ("outside", {**SNAP, "review": True}, root + "-outside", NOW + 3600),
        ("offline", {**SNAP, "review": True, "generated_by": "offline"}, root, NOW + 3600),
        ("unreviewed", SNAP, root, NOW + 3600),
    ):
        store.create_quiz(qid, "historic-kp", snapshot, "ready", scope, "", NOW - 10, expiry)
    engine.session._hydrate_pending_locked(store)
    # Also exercise cleanup of a once-valid resident quiz whose admission changed.
    engine.session._state["quizzes"]["ready"]["generated_by"] = "offline"
    monitor.paused = paused
    for qid in ("expired", "outside", "offline", "unreviewed", "original-1", "ready", "missing"):
        assert monitor.enter_quiz(qid) is None
    assert {q["quiz_id"] for q in monitor.list_pending()} == {"delivered", manual}
    assert monitor.enter_quiz(manual)["manual_review"]  # manual scope exception remains


def test_paused_poll_retry_and_automatic_delivery_do_no_work(pending, monkeypatch):
    engine, store, calls, monitor, _ = pending
    monitor.paused = True
    monitor.waiting["synthetic"] = (0, {"context": "cache"})
    monitor.failed_generations["synthetic"] = ({"context": "cache"}, ("historic-kp",))
    for method in ("observe_project", "generate_activity"):
        monkeypatch.setattr(engine, method, lambda *a, **kw: pytest.fail("paused automatic work ran"))
    monkeypatch.setattr(engine.session, "take_quiz", lambda: pytest.fail("paused delivery ran"))
    for _ in range(2):
        assert monitor.poll()["state"] == "paused"
        assert monitor.retry_generation()["state"] == "paused"
        assert monitor.take_quiz() is None
        assert monitor.pending_count() == 3
    assert store.get_quiz("ready")["status"] == "ready" and not calls


def make_app(engine, monkeypatch):
    from bagu_trainer.desktop import DesktopApp
    monitor = DesktopMonitor(engine)
    original_poll = monitor.poll
    # Resume tests must not observe real files. Paused cycles use the real poll.
    monkeypatch.setattr(monitor, "poll", lambda: original_poll() if monitor.paused else
                        {"state": "watching", "projects": 1})
    app = DesktopApp(monitor)
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    app.root.withdraw()
    return app, errors


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk practice")
def test_worker_pause_resume_preserves_ui_counts_selection_and_grade(pending, monkeypatch):
    engine, store, calls, _, _ = pending
    app, errors = make_app(engine, monkeypatch)
    app._show_quiz(app.monitor.enter_quiz("delivered"))
    app._navigate_page("practice")
    app.practice.choose("B")
    app._start_worker()
    try:
        pump(app, lambda: len(app._pending_items) == 3)
        page = app._page_frame("practice")
        def toggle(expected):
            app.pause_button.invoke()
            pump(app, lambda: app.monitor.paused == expected and not app.pending_actions, timeout=6)
            assert app.shell.badges["practice"] == 3
            assert app.learning_values["pending"].get() == "3"
            assert len(page.pending_list.get_children()) == 3
            assert page.summary.get().startswith("待答题 3")
            assert app.practice.quiz_id == "delivered" and app.answer.get() == "B"
        toggle(True)
        toggle(False)
        toggle(True)
        assert not app._answer_window().submit_button.instate(["disabled"])
        assert app.practice.submit()
        pump(app, lambda: app.practice.submit_state == "finished", timeout=6)
        result = app.feedback.get()
        assert SNAP["analysis"] in result and "回答错误" in result
        assert app.monitor.paused and store.attempt_count("delivered") == 1
        app.pause_button.invoke()
        pump(app, lambda: not app.monitor.paused and not app.pending_actions, timeout=6)
        assert app.practice.quiz_id == "delivered" and app.answer.get() == "B"
        assert app.practice.submit_state == "finished" and app.feedback.get() == result
        assert not app.practice.submit()
        assert store.attempt_count("delivered") == 1
        assert not calls and not errors
    finally:
        dispose(app)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk history")
def test_history_retry_enter_and_submit_remain_available_while_paused(history, monkeypatch):
    engine, store, calls = history
    seed(store)
    app, errors = make_app(engine, monkeypatch)
    app.monitor.paused = True
    app._start_worker()
    try:
        app._open_history()
        page = app.history_page
        pump(app, lambda: len(page.tree.get_children()) == 1)
        page.open_selected()
        pump(app, lambda: hasattr(page, "retry_button"))
        page.retry_button.invoke()
        pump(app, lambda: app.practice.manual and app.practice.current is not None, timeout=6)
        qid = app.practice.quiz_id
        assert app.monitor.paused and app.shell.current_page == "practice"
        # Enter explicitly through the actual pending-list button as well.
        pump(app, lambda: qid in app._page_frame("practice").pending_list.get_children())
        practice = app._page_frame("practice")
        practice.pending_list.selection_set(qid)
        app.root.update()
        practice.enter_button.invoke()
        pump(app, lambda: not app.practice.busy and not app.pending_actions, timeout=6)
        app.practice.choose("A")
        app._answer_window().submit_button.invoke()
        pump(app, lambda: app.practice.submit_state == "finished", timeout=6)
        assert SNAP["analysis"] in app.feedback.get()
        assert store.attempt_count(qid) == store.attempt_count("original-1") == 1
        app.pause_button.invoke()
        pump(app, lambda: not app.monitor.paused and not app.pending_actions, timeout=6)
        assert app.practice.quiz_id == qid and app.practice.submit_state == "finished"
        assert app.monitor.take_quiz() is None
        assert not engine.submit_answer(qid, "A")["ok"]
        assert store.attempt_count(qid) == 1 and not calls and not errors
    finally:
        dispose(app)
