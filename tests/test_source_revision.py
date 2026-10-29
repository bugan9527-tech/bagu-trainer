"""A delayed manual generation cannot resurrect deleted content."""
import threading
from types import SimpleNamespace

from bagu_trainer.application.learning import engine


def test_late_generation_is_discarded_at_content_revision_boundary(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    generator = env["gen"].generate
    def delayed(question, context):
        entered.set()
        assert release.wait(3)
        return generator(question, context)
    monkeypatch.setattr(env["gen"], "generate", delayed)
    question = {**env["matcher"].KP, "sources": [{"repository": "fixture/source", "commit": "a" * 40}]}
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda identifier: question))
    result = []
    worker = threading.Thread(target=lambda: result.append(engine.create_quiz(question["id"], "synthetic context")))
    worker.start()
    try:
        assert entered.wait(2)
        engine.invalidate_content()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not result[0]["ok"] and "过期" in result[0]["note"]
    assert not engine.session._state["quizzes"] and not engine.session._state["pending"]
    assert engine.session._store is None
