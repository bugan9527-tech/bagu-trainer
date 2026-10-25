"""Retry failed generation without replaying successful work or learning writes."""
from bagu_trainer import engine
from bagu_trainer.desktop_monitor import DesktopMonitor


def test_manual_generation_retry_bypasses_only_failed_fingerprint(env, monkeypatch):
    clock, context, calls, events = [0], ["baseline"], [], []
    monkeypatch.setattr(engine, "_collect_context", lambda *_: {"context": context[0], "changes": {"source": "git"}, "sources": ["local change"]})
    original = env["gen"].generate
    def generate(question, text):
        calls.append(1)
        if len(calls) == 1:
            return {"available": False, "unavailable_reason": "HTTP 429：额度不足，稍后重试。"}
        return original(question, text)
    monkeypatch.setattr(env["gen"], "generate", generate)
    monitor = DesktopMonitor(engine, quiet_seconds=0, clock=lambda: clock[0])
    monitor.ui_post = events.append
    monitor.poll()
    context[0] = "redis cache new code"
    assert monitor.poll()["state"] == "generation_unavailable"
    assert len(calls) == 1 and monitor.failed_generations
    assert events[0][1]["busy"] and not events[-1][1]["busy"]
    assert "429" in events[-1][1]["reason"]
    assert monitor.poll()["state"] == "generation_unavailable"
    assert len(calls) == 1  # ordinary polling never retries a charged request
    assert monitor.retry_generation()["state"] == "quiz_ready"
    assert len(calls) == 2 and not monitor.failed_generations
    quiz = monitor.take_quiz()
    assert quiz is not None
    monitor.retry_generation()
    assert len(calls) == 2  # success can never be replayed with this action
    assert engine.get_learning_stats()["total_attempts"] == 0
    assert engine.submit_answer(quiz["quiz_id"], "A")["ok"]
    assert not engine.submit_answer(quiz["quiz_id"], "A")["ok"]
    assert engine.get_learning_stats()["total_attempts"] == 1


def test_generator_exception_becomes_safe_retryable_unavailable(env, monkeypatch):
    def broken(*_):
        raise OSError("must-not-display-secret")
    monkeypatch.setattr(env["gen"], "generate", broken)
    with engine.session._lock:
        _, generated, _, unavailable = engine._generate_matches_locked(
            "redis changed", str(engine.PROJECT_ROOT), 1, engine._caps())
    assert not generated and "OSError" in unavailable[0]["reason"]
    assert "must-not-display" not in unavailable[0]["reason"]
    assert engine.get_learning_stats()["total_attempts"] == 0


def test_retry_rechecks_scope_and_pause_before_network(env, monkeypatch):
    monitor = DesktopMonitor(engine)
    root = engine._normalize_project_root(engine.PROJECT_ROOT)
    monitor.failed_generations[root] = ({"context": "redis", "sources": []}, ("test-kp-1",))
    monitor.paused = True
    assert monitor.retry_generation()["state"] == "paused"
    assert env["gen"].calls == 0
    monitor.paused = False
    monkeypatch.setattr(engine, "load_config", lambda: {"project_roots": [str(engine.PROJECT_ROOT / "different")]})
    monitor.retry_generation()
    assert not monitor.failed_generations and env["gen"].calls == 0


def test_mixed_success_retry_never_replays_successful_kp(env, monkeypatch):
    base = env["matcher"].KP
    questions = [{**base, "id": name} for name in ("success", "failed")]
    monkeypatch.setattr(env["matcher"], "rank", lambda *_args, **_kwargs: [
        {"question": q, "score": 5, "hits": ["redis"], "confidence": 1, "margin": 1} for q in questions])
    calls = {"success": 0, "failed": 0}
    original = env["gen"].generate
    def generate(q, text):
        calls[q["id"]] += 1
        if q["id"] == "failed" and calls["failed"] == 1:
            return {"available": False, "unavailable_reason": "暂不可用"}
        return original(q, text)
    monkeypatch.setattr(env["gen"], "generate", generate)
    with engine.session._lock:
        _, generated, _, failed = engine._generate_matches_locked("redis", str(engine.PROJECT_ROOT), 2, engine._caps())
        assert len(generated) == 1 and failed[0]["kp_id"] == "failed"
        _, retried, _, failures = engine._generate_matches_locked("redis", str(engine.PROJECT_ROOT), 2, engine._caps(), retry_failed_ids={"failed"})
        assert len(retried) == 1 and not failures
        engine._generate_matches_locked("redis", str(engine.PROJECT_ROOT), 2, engine._caps(), retry_failed_ids={"failed"})
    assert calls == {"success": 1, "failed": 2}
