"""Event-gated generation reservations and lifecycle/transaction regressions."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from bagu_trainer import engine


def generate(root=None, **kwargs):
    return engine.generate_activity(root or str(engine.PROJECT_ROOT),
                                    {"context": "redis gated edit"}, **kwargs)


@pytest.mark.parametrize("transition", ["reload", "invalidate", "close", "forget", "scope"])
def test_late_activity_generation_is_discarded(env, monkeypatch, transition):
    entered, release = threading.Event(), threading.Event()
    original = env["gen"].generate
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(env["gen"], "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(generate)
        try:
            assert entered.wait(1)
            if transition == "reload":
                monkeypatch.setattr(engine, "kb", engine.kb)  # reload assigns this module global.
                bank = SimpleNamespace(explicit_empty=False)
                monkeypatch.setattr(engine, "KnowledgeBase", lambda: bank)
                monkeypatch.setattr(engine, "Matcher", lambda _: env["matcher"])
                engine.reload_bank()
            elif transition == "invalidate":
                engine.invalidate_content()
            elif transition == "close":
                engine.session.close()
                engine.initialize()
            elif transition == "forget":
                engine.session.forget_projects([str(engine.PROJECT_ROOT)])
            else:
                env["cfg"]["project_roots"] = []
        finally:
            release.set()
        assert result.result(1)[1] == []
    assert engine.session.list_pending() == []
    if transition == "close":
        engine.session.close()


def test_concurrent_explicit_retry_cannot_double_bill_reserved_kp(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = env["gen"].generate
    calls = []
    def delayed(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(env["gen"], "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(generate)
        try:
            assert entered.wait(1)
            # An explicit retry bypasses a failed fingerprint, never an active reservation.
            assert not generate(retry_failed_ids={"test-kp-1"})[1]
        finally:
            release.set()
        assert len(first.result(1)[1]) == 1
    assert len(calls) == 1 and len(engine.session.list_pending()) == 1


def test_cooldown_is_rechecked_after_model_returns(env, monkeypatch, tmp_path):
    first_root, other_root = str(engine.PROJECT_ROOT), str(tmp_path)
    env["cfg"]["project_roots"] = [first_root, other_root]
    existing = generate(first_root)[1][0]["quiz_id"]
    entered, release = threading.Event(), threading.Event()
    original = env["gen"].generate
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(env["gen"], "generate", delayed)
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(generate, other_root)
        try:
            assert entered.wait(1)
            assert engine.submit_answer(existing, "A")["ok"]
        finally:
            release.set()
        assert not result.result(1)[1]
    assert not engine.session.list_pending()
    assert engine.session._store.attempt_count(existing) == 1


def test_old_scheduled_revision_never_starts_model(env):
    revision = engine.session._generation_token()
    engine.invalidate_content()
    assert not generate(revision=revision)[1]
    assert env["gen"].calls == 0


def test_old_completion_cannot_remove_replacement_reservation(env, monkeypatch):
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    original = env["gen"].generate
    calls = []
    def delayed(*args):
        index = len(calls)
        calls.append(1)
        entered[index].set()
        assert release[index].wait(3)
        return original(*args)
    monkeypatch.setattr(env["gen"], "generate", delayed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        old = workers.submit(generate)
        try:
            assert entered[0].wait(1)
            engine.invalidate_content()
            current = workers.submit(generate)
            assert entered[1].wait(1)
            release[0].set()
            assert not old.result(1)[1]
            assert not generate(retry_failed_ids={"test-kp-1"})[1]
            release[1].set()
            assert len(current.result(1)[1]) == 1
        finally:
            for event in release:
                event.set()
    assert len(calls) == 2
