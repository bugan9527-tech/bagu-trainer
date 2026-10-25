"""Exercise session transitions through the same operations as the desktop.

All generation, credentials, configuration and SQLite are synthetic (conftest).
No assertions require delivery dictionaries, a connection, or the session lock.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bagu_trainer import engine
from bagu_trainer.desktop_monitor import DesktopMonitor


def generate(text="redis changed"):
    _, quizzes, _, failures = engine.generate_activity(
        str(engine.PROJECT_ROOT), {"context": text, "sources": ["synthetic edit"]})
    assert len(quizzes) == 1 and not failures
    return quizzes[0]["quiz_id"]


def test_list_select_submit_and_duplicate_use_one_persisted_transition(env):
    qid = generate()
    session = engine.session
    assert session.list_pending()[0]["status"] == "ready"
    # Public snapshots cannot mutate the delivery state or expose the answer.
    public = session.list_pending()[0]
    assert not {"answer", "analysis"} & public.keys()
    public["options"]["A"] = "tampered"
    assert session.list_pending()[0]["options"]["A"] == "a"
    assert session.enter_quiz(qid)["quiz_id"] == qid
    assert session.list_pending()[0]["status"] == "delivered"
    assert session.take_quiz() is None
    assert engine.submit_answer(qid, "A")["ok"]
    assert not engine.submit_answer(qid, "B")["ok"]
    assert not session.list_pending() and session.enter_quiz(qid) is None
    assert engine.get_learning_stats()["total_attempts"] == 1


def test_close_reopen_rehydrates_pending_and_keeps_duplicate_submission_guard(env, monkeypatch):
    answered = generate()
    assert engine.submit_answer(answered, "A")["ok"]
    monkeypatch.setattr(env["matcher"], "KP", {**env["matcher"].KP, "id": "another-kp"})
    retained = generate("redis another concept")
    old = engine.session
    old.close()
    old.close()
    with pytest.raises(RuntimeError, match="关闭"):
        old.list_pending()
    engine.initialize()
    try:
        assert engine.session is not old
        assert engine.session.list_pending()[0]["quiz_id"] == retained
        assert engine.session.resume_quiz()["quiz_id"] == retained
        assert not engine.submit_answer(answered, "A")["ok"]
        assert engine.get_answer_history()["total"] == 1
    finally:
        engine.session.close()


def test_independent_sessions_and_restart_do_not_share_delivery_state(env, tmp_path):
    qid = generate()
    first = engine.session
    empty = engine.EngineSession(database=str(tmp_path / "another.db"))
    restarted = engine.EngineSession(database=first.database_path)
    first.close()
    try:
        assert empty.list_pending() == []
        assert restarted.list_pending()[0]["quiz_id"] == qid
        assert restarted.enter_quiz(qid)["quiz_id"] == qid
        assert restarted.list_pending()[0]["status"] == "delivered"
        assert empty.list_pending() == []
    finally:
        empty.close()
        restarted.close()


def test_concurrent_automatic_delivery_only_publishes_once(env):
    qid = generate()
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: engine.session.take_quiz(), range(8)))
    assert [quiz["quiz_id"] for quiz in results if quiz] == [qid]
    assert engine.session.list_pending()[0]["status"] == "delivered"


def test_failed_hydration_releases_connection_and_retry_restores_one_queue(env, monkeypatch):
    qid = generate()
    database = engine.session.database_path
    engine.session.close()
    engine.initialize()
    original = engine.Store.load_pending

    def interrupted(store, now):
        yield from original(store, now)
        raise OSError("synthetic interrupted hydration")

    with monkeypatch.context() as temporary:
        temporary.setattr(engine.Store, "load_pending", interrupted)
        with pytest.raises(OSError, match="interrupted hydration"):
            engine.session.list_pending()
    # Windows refuses this rename if the failed hydration kept SQLite open.
    from pathlib import Path
    moved = Path(str(database) + ".recovery-check")
    Path(database).rename(moved)
    moved.rename(database)
    try:
        assert [q["quiz_id"] for q in engine.session.list_pending()] == [qid]
        assert engine.session.enter_quiz(qid)["quiz_id"] == qid
        assert engine.session.list_pending()[0]["status"] == "delivered"
    finally:
        engine.session.close()


def test_pause_keeps_explicit_practice_and_submit_available(env):
    qid = generate()
    monitor = DesktopMonitor(engine)
    monitor.paused = True
    monitor.delivery_paused.set()
    assert monitor.poll()["state"] == "paused"
    assert monitor.take_quiz() is None
    assert monitor.list_pending()[0]["status"] == "ready"
    assert monitor.resume_quiz()["quiz_id"] == qid
    assert engine.submit_answer(qid, "A")["ok"]
    assert monitor.list_pending() == [] and env["gen"].calls == 1


@pytest.mark.parametrize("transition", ["reload", "invalidate", "close"])
def test_late_generation_cannot_publish_after_lifecycle_transition(env, monkeypatch, transition):
    retained = generate()
    question = dict(env["matcher"].KP)
    bank = SimpleNamespace(get=lambda _: question, all=lambda: [question])
    monkeypatch.setattr(engine, "kb", bank)
    monkeypatch.setattr(engine, "KnowledgeBase", lambda: bank)
    monkeypatch.setattr(engine, "Matcher", lambda _: env["matcher"])
    entered, release = threading.Event(), threading.Event()
    original = env["gen"].generate

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(env["gen"], "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(engine.create_quiz, question["id"], "synthetic context")
        try:
            assert entered.wait(3)
            if transition == "reload":
                engine.reload_bank()
            elif transition == "invalidate":
                engine.invalidate_content()
            else:
                engine.session.close()
                engine.initialize()  # replacement identity must also reject the old result
        finally:
            release.set()
        assert not result.result(timeout=3)["ok"]
    if transition == "close":
        engine.initialize()
    try:
        assert [q["quiz_id"] for q in engine.session.list_pending()] == [retained]
    finally:
        if transition == "close":
            engine.session.close()
