"""First-use bank application through the real desktop/workflow boundaries."""
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from bagu_trainer import bootstrap
from bagu_trainer.desktop.service import DesktopService
from bagu_trainer.infrastructure import config, secrets
from bagu_trainer.infrastructure.bank_build.service import BankBuildService
from bagu_trainer.infrastructure.matcher import Matcher
from bagu_trainer.application.settings import verification
from tests.test_engine_instances import Generator


@pytest.fixture
def bank_desktop(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(profile))
    monkeypatch.setattr(config, "USER_DATA_ROOT", profile)
    monkeypatch.setattr(config, "CONFIG_PATH", profile / "config.json")
    config._write_config_atomic({"project_roots": []})
    vault = {}
    monkeypatch.setattr(secrets, "_load_keyring", lambda: SimpleNamespace(
        set_password=lambda service, user, key: vault.update({(service, user): key}),
        get_password=lambda service, user: vault.get((service, user))))
    monkeypatch.setattr(verification, "open_model_request", lambda *_a, **_kw: io.BytesIO(
        b'{"choices":[{"message":{"content":"OK"}}]}'))
    events, built = [], threading.Event()

    def emit(event):
        events.append(event)
        if event["type"] == "bank_built":
            built.set()

    service = DesktopService(emit)
    # Real engine assembly, bound only to this temporary profile, with a fake model.
    service.coordinator.engine_loader = lambda: bootstrap.create_engine(
        cfg=config.load_config(), config_loader=config.load_config, generator=Generator(),
        matcher_factory=lambda bank: Matcher(bank, raw_min=0, conf_min=0,
                                             conf_strong=0, margin_min=0))
    generations = []

    def build(_builder, _repositories, **_kwargs):
        generations.append(len(generations) + 1)
        data = profile / "data"
        data.mkdir(exist_ok=True)
        (data / "questions.json").write_text(json.dumps([{
            "id": "redis", "knowledge_point": "Redis cache",
            "question": f"Synthetic bank generation {generations[-1]}?",
            "difficulty": "easy", "tags": ["Redis"], "keywords": ["redis", "cache"],
        }]), encoding="utf-8")
        return SimpleNamespace(questions=1, sources=())

    monkeypatch.setattr(BankBuildService, "build", build)

    def start_build():
        built.clear()
        assert service.command("bank.build", {"repositories": ["synthetic/source"]})["ok"]
        assert built.wait(3)
        assert service.banks.task_snapshot()["state"] == "applying"

    def save_model():
        form = {"base_url": "http://127.0.0.1:9/v1", "model": "synthetic",
                "api_key": "synthetic-key", "timeout_sec": 2}
        service.command("model.test", form)
        deadline = time.monotonic() + 3
        while service.settings.task_snapshot()["state"] == "testing" and time.monotonic() < deadline:
            time.sleep(.01)
        assert service.settings.task_snapshot()["state"] == "verified"
        return service.command("model.save", form)

    yield SimpleNamespace(service=service, project=str(project.resolve()),
                          start_build=start_build, save_model=save_model,
                          events=events, generations=generations, profile=profile)
    service.close()


@pytest.mark.parametrize("prepare_entry", ["command", "direct", "model.save", "projects.save"])
def test_build_before_setup_finishes_without_an_engine(bank_desktop, prepare_entry):
    case = bank_desktop
    service = case.service
    case.start_build()
    assert service.monitor.engine is None
    if prepare_entry == "model.save":
        result = case.save_model()
    elif prepare_entry == "projects.save":
        result = service.command("projects.save", {"projects": [case.project]})
    elif prepare_entry == "direct":
        result = service.prepare()
    else:
        result = service.command("prepare")
    assert result == {"ok": True, "ready": False}
    assert service.monitor.engine is None
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()
    plan = service.command("bank.plan", {"kind": "history"})
    assert service.command("bank.clean", {"token": plan["token"]})["ok"]
    # Both task gates must reopen even before the rest of setup is complete.
    case.start_build()
    assert service.command("prepare") == {"ok": True, "ready": False}
    assert service.monitor.engine is None
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()
    if prepare_entry != "model.save":
        case.save_model()
    assert service.command("projects.save", {"projects": [case.project]})["ready"]
    case.start_build()
    assert service.command("prepare")["ready"]
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()


def test_model_and_projects_before_build_create_engine_on_prepare(bank_desktop):
    case = bank_desktop
    service = case.service
    assert not case.save_model()["ready"]
    assert not service.command("projects.save", {"projects": [case.project]})["ready"]
    assert service.monitor.engine is None
    case.start_build()
    assert service.command("prepare")["ready"]
    assert service.monitor.engine is service.coordinator.engine
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()
    assert service.command("bank.plan", {"kind": "history"})["token"]
    case.start_build()
    assert service.prepare()["ready"]
    assert service.banks.task_snapshot()["state"] == "applied"


def ready_engine(case):
    case.start_build()
    case.save_model()
    assert case.service.command("projects.save", {"projects": [case.project]})["ready"]
    return case.service.monitor.engine


@pytest.mark.parametrize("prepare_entry", ["direct", "command", "model.save", "projects.save"])
def test_rebuild_reloads_existing_engine_and_preserves_pending_and_drafts(bank_desktop, prepare_entry):
    case = bank_desktop
    service = case.service
    engine = ready_engine(case)
    result = engine.learning.generate_for_activity(case.project,
        {"context": "Redis cache redis cache changed", "sources": []})
    assert result["result"][1], result
    quiz_id = result["result"][1][0]["quiz_id"]
    entered = service.command("practice.enter", {"quiz_id": quiz_id})["quiz"]
    bank = engine.kb
    case.events.clear()
    case.start_build()
    # Background build must leave the live engine for the serial lane to reload.
    assert engine.kb is bank
    if prepare_entry == "direct":
        prepared = service.prepare()
    elif prepare_entry == "model.save":
        prepared = case.save_model()
    elif prepare_entry == "projects.save":
        prepared = service.command("projects.save", {"projects": [case.project]})
    else:
        prepared = service.command("prepare")
    assert prepared["ready"]
    assert service.monitor.engine is engine is service.coordinator.engine
    assert engine.kb is not bank
    assert engine.kb.get("redis")["question"] == "Synthetic bank generation 2?"
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()
    assert service.snapshot()["pending"][0]["quiz_id"] == quiz_id
    assert service.command("practice.resume")["quiz"] == entered
    # Electron owns drafts and clears them only for this deletion notification.
    assert not any(event["type"] == "content_changed" for event in case.events)
    assert service.command("bank.plan", {"kind": "history"})["token"]


def test_repeated_prepare_does_not_reload_applied_bank(bank_desktop):
    case = bank_desktop
    engine = ready_engine(case)
    case.start_build()
    assert case.service.prepare()["ready"]
    bank = engine.kb
    for _ in range(3):
        assert case.service.command("prepare")["ready"]
        assert engine.kb is bank
        assert case.service.monitor.engine is engine
        assert case.service.banks.task_snapshot()["state"] == "applied"


def test_failed_application_is_not_applied_and_explicit_prepare_retries(bank_desktop):
    case = bank_desktop
    service = case.service
    engine = ready_engine(case)
    generated = engine.learning.generate_for_activity(case.project,
        {"context": "Redis cache redis cache changed", "sources": []})
    quiz_id = generated["result"][1][0]["quiz_id"]
    entered = service.command("practice.enter", {"quiz_id": quiz_id})["quiz"]
    case.events.clear()
    case.start_build()
    path = case.profile / "data" / "questions.json"
    valid = path.read_text(encoding="utf-8")
    path.write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(ValueError):
        service.prepare()
    assert service.banks.task_snapshot()["state"] == "applying"
    assert service.banks.is_busy()
    assert not service.ready
    with pytest.raises(ValueError):
        service.command("bank.build", {"repositories": ["synthetic/source"]})
    with pytest.raises(ValueError):
        service.command("bank.plan", {"kind": "history"})
    path.write_text(valid, encoding="utf-8")
    assert service.command("prepare")["ready"]
    assert service.monitor.engine is engine
    assert engine.kb.get("redis")["question"] == "Synthetic bank generation 2?"
    assert service.banks.task_snapshot()["state"] == "applied"
    assert not service.banks.is_busy()
    assert service.snapshot()["pending"][0]["quiz_id"] == quiz_id
    resumed = service.command("practice.resume")["quiz"]
    for field in ("quiz_id", "question", "options"):
        assert resumed[field] == entered[field]
    assert not any(event["type"] == "content_changed" for event in case.events)
    plan = service.command("bank.plan", {"kind": "history"})
    assert service.command("bank.clean", {"token": plan["token"]})["ok"]
    case.start_build()
    assert service.command("prepare")["ready"]
    assert service.banks.task_snapshot()["state"] == "applied"
