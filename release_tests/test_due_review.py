"""Due reviews through SQLite, the scheduler and desktop practice entry points."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from bagu_trainer.desktop import DesktopApp
from bagu_trainer.desktop_monitor import DesktopMonitor
from bagu_trainer.quiz_validation import grounding_receipt
from release_tests.test_answer_history import history, SNAP, NOW, pump, dispose
from release_tests.test_quiz_grounding import CANDIDATE, REVIEW, CONTEXT, SOURCE


@pytest.fixture
def due_review(history, monkeypatch):
    engine, store, calls = history
    cfg = engine.load_config()
    monkeypatch.setattr(engine, "load_config", lambda: {**cfg, "max_pending_quizzes": 20, "max_quizzes": 40})
    clock = [NOW]
    monkeypatch.setattr(engine, "_now", lambda: clock[0])
    root = engine._normalize_project_root(engine.load_config()["project_roots"][0])
    # A historical AI response is already delivered. Grade through the real
    # engine so the real scheduler persists the next review time in SQLite.
    snapshot = {**SNAP, "grounding": grounding_receipt(CANDIDATE["grounding"], REVIEW, CONTEXT, SOURCE, "fixture")}
    store.create_quiz("original", SNAP["kp_id"], snapshot, "delivered", root, "", NOW, NOW + 3600)
    engine._hydrate_pending_locked(store)
    grade = engine.submit_answer("original", "A")
    assert grade["ok"]
    clock[0] = grade["schedule"]["next_review_at"] + 1
    monitor = DesktopMonitor(engine)
    monkeypatch.setattr(engine, "_collect_context", lambda *_: pytest.fail("explicit review scanned project context"))
    assert engine.get_learning_stats()["due_reviews"] == 1
    assert monitor.list_pending() == []
    return engine, store, calls, monitor, clock, root


def pending(store, engine, quiz_id, root, now, *, status="ready"):
    store.create_quiz(quiz_id, SNAP["kp_id"], {**SNAP, "review": True}, status, root, "", now, now + 3600)
    engine._hydrate_pending_locked(store)


def test_continue_practice_delivers_due_ai_snapshot_without_new_activity(due_review):
    engine, store, calls, monitor, _, root = due_review
    quiz = monitor.resume_quiz()
    assert quiz is not None, "desktop continue-practice did not deliver an available due review"
    assert quiz["review"] is True and quiz["project_root"] == root
    assert quiz["question"] == SNAP["question"]
    assert "answer" not in quiz and "analysis" not in quiz
    assert store.get_quiz(quiz["quiz_id"])["status"] == "delivered"
    assert not calls


def test_engine_next_quiz_reuses_due_ai_snapshot_with_empty_context(due_review, monkeypatch):
    engine, _, calls, _, _, root = due_review
    monkeypatch.setattr(engine, "_collect_context", lambda *_: {"context": "", "project_root": root})
    result = engine.next_quiz(root)
    assert result["ok"] and result["has_pending"]
    assert result["quiz"]["review"] is True
    assert result["quiz"]["question"] == SNAP["question"]
    assert not calls


@pytest.mark.parametrize("paused", [False, True])
def test_real_desktop_continue_button_delivers_due_review(due_review, monkeypatch, paused):
    _, store, calls, monitor, _, _ = due_review
    # Keep files unchanged; exercise the real UI button, worker and event drain.
    monitor.paused = paused
    monkeypatch.setattr(monitor, "poll", lambda: {"state": "paused" if monitor.paused else "watching", "projects": 1})
    app = DesktopApp(monitor)
    app.root.withdraw()
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    app._start_worker()
    try:
        pump(app, lambda: app.learning_values["reviews"].get() == "1")
        assert not app._pending_items and app.practice.current is None
        app.resume_button.invoke()
        assert "resume" in app.pending_actions
        pump(app, lambda: "resume" not in app.pending_actions, timeout=6)
        assert app.shell.current_page == "practice"
        assert app.practice.current is not None, "continue button completed with an empty current question"
        assert app.practice.current["review"] is True
        qid = app.practice.quiz_id
        assert store.get_quiz(qid)["status"] == "delivered"
        app.resume_button.invoke()
        assert app.practice.quiz_id == qid and "resume" not in app.pending_actions
        app.practice.choose("A")
        assert app.practice.submit()
        pump(app, lambda: app.practice.submit_state == "finished", timeout=6)
        assert store.attempt_count(qid) == 1
        assert monitor.paused == paused
        assert not calls and not errors
    finally:
        dispose(app)


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("gate", [False, True])
def test_explicit_review_works_with_monitoring_and_popup_pauses(due_review, paused, gate):
    _, _, calls, monitor, _, _ = due_review
    monitor.paused = paused
    if gate:
        monitor.delivery_paused.set()
    assert monitor.take_quiz() is None
    quiz = monitor.resume_quiz()
    assert quiz and quiz["review"]
    assert monitor.take_quiz() is None
    assert monitor.paused == paused and monitor.delivery_paused.is_set() == gate
    assert not calls


def test_repeated_and_concurrent_resume_reuse_one_delivered_review(due_review):
    engine, store, calls, monitor, clock, _ = due_review
    with ThreadPoolExecutor(max_workers=4) as workers:
        quizzes = list(workers.map(lambda _: monitor.resume_quiz(), range(8)))
    qids = {quiz["quiz_id"] for quiz in quizzes}
    assert len(qids) == 1
    qid = qids.pop()
    assert monitor.resume_quiz()["quiz_id"] == qid
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 2
    assert store.get_quiz(qid)["delivered_at"] == clock[0]
    assert engine.submit_answer(qid, "A")["ok"]
    assert not engine.submit_answer(qid, "A")["ok"]
    assert store.attempt_count(qid) == 1
    assert monitor.resume_quiz() is None
    assert engine.get_learning_stats()["due_reviews"] == 0
    assert not calls


def test_not_due_does_not_create_a_review(due_review):
    _, store, calls, monitor, clock, _ = due_review
    clock[0] = NOW + 10
    assert monitor.resume_quiz() is None
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 1
    assert not calls


@pytest.mark.parametrize("snapshot", [{**SNAP, "generated_by": "offline"}, {**SNAP, "generated_by": None}])
def test_non_ai_historical_snapshot_is_not_delivered(due_review, snapshot):
    _, store, calls, monitor, _, _ = due_review
    with store._conn:
        store._conn.execute("UPDATE quizzes SET snapshot = ? WHERE quiz_id = 'original'", (json.dumps(snapshot),))
    assert monitor.resume_quiz() is None
    assert store.attempt_count("original") == 1
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 1
    assert not calls


def test_removed_project_pending_is_not_moved_but_global_history_can_be_reused(due_review, monkeypatch, tmp_path):
    engine, store, calls, monitor, clock, old_root = due_review
    pending(store, engine, "outside-pending", old_root, clock[0])
    new_root = tmp_path / "new-project"
    new_root.mkdir()
    new_root = engine._normalize_project_root(str(new_root))
    cfg = engine.load_config()
    monkeypatch.setattr(engine, "load_config", lambda: {**cfg, "project_roots": [new_root]})
    quiz = monitor.resume_quiz()
    assert quiz["quiz_id"] != "outside-pending" and quiz["review"]
    assert quiz["project_root"] == new_root
    assert store.get_quiz("outside-pending")["status"] == "ready"
    assert store.get_quiz("outside-pending")["project_root"] == old_root
    assert not calls


@pytest.mark.parametrize("status", ["ready", "delivered"])
def test_existing_pending_in_any_selected_project_precedes_new_review(due_review, monkeypatch, tmp_path, status):
    engine, store, calls, monitor, clock, root = due_review
    other = tmp_path / "other-project"
    other.mkdir()
    other = engine._normalize_project_root(str(other))
    cfg = engine.load_config()
    monkeypatch.setattr(engine, "load_config", lambda: {**cfg, "project_roots": [root, other]})
    pending(store, engine, "existing", other, clock[0], status=status)
    assert monitor.resume_quiz()["quiz_id"] == "existing"
    assert store.get_quiz("existing")["status"] == "delivered"
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 2
    assert not calls


def test_manual_history_repractice_retains_scope_exception_and_identity(due_review, monkeypatch, tmp_path):
    engine, store, calls, monitor, _, _ = due_review
    attempt = store._conn.execute("SELECT id FROM attempts WHERE quiz_id = 'original'").fetchone()[0]
    manual = engine.repractice_attempt(attempt)["quiz"]
    other = tmp_path / "current-project"
    other.mkdir()
    cfg = engine.load_config()
    monkeypatch.setattr(engine, "load_config", lambda: {**cfg, "project_roots": [str(other)]})
    resumed = monitor.resume_quiz()
    assert resumed["quiz_id"] == manual["quiz_id"] and resumed["manual_review"]
    assert engine.repractice_attempt(attempt)["quiz"]["quiz_id"] == manual["quiz_id"]
    assert engine.submit_answer(resumed["quiz_id"], "A")["ok"]
    assert store.attempt_count(resumed["quiz_id"]) == 1
    assert not calls


def test_due_review_drops_manual_retry_identity_from_history(due_review):
    engine, store, calls, monitor, clock, _ = due_review
    attempt = store._conn.execute("SELECT id FROM attempts WHERE quiz_id = 'original'").fetchone()[0]
    manual = engine.repractice_attempt(attempt)["quiz"]
    grade = engine.submit_answer(manual["quiz_id"], "A")
    assert grade["ok"]
    clock[0] = grade["schedule"]["next_review_at"] + 1
    quiz = monitor.resume_quiz()
    assert quiz["review"] and not quiz.get("manual_review")
    snap = store.get_quiz(quiz["quiz_id"])["snapshot"]
    assert not {"manual_review", "retry_of_attempt_id", "retry_of_quiz_id"} & snap.keys()
    assert not calls


def test_restart_rehydrates_existing_due_review_without_duplicate(due_review, monkeypatch):
    engine, store, calls, monitor, _, _ = due_review
    quiz = monitor.resume_quiz()
    database = store._conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(engine, "_store", None)
    monkeypatch.setattr(engine, "_db_path", lambda: database)
    engine._state["quizzes"].clear()
    engine._state["pending"].clear()
    try:
        resumed = DesktopMonitor(engine).resume_quiz()
        assert resumed["quiz_id"] == quiz["quiz_id"]
        assert engine._store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 2
        assert not calls
    finally:
        engine._store.close()


@pytest.mark.parametrize("missing", [False, True])
def test_due_review_respects_missing_projects_and_empty_bank(due_review, monkeypatch, tmp_path, missing):
    engine, store, calls, monitor, _, _ = due_review
    if missing:
        cfg = engine.load_config()
        monkeypatch.setattr(engine, "load_config", lambda: {**cfg, "project_roots": [str(tmp_path / "missing")]})
    else:
        monkeypatch.setattr(engine.kb, "explicit_empty", True, raising=False)
    assert monitor.resume_quiz() is None
    assert store._conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 1
    assert not calls
