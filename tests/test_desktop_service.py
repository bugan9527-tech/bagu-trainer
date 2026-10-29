"""Desktop transport contracts exercised against the existing learning engine."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.application.learning import engine
from bagu_trainer.desktop.service import DesktopService


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


def test_uninstall_handoff_uses_owner_pid_without_touching_user_data(desktop, monkeypatch):
    from bagu_trainer.desktop import cleanup_cli
    calls = []
    monkeypatch.setenv("BAGU_ELECTRON_OWNER_PID", "123")
    monkeypatch.setattr(cleanup_cli, "launch_uninstall", lambda **values: calls.append(values))
    assert desktop.command("uninstall.launch") == {"ok": True}
    assert calls == [{"wait_pid": 123}]


def test_background_model_test_cannot_block_practice_submission(desktop, monkeypatch):
    from bagu_trainer.application.settings import verification as onboarding
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


def test_model_test_cancellation_is_owned_by_settings_workflow(desktop, monkeypatch):
    from bagu_trainer.application.settings import verification as onboarding
    started = threading.Event()

    def test_connection(_settings, *, cancel):
        started.set()
        assert cancel.wait(3)
        raise onboarding.SetupError("cancelled", "连接测试已取消，可修改配置后重新测试。")

    monkeypatch.setattr(onboarding, "test_connection", test_connection)
    desktop.command("model.test", {"base_url": "http://127.0.0.1:9/v1", "model": "test",
                                    "api_key": "test-key", "timeout_sec": 2})
    assert started.wait(2)
    desktop.command("model.cancel")
    deadline = time.monotonic() + 2
    while desktop.snapshot()["model_task"]["state"] == "testing" and time.monotonic() < deadline:
        time.sleep(.01)
    assert desktop.snapshot()["model_task"]["state"] == "cancelled"


def test_changed_model_form_must_be_tested_again(desktop, monkeypatch):
    from bagu_trainer.application.settings import verification as onboarding

    monkeypatch.setattr(onboarding, "test_connection",
                        lambda settings, **_kwargs: SimpleNamespace(settings=settings))
    checked = {"base_url": "http://127.0.0.1:9/v1", "model": "test",
               "api_key": "test-key", "timeout_sec": 2, "form_revision": 1}
    desktop.command("model.test", checked)
    deadline = time.monotonic() + 2
    while desktop.snapshot()["model_task"]["state"] == "testing" and time.monotonic() < deadline:
        time.sleep(.01)
    changed = {**checked, "model": "changed", "form_revision": 2}
    with pytest.raises(ValueError, match="重新测试"):
        desktop.command("model.save", changed)
    assert desktop.snapshot()["model_task"]["state"] == "verified"


def test_bank_completion_is_applied_only_on_serial_prepare_command(desktop, monkeypatch):
    from bagu_trainer.infrastructure.bank_build.service import BankBuildService
    from bagu_trainer.infrastructure.bank_build.control import BuildProgress

    built = threading.Event()
    monkeypatch.setattr(BankBuildService, "build", lambda _service, _repositories, **kwargs: (
        kwargs["progress"](BuildProgress("extract", "处理中", 1, 1))
        or SimpleNamespace(questions=3, sources=())))
    learning = desktop.monitor.engine.learning
    applied = []
    monkeypatch.setattr(learning, "replace_question_bank", lambda: applied.append("command"))
    desktop.emit = lambda message: built.set() if message["type"] == "bank_built" else None
    desktop.command("bank.build", {"repositories": ["synthetic/source"]})
    assert built.wait(3)
    assert desktop.snapshot()["bank_task"]["state"] == "applying"
    assert applied == []
    assert desktop.command("prepare")["ok"]
    assert applied == ["command"]
    assert desktop.snapshot()["bank_task"]["state"] == "applied"


def test_close_cancels_and_joins_model_task(desktop, monkeypatch):
    from bagu_trainer.application.settings import verification as onboarding
    finished = threading.Event()

    def test_connection(_settings, *, cancel):
        assert cancel.wait(3)
        finished.set()
        raise onboarding.SetupError("cancelled", "连接测试已取消，可修改配置后重新测试。")

    monkeypatch.setattr(onboarding, "test_connection", test_connection)
    desktop.command("model.test", {"base_url": "http://127.0.0.1:9/v1", "model": "test",
                                    "api_key": "test-key", "timeout_sec": 2})
    desktop.close()
    assert finished.is_set()


def test_bank_cancel_reaches_running_build(desktop, monkeypatch):
    from bagu_trainer.infrastructure.bank_build.service import BankBuildService
    from bagu_trainer.infrastructure.bank_build.control import BuildCancelled
    started, cancelled = threading.Event(), threading.Event()

    def build(_service, _repositories, *, cancel, **_kwargs):
        started.set()
        assert cancel.wait(3)
        cancelled.set()
        raise BuildCancelled("prepare", "已取消")

    monkeypatch.setattr(BankBuildService, "build", build)
    desktop.command("bank.build", {"repositories": ["synthetic/source"]})
    assert started.wait(2)
    desktop.command("bank.cancel")
    deadline = time.monotonic() + 2
    while desktop.snapshot()["bank_task"]["state"] == "building" and time.monotonic() < deadline:
        time.sleep(.01)
    assert cancelled.is_set()
    assert desktop.snapshot()["bank_task"]["state"] == "cancelled"


def test_empty_project_scope_can_be_saved_without_deleting_history(desktop):
    desktop.command("projects.save", {"projects": []})
    assert config.read_user_config()["project_roots"] == []
    assert desktop.monitor.applied_roots == []


def test_completed_bank_build_requests_serial_application(desktop, monkeypatch):
    from bagu_trainer.infrastructure.bank_build.service import BankBuildService
    from bagu_trainer.infrastructure.bank_build.control import BuildProgress
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
