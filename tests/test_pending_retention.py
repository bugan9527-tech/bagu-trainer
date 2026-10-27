"""Pending work survives overnight and an application upgrade/restart."""
import pytest

from bagu_trainer import config, engine


@pytest.mark.parametrize("restart", [False, True])
def test_default_pending_survives_overnight_and_remains_answerable(env, monkeypatch, restart):
    env["caps"]["quiz_ttl_seconds"] = config.DEFAULTS["quiz_ttl_seconds"]
    clock = [1_790_000_000.0]
    monkeypatch.setattr(engine, "_now", lambda: clock[0])
    _, generated, _, failures = engine.generate_activity(
        str(engine.PROJECT_ROOT), {"context": "redis changed", "sources": ["synthetic edit"]})
    assert not failures and len(generated) == 1
    qid = generated[0]["quiz_id"]
    assert [q["quiz_id"] for q in engine.session.list_pending()] == [qid]
    clock[0] += 2 * 86400
    if restart:
        engine.session.close()
        engine.initialize()
    try:
        assert [q["quiz_id"] for q in engine.session.list_pending()] == [qid]
        from bagu_trainer.history_service import LearningHistory
        monkeypatch.setattr(config, "read_user_config", lambda: {
            "db_path": str(engine.session.database_path), "quiz_ttl_seconds": 0})
        assert LearningHistory().dashboard()["stats"]["pending"] == 1
        assert engine.session.enter_quiz(qid)["quiz_id"] == qid
        assert engine.submit_answer(qid, "A")["ok"]
        assert engine.get_answer_history()["total"] == 1
    finally:
        if restart:
            engine.session.close()


@pytest.mark.parametrize("delivered", [False, True])
def test_upgrade_retains_old_hourly_pending_but_does_not_revive_retired_rows(env, monkeypatch, delivered):
    clock = [1_790_000_000.0]
    monkeypatch.setattr(engine, "_now", lambda: clock[0])
    _, generated, _, _ = engine.generate_activity(
        str(engine.PROJECT_ROOT), {"context": "redis changed", "sources": ["synthetic edit"]})
    qid = generated[0]["quiz_id"]
    if delivered:
        engine.session.enter_quiz(qid)
    store = engine.session._get_store_locked()
    snapshot = store.get_quiz(qid)["snapshot"]
    store.create_quiz("retired", "test-kp-1", snapshot, "expired",
                      str(engine.PROJECT_ROOT), "fixture", clock[0], clock[0] + 3600)
    invalid = dict(snapshot, generated_by="offline")
    store.create_quiz("invalid", "test-kp-1", invalid, "ready",
                      str(engine.PROJECT_ROOT), "fixture", clock[0], clock[0] + 3600)
    store.create_quiz("other-project", "test-kp-1", snapshot, "ready",
                      str(engine.PROJECT_ROOT / "unwatched"), "fixture", clock[0], clock[0] + 3600)
    engine.session.close()
    env["caps"]["quiz_ttl_seconds"] = config.DEFAULTS["quiz_ttl_seconds"]
    clock[0] += 2 * 86400
    engine.initialize()
    try:
        assert [q["quiz_id"] for q in engine.session.list_pending()] == [qid]
        assert engine.session.enter_quiz("other-project") is None
        assert engine.session.enter_quiz("invalid") is None
        assert engine.session.enter_quiz("retired") is None
        assert engine.submit_answer(qid, "A")["ok"]
    finally:
        engine.session.close()


def test_upgrade_reuses_unfinished_historical_retry_after_overnight(env, monkeypatch):
    clock = [1_790_000_000.0]
    monkeypatch.setattr(engine, "_now", lambda: clock[0])
    _, generated, _, _ = engine.generate_activity(
        str(engine.PROJECT_ROOT), {"context": "redis changed", "sources": ["synthetic edit"]})
    assert engine.submit_answer(generated[0]["quiz_id"], "A")["ok"]
    attempt = engine.get_answer_history()["items"][0]["id"]
    retry = engine.repractice_attempt(attempt)["quiz"]["quiz_id"]
    engine.session.close()
    clock[0] += 2 * 86400
    env["caps"]["quiz_ttl_seconds"] = config.DEFAULTS["quiz_ttl_seconds"]
    engine.initialize()
    try:
        assert engine.repractice_attempt(attempt)["quiz"]["quiz_id"] == retry
        assert [q["quiz_id"] for q in engine.session.list_pending()] == [retry]
        assert engine.get_answer_history()["total"] == 1
    finally:
        engine.session.close()


def test_explicit_ttl_still_expires_pending_after_restart(env, monkeypatch):
    clock = [1_790_000_000.0]
    monkeypatch.setattr(engine, "_now", lambda: clock[0])
    _, generated, _, _ = engine.generate_activity(
        str(engine.PROJECT_ROOT), {"context": "redis changed", "sources": ["synthetic edit"]})
    qid = generated[0]["quiz_id"]
    engine.session.close()
    clock[0] += 86400
    engine.initialize()
    try:
        assert engine.session.list_pending() == []
        assert not engine.submit_answer(qid, "A")["ok"]
    finally:
        engine.session.close()
