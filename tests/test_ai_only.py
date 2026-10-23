"""AI-only delivery policy across generation, restart, desktop and reviews."""
from types import SimpleNamespace

import pytest

from bagu_trainer import engine
from bagu_trainer.desktop_monitor import DesktopMonitor
from bagu_trainer.scheduler import next_state


def snapshot(origin):
    quiz = {
        "kp_id": "test-kp-1", "kp_name": "缓存", "difficulty": "easy",
        "question": "如何避免缓存穿透？",
        "options": {"A": "空值缓存", "B": "删除缓存", "C": "忽略请求", "D": "关闭数据库"},
        "answer": "A", "analysis": "短期缓存空结果可以减少重复查询。",
    }
    if origin is not None:
        quiz["generated_by"] = origin
    return quiz


@pytest.mark.parametrize("origin", ["fallback", "manual", None])
def test_non_ai_generator_result_never_reaches_pending(env, monkeypatch, origin):
    monkeypatch.setattr(engine, "generator", SimpleNamespace(generate=lambda *_: snapshot(origin)))
    env["diff"].results = [{"source": "git", "files": ["cache.py"], "diff_text": "redis cache change"}]
    result = engine.process_activity()
    assert result["quizzes"] == []
    assert "仅允许展示 AI" in result["unavailable"][0]["reason"]
    assert engine._state["pending"] == []
    assert engine._store._conn.execute("SELECT count(*) FROM quizzes").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["ready", "delivered"])
@pytest.mark.parametrize("origin", ["fallback", None, "llm"])
def test_old_non_ai_pending_is_retired_on_restart(env, monkeypatch, status, origin):
    monkeypatch.setattr(engine, "_now", lambda: 1000)
    with engine._STATE_LOCK:
        store = engine._get_store_locked()
        store.create_quiz("old", "test-kp-1", snapshot(origin), status,
                          str(engine.PROJECT_ROOT), "old data", 900, 2000)
        store.close()
        engine._store = None
    monitor = DesktopMonitor(engine)
    assert monitor.take_quiz() is None
    assert monitor.resume_quiz() is None
    assert engine.next_quiz()["has_pending"] is False
    assert engine.submit_answer("old", "A")["ok"] is False
    row = engine._store.get_quiz("old")
    assert row["status"] == "expired"
    assert row["snapshot"] == snapshot(origin)  # Preserve content; do not delete history.


@pytest.mark.parametrize("origin", ["fallback", None, "llm"])
def test_reviews_only_replay_ai_snapshots_and_keep_history(env, monkeypatch, origin):
    monkeypatch.setattr(engine, "_now", lambda: 1000)
    with engine._STATE_LOCK:
        store = engine._get_store_locked()
        store.create_quiz("history", "test-kp-1", snapshot(origin), "answered",
                          str(engine.PROJECT_ROOT), "history", 800, 2000, answered_at=900)
        state = next_state(False, None, 900)
        state["next_review_at"] = 950
        store.record_attempt("history", "test-kp-1", "B", "B", False, 900, state)
    result = engine.next_quiz()
    assert result["has_pending"] is (origin == "llm")
    if origin == "llm":
        assert result["quiz"]["generated_by"] == "llm" and result["quiz"]["review"]
    assert store.attempt_count("history") == 1
    assert store.concept_state("test-kp-1")["attempts"] == 1
    assert store.get_quiz("history")["snapshot"] == snapshot(origin)
    assert env["gen"].calls == 0


def test_manual_context_can_still_generate_an_ai_quiz_without_file_changes(env, monkeypatch):
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda *_: env["matcher"].KP))
    result = engine.create_quiz("test-kp-1", context="解释缓存穿透与空值缓存")
    assert result["ok"] and result["generated_by"] == "llm"
    assert env["gen"].last_ctx == "解释缓存穿透与空值缓存"
    assert engine.submit_answer(result["quiz_id"], "A")["ok"]


def test_new_ai_result_without_review_is_not_delivered(env, monkeypatch):
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda *_: env["matcher"].KP))
    monkeypatch.setattr(engine, "generator", SimpleNamespace(generate=lambda *_: snapshot("llm")))
    result = engine.create_quiz("test-kp-1", context="手动描述缓存过期策略")
    assert result["ok"] is False and "审核记录" in result["reason"]
    assert engine._state["quizzes"] == {}


def test_review_evidence_is_persisted_but_hidden_before_answer(env, monkeypatch):
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda *_: env["matcher"].KP))
    result = engine.create_quiz("test-kp-1", context="手动描述缓存过期策略")
    assert result["ok"] and "grounding" not in result and "answer" not in result
    assert engine._store.get_quiz(result["quiz_id"])["snapshot"]["grounding"]["version"] == 1
