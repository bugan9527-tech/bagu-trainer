"""Deterministic configuration races with isolated files and memory credentials."""
import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.application.settings import verification as onboarding
from bagu_trainer.infrastructure import secrets
from bagu_trainer.desktop import startup
from bagu_trainer.desktop import DesktopApp
from bagu_trainer.application.monitor import DesktopMonitor
from bagu_trainer.application.monitor import add_project


KEY = "local-concurrent-save-fixture"


class MemoryCredentials:
    def __init__(self):
        self.value = None
        self.writing = threading.Event()
        self.resume = threading.Event()
        self.fail_write = False
        self.fail_read = False

    def set_password(self, _service, _user, value):
        self.writing.set()
        if not self.resume.wait(5):
            raise TimeoutError("fixture credentials were not resumed")
        if self.fail_write:
            raise RuntimeError("fixture credential failure")
        self.value = value

    def get_password(self, *_):
        return None if self.fail_read else self.value


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "isolated-installation"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "USER_DATA_ROOT", root)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.json")
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(root))
    monkeypatch.delenv(secrets.ENV_KEY, raising=False)
    credentials = MemoryCredentials()
    monkeypatch.setattr(secrets, "_load_keyring", lambda: credentials)
    projects = [tmp_path / name for name in ("project-a", "project-b")]
    for project in projects:
        project.mkdir()
    settings = onboarding.validate_settings("https://example.invalid/v1", "fixture-model", KEY, 2)
    response = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
    verified = onboarding.test_connection(settings, opener=lambda *_a, **_k: io.BytesIO(response))
    startup.set_projects([projects[0]])
    cfg = config.read_user_config()
    cfg.update({"session_ingest": False, "custom": {"enabled": False, "items": []},
                "zero": 0, "empty": "", "nullable": None, "db_path": "data/original.db"})
    config._write_config_atomic(cfg)
    (root / "data").mkdir()
    (root / "data/original.db").write_bytes(b"learning data must stay untouched")
    (root / "data/questions.json").write_text(json.dumps([{
        "id": "fixture", "knowledge_point": "缓存", "question": "如何设置缓存有效期？",
    }]), encoding="utf-8")
    return SimpleNamespace(root=root, credentials=credentials, projects=projects, verified=verified)


def test_model_save_preserves_project_save_while_credentials_are_pending(installation):
    a, b = installation.projects
    credentials = installation.credentials
    with ThreadPoolExecutor(max_workers=1) as workers:
        model_save = workers.submit(onboarding.save_verified_model, installation.verified)
        try:
            assert credentials.writing.wait(5), "model save did not reach credentials"
            startup.set_projects([b])
            assert config.read_user_config()["project_roots"] == [str(b)]
        finally:
            credentials.resume.set()
        assert model_save.result(timeout=5) is None
    saved = config.read_user_config()
    assert saved["project_roots"] == [str(b)], "successful model save restored removed project A"
    assert saved["llm"] == installation.verified.settings.public()
    assert onboarding.saved_model_verified()


def edit(installation, kind):
    if kind == "projects":
        startup.set_projects([installation.projects[1]])
    elif kind == "add":
        add_project(installation.projects[1])
    elif kind == "verified_model":
        return onboarding.save_verified_model(installation.verified)
    elif kind == "legacy_model":
        return config.update_llm({**installation.verified.settings.public(), "api_key": KEY})
    else:
        raise AssertionError("unknown fixture editor")


@pytest.mark.parametrize("model_editor", ["verified_model", "legacy_model"])
@pytest.mark.parametrize("project_editor", ["projects", "add"])
def test_pending_credentials_preserve_projects_and_unrelated_changes(installation, model_editor, project_editor):
    credentials = installation.credentials
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(edit, installation, model_editor)
        try:
            assert credentials.writing.wait(5)
            edit(installation, project_editor)
            with config.config_transaction():
                latest = config.read_user_config()
                latest["custom"] = {"changed_while_saving": True}
                config._write_config_atomic(latest)
        finally:
            credentials.resume.set()
        assert saving.result(timeout=5) == ("keyring" if model_editor == "legacy_model" else None)
    saved = config.read_user_config()
    expected = [installation.projects[1]] if project_editor == "projects" else installation.projects
    assert saved["project_roots"] == list(map(str, expected))
    assert saved["custom"] == {"changed_while_saving": True}
    assert saved["session_ingest"] is False and saved["zero"] == 0
    assert saved["empty"] == "" and saved["nullable"] is None
    assert saved["llm"] == installation.verified.settings.public()
    assert KEY not in config.CONFIG_PATH.read_text(encoding="utf-8")


def test_project_save_keeps_existing_model_verification(installation):
    installation.credentials.resume.set()
    onboarding.save_verified_model(installation.verified)
    receipt = onboarding.state_path().read_bytes()
    startup.set_projects([installation.projects[1]])
    assert onboarding.saved_model_verified()
    assert onboarding.state_path().read_bytes() == receipt
    assert startup.check_readiness().ready


def test_model_commit_preserves_progress_updated_during_credentials(installation):
    credentials = installation.credentials
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(onboarding.save_verified_model, installation.verified)
        try:
            assert credentials.writing.wait(5)
            with config.config_transaction():
                state = onboarding.read_state()
                state["other_progress"] = {"keep": True}
                onboarding.atomic_json(onboarding.state_path(), state)
        finally:
            credentials.resume.set()
        saving.result(timeout=5)
    assert onboarding.read_state()["other_progress"] == {"keep": True}
    assert onboarding.saved_model_verified()


class ObservedLock:
    """Signal real lock contention; no sleep or scheduler timing assumption."""
    def __init__(self):
        self.lock = threading.RLock()
        self.blocked = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.blocked.set()
            assert self.lock.acquire(timeout=5), "configuration transaction deadlocked"
        return self

    def __exit__(self, *_):
        self.lock.release()


@pytest.mark.parametrize("first,second", [
    ("projects", "verified_model"), ("projects", "legacy_model"),
    ("add", "verified_model"), ("add", "legacy_model"),
    ("verified_model", "projects"), ("legacy_model", "projects"),
])
def test_editors_serialize_latest_read_through_atomic_commit(installation, monkeypatch, first, second):
    installation.credentials.resume.set()
    observed = ObservedLock()
    monkeypatch.setattr(config, "_CONFIG_LOCK", observed)
    at_commit, resume = threading.Event(), threading.Event()
    writer = config._write_config_atomic

    def pause_first_commit(value):
        if not at_commit.is_set():
            at_commit.set()
            assert resume.wait(5), "first config commit was not resumed"
        writer(value)

    monkeypatch.setattr(config, "_write_config_atomic", pause_first_commit)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first_save = workers.submit(edit, installation, first)
        try:
            assert at_commit.wait(5)
            second_save = workers.submit(edit, installation, second)
            assert observed.blocked.wait(5), "second editor bypassed the shared transaction"
        finally:
            resume.set()
        first_save.result(timeout=5)
        second_save.result(timeout=5)
    saved = config.read_user_config()
    expected = installation.projects if first == "add" else [installation.projects[1]]
    assert saved["project_roots"] == list(map(str, expected))
    assert saved["llm"] == installation.verified.settings.public()
    assert saved["custom"] == {"enabled": False, "items": []}
    if "verified_model" in (first, second):
        assert onboarding.saved_model_verified()


def make_monitor():
    visited = []
    session = SimpleNamespace(_diff_readers={}, forget_projects=lambda roots: None)
    scope = [None]
    def apply_scope(roots):
        previous = scope[0] or ()
        session.forget_projects(set(previous) - set(roots))
        scope[0] = tuple(roots)
    learning = SimpleNamespace(has_question_bank=lambda: True, question_count=lambda: 0,
        apply_project_scope=apply_scope, generation_window=lambda: None)
    engine = SimpleNamespace(load_config=config.load_config, PROJECT_ROOT=config.PROJECT_ROOT,
        session=session, learning=learning,
        observe_project=lambda root, **_: visited.append(root) or {"context": ""})
    return DesktopMonitor(engine), visited


def test_project_application_and_fresh_process_restore_only_new_scope(installation):
    a, b = (os.path.normcase(os.path.realpath(path)) for path in map(str, installation.projects))
    monitor, visited = make_monitor()
    monitor.poll()
    assert visited == [a]
    monitor.waiting[a] = (0, {"context": "obsolete pending task"})
    monitor.failed_generations[a] = ("obsolete failure", ())
    credentials = installation.credentials
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(onboarding.save_verified_model, installation.verified)
        try:
            assert credentials.writing.wait(5)
            startup.set_projects([b])
            DesktopApp._apply_projects_serial(SimpleNamespace(monitor=monitor), [b])
            assert a not in monitor.initialized and a not in monitor.engine.session._diff_readers
            assert a not in monitor.waiting and a not in monitor.failed_generations
            visited.clear()
            monitor.poll()
            assert visited == [b]
        finally:
            credentials.resume.set()
        saving.result(timeout=5)
    visited.clear()
    monitor.poll()
    assert visited == [b] and monitor.initialized == {b}
    assert (installation.root / "data/original.db").read_bytes() == b"learning data must stay untouched"
    child = r'''
import json, os, threading
from types import SimpleNamespace
from bagu_trainer.infrastructure import config
from bagu_trainer.application.settings import verification as onboarding
from bagu_trainer.infrastructure import secrets
from bagu_trainer.desktop import startup
from bagu_trainer.application.monitor import DesktopMonitor
secrets._load_keyring = lambda: SimpleNamespace(get_password=lambda *_: "local-concurrent-save-fixture")
report = startup.check_readiness()
assert report.ready
assert onboarding.saved_model_verified()
visited = []
session = SimpleNamespace(_diff_readers={}, forget_projects=lambda roots: None)
scope = [None]
def apply_scope(roots):
    previous = scope[0] or ()
    session.forget_projects(set(previous) - set(roots))
    scope[0] = tuple(roots)
learning = SimpleNamespace(has_question_bank=lambda: True, question_count=lambda: 0,
    apply_project_scope=apply_scope, generation_window=lambda: None)
engine = SimpleNamespace(load_config=config.load_config, PROJECT_ROOT=config.PROJECT_ROOT,
    session=session, learning=learning,
    observe_project=lambda root, **_: visited.append(root) or {"context": ""})
monitor = DesktopMonitor(engine)
monitor.poll()
assert visited == [os.path.normcase(os.path.realpath(root))
                   for root in config.read_user_config()["project_roots"]]
print(json.dumps({"projects": visited, "verified": True}))
'''
    process = subprocess.run([sys.executable, "-B", "-c", child], env=os.environ.copy(),
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"projects": [b], "verified": True}


def test_atomic_replace_keeps_old_json_readable_until_commit(installation, monkeypatch):
    before = config.CONFIG_PATH.read_bytes()
    ready, resume = threading.Event(), threading.Event()
    replace = config.os.replace
    fsync = config.os.fsync
    flushed = threading.Event()

    def sync(fd):
        fsync(fd)
        flushed.set()

    def pause_replace(source, target):
        if Path(target) == config.CONFIG_PATH:
            assert Path(source).parent == config.CONFIG_PATH.parent
            assert flushed.is_set()
            assert json.loads(Path(source).read_text(encoding="utf-8"))["project_roots"] == [str(installation.projects[1])]
            ready.set()
            assert resume.wait(5)
        replace(source, target)

    monkeypatch.setattr(config.os, "fsync", sync)
    monkeypatch.setattr(config.os, "replace", pause_replace)
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(startup.set_projects, [installation.projects[1]])
        try:
            assert ready.wait(5)
            assert config.CONFIG_PATH.read_bytes() == before
            assert config.read_user_config()["project_roots"] == [str(installation.projects[0])]
        finally:
            resume.set()
        saving.result(timeout=5)
    assert config.read_user_config()["project_roots"] == [str(installation.projects[1])]
    assert not list(config.CONFIG_PATH.parent.glob(".config.json.*.tmp"))


def test_atomic_replace_failure_preserves_file_and_releases_transaction(installation, monkeypatch):
    before = config.CONFIG_PATH.read_bytes()
    replace = config.os.replace
    monkeypatch.setattr(config.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("fixture")))
    with pytest.raises(PermissionError):
        startup.set_projects([installation.projects[1]])
    assert config.CONFIG_PATH.read_bytes() == before
    assert not list(config.CONFIG_PATH.parent.glob(".config.json.*.tmp"))
    monkeypatch.setattr(config.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=1) as workers:
        workers.submit(startup.set_projects, [installation.projects[1]]).result(timeout=5)
    assert config.read_user_config()["project_roots"] == [str(installation.projects[1])]


@pytest.mark.parametrize("failure", ["credentials", "readback", "config", "receipt"])
def test_failed_model_save_keeps_concurrent_projects_and_can_retry(installation, monkeypatch, failure):
    credentials = installation.credentials
    fail = threading.Event()
    replace = config.os.replace
    atomic = onboarding.atomic_json

    def replace_or_fail(source, target):
        if fail.is_set() and failure == "config" and Path(target) == config.CONFIG_PATH:
            raise PermissionError("fixture config failure")
        replace(source, target)

    def receipt_or_fail(path, value):
        if fail.is_set() and failure == "receipt" and "model_verification" in value:
            raise PermissionError("fixture receipt failure")
        atomic(path, value)

    monkeypatch.setattr(config.os, "replace", replace_or_fail)
    monkeypatch.setattr(onboarding, "atomic_json", receipt_or_fail)
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(onboarding.save_verified_model, installation.verified)
        try:
            assert credentials.writing.wait(5)
            startup.set_projects([installation.projects[1]])
            credentials.fail_write = failure == "credentials"
            credentials.fail_read = failure == "readback"
            fail.set()
        finally:
            credentials.resume.set()
        with pytest.raises(onboarding.SetupError):
            saving.result(timeout=5)
    assert config.read_user_config()["project_roots"] == [str(installation.projects[1])]
    assert not onboarding.saved_model_verified()
    assert KEY not in config.CONFIG_PATH.read_text(encoding="utf-8")
    assert not list(config.CONFIG_PATH.parent.glob(".config.json.*.tmp"))
    credentials.fail_write = credentials.fail_read = False
    fail.clear()
    onboarding.save_verified_model(installation.verified)
    assert onboarding.saved_model_verified()
    assert config.read_user_config()["project_roots"] == [str(installation.projects[1])]


@pytest.mark.parametrize("broken", ["not JSON", "[]"])
def test_model_commit_refuses_config_corrupted_during_credentials(installation, broken):
    credentials = installation.credentials
    with ThreadPoolExecutor(max_workers=1) as workers:
        saving = workers.submit(onboarding.save_verified_model, installation.verified)
        try:
            assert credentials.writing.wait(5)
            config.CONFIG_PATH.write_text(broken, encoding="utf-8")
        finally:
            credentials.resume.set()
        with pytest.raises(onboarding.SetupError) as error:
            saving.result(timeout=5)
    assert error.value.code == "config"
    assert config.CONFIG_PATH.read_text(encoding="utf-8") == broken
    assert "model_verification" not in onboarding.read_state()
