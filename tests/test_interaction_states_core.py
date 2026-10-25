"""Retry failed generation without replaying successful work or learning writes."""
import pytest

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
    assert unavailable[0]["failure_kind"] == "service"
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


def test_quality_skip_is_silent_and_only_new_context_generates_again(env, monkeypatch):
    context, calls, events = ["baseline"], [], []
    monkeypatch.setattr(engine, "_collect_context", lambda *_: {
        "context": context[0], "changes": {"source": "git"}, "sources": []})
    original = env["gen"].generate

    def generate(question, text):
        calls.append(text)
        if len(calls) == 1:
            return {"available": False, "failure_kind": "quality",
                    "unavailable_reason": "关联性审核未通过：来源正文对结论的支持。"}
        return original(question, text)

    monkeypatch.setattr(env["gen"], "generate", generate)
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    monitor.ui_post = events.append
    monitor.poll()
    context[0] = "redis first edit"
    assert monitor.poll()["state"] == "watching"
    assert events[-1][1] == {"busy": False, "state": "watching",
                              "reason": "等待新的代码改动。", "retryable": False}
    assert not monitor.failed_generations and not monitor.list_pending()
    for _ in range(3):
        assert monitor.poll()["state"] == "watching"
    monitor.retry_generation()
    assert len(calls) == 1
    context[0] = "redis second edit"
    assert monitor.poll()["state"] == "quiz_ready"
    assert len(calls) == 2 and monitor.take_quiz()
    assert engine.get_learning_stats()["total_attempts"] == 0


@pytest.mark.parametrize("kind", [None, "unknown", "service"])
@pytest.mark.parametrize("explicit_failure", [False, True])
def test_unknown_or_service_failure_remains_retryable(env, monkeypatch, kind, explicit_failure):
    candidate = {"failure_kind": kind}
    if explicit_failure:
        candidate.update(available=False, unavailable_reason="HTTP 429")
    monkeypatch.setattr(env["gen"], "generate", lambda *_: dict(candidate))
    monitor = DesktopMonitor(engine)
    events = []
    monitor.ui_post = events.append
    root = engine._normalize_project_root(engine.PROJECT_ROOT)
    assert monitor._generate(root, {"context": "redis edit"}) == "generation_unavailable"
    assert monitor.failed_generations and events[-1][1]["retryable"]
    if explicit_failure:
        assert events[-1][1]["reason"] == "HTTP 429"
    else:
        assert "仅允许展示 AI 生成" in events[-1][1]["reason"]


def test_quality_skip_clears_only_its_project_service_failure(env, monkeypatch, tmp_path):
    roots = [engine._normalize_project_root(tmp_path / name) for name in ("a", "b")]
    env["cfg"]["project_roots"] = roots
    results = iter(["service", "service", "quality", "quality"])
    monkeypatch.setattr(env["gen"], "generate", lambda *_: {
        "available": False, "failure_kind": next(results), "unavailable_reason": "fixed diagnostic"})
    monitor = DesktopMonitor(engine)
    events = []
    monitor.ui_post = events.append
    for root in roots:
        assert monitor._generate(root, {"context": "redis edit"}) == "generation_unavailable"
    monitor._generate(roots[0], {"context": "redis edit"}, ("test-kp-1",))
    assert set(monitor.failed_generations) == {roots[1]}
    assert events[-1][1]["state"] == "generation_unavailable"
    assert events[-1][1]["reason"] == "fixed diagnostic"
    monitor._generate(roots[1], {"context": "redis edit"}, ("test-kp-1",))
    assert not monitor.failed_generations
    assert events[-1][1]["state"] == "watching"
    assert events[-1][1]["reason"] == "等待新的代码改动。"


@pytest.mark.parametrize("with_service", [False, True])
def test_mixed_quality_service_and_success_retry_only_service(env, monkeypatch, with_service):
    names = ("quality", "service", "success") if with_service else ("quality", "success")
    questions = [{**env["matcher"].KP, "id": name} for name in names]
    monkeypatch.setattr(env["matcher"], "rank", lambda *_a, **_k: [
        {"question": q, "score": 5, "hits": ["redis"], "confidence": 1, "margin": 1}
        for q in questions])
    calls = {q["id"]: 0 for q in questions}
    original = env["gen"].generate

    def generate(q, text):
        calls[q["id"]] += 1
        if q["id"] == "quality" or (q["id"] == "service" and calls["service"] == 1):
            return {"available": False, "failure_kind": q["id"], "unavailable_reason": "fixed diagnostic"}
        return original(q, text)

    monkeypatch.setattr(env["gen"], "generate", generate)
    monitor = DesktopMonitor(engine)
    root = engine._normalize_project_root(engine.PROJECT_ROOT)
    state = monitor._generate(root, {"context": "redis edit"})
    assert len(monitor.list_pending()) == 1
    if with_service:
        assert state == "generation_unavailable"
        assert monitor.failed_generations[root][1] == ("service",)
        assert monitor.retry_generation()["state"] == "quiz_ready"
        assert len(monitor.list_pending()) == 2
        assert calls == {"quality": 1, "service": 2, "success": 1}
    else:
        assert state == "quiz_ready" and not monitor.failed_generations
        monitor.retry_generation()
        assert calls == {"quality": 1, "success": 1}


def test_pause_during_quality_skip_stays_paused(env, monkeypatch):
    monitor = DesktopMonitor(engine)

    def generate(*_):
        monitor.paused = True
        return {"available": False, "failure_kind": "quality", "unavailable_reason": "not supported"}

    monkeypatch.setattr(env["gen"], "generate", generate)
    events = []
    monitor.ui_post = events.append
    root = engine._normalize_project_root(engine.PROJECT_ROOT)
    assert monitor._generate(root, {"context": "redis edit"}) == "paused"
    assert events[-1][1]["state"] == "paused"
    assert not events[-1][1]["retryable"]
    assert monitor.poll()["state"] == "paused"
