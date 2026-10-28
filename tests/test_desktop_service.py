"""Desktop transport contracts exercised against the existing learning engine."""
import json
import threading
from types import SimpleNamespace

import pytest

from bagu_trainer import config, engine
from bagu_trainer.desktop_service import DesktopService


@pytest.fixture
def desktop(env, monkeypatch, tmp_path):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setattr(config, "USER_DATA_ROOT", path)
    monkeypatch.setattr(config, "CONFIG_PATH", path / "config.json")
    config._write_config_atomic({"project_roots": env["cfg"]["project_roots"], "llm": {"api_key": "private-key"}})
    service = DesktopService()
    service.monitor.engine = engine
    service.monitor.capabilities_ready = True
    service.monitor.apply_projects(env["cfg"]["project_roots"])
    yield service
    service.close()


def test_real_pending_enter_grade_history_are_one_learning_loop(desktop):
    _, generated, _, _ = engine.generate_activity(str(engine.PROJECT_ROOT), {"context": "redis changed", "sources": []})
    quiz_id = generated[0]["quiz_id"]
    snapshot = desktop.command("snapshot")
    assert snapshot["pending"][0]["status"] == "ready"
    assert not {"answer", "analysis"} & snapshot["pending"][0].keys()
    assert "private-key" not in json.dumps(snapshot)
    assert desktop.command("practice.enter", {"quiz_id": quiz_id})["quiz"]["quiz_id"] == quiz_id
    assert desktop.command("practice.submit", {"quiz_id": quiz_id, "answer": "A"})["correct"]
    assert not desktop.command("practice.submit", {"quiz_id": quiz_id, "answer": "A"})["ok"]
    assert desktop.command("history.list")["total"] == 1


def test_monitoring_pause_keeps_explicit_practice_available(desktop):
    _, generated, _, _ = engine.generate_activity(str(engine.PROJECT_ROOT), {"context": "redis paused", "sources": []})
    desktop.command("monitor.pause", {"paused": True})
    assert desktop.command("practice.enter", {"quiz_id": generated[0]["quiz_id"]})["quiz"]


def test_unknown_commands_and_invalid_answers_do_not_write(desktop):
    with pytest.raises(ValueError):
        desktop.command("execute", {"code": "anything"})
    with pytest.raises(ValueError):
        desktop.command("practice.submit", {"quiz_id": "missing", "answer": "E"})
    assert desktop.command("history.list")["total"] == 0


def test_background_model_test_cannot_block_practice_submission(desktop, monkeypatch):
    from bagu_trainer import onboarding
    started, release = threading.Event(), threading.Event()
    def test_connection(settings, **kwargs):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(settings=settings)
    monkeypatch.setattr(onboarding, "test_connection", test_connection)
    _, generated, _, _ = engine.generate_activity(str(engine.PROJECT_ROOT), {"context": "redis network wait", "sources": []})
    try:
        desktop.command("model.test", {"base_url": "http://127.0.0.1:9/v1", "model": "test", "api_key": "test-key", "timeout_sec": 2})
        assert started.wait(2)
        assert desktop.command("practice.submit", {"quiz_id": generated[0]["quiz_id"], "answer": "A"})["ok"]
        assert desktop.command("history.list")["total"] == 1
    finally:
        release.set()


def test_empty_project_scope_can_be_saved_without_deleting_history(desktop):
    desktop.command("projects.save", {"projects": []})
    assert config.read_user_config()["project_roots"] == []
    assert desktop.monitor.applied_roots == []


def test_completed_bank_build_requests_serial_application(desktop, monkeypatch):
    from bagu_trainer.bank_build.service import BankBuildService
    from bagu_trainer.bank_build.control import BuildProgress
    applied = threading.Event()
    def emit(message):
        if message["type"] == "bank_built":
            applied.set()
    desktop.emit = emit
    def build(_service, repositories, **kwargs):
        kwargs["progress"](BuildProgress("extract", "正在处理自编来源", 1, 1))
        return SimpleNamespace(questions=7, sources=())
    monkeypatch.setattr(BankBuildService, "build", build)
    desktop.command("bank.build", {"repositories": ["synthetic/source"]})
    assert applied.wait(3)
    assert desktop.snapshot()["bank_task"]["state"] == "applying"
