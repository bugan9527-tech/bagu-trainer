"""Startup tests are independent of the user's bank, config, keyring and DB."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure import secrets
from bagu_trainer.desktop import startup


QUESTION = {"id": "test-1", "knowledge_point": "缓存", "question": "缓存失效？",
            "tags": ["cache"], "keywords": ["redis"], "difficulty": "easy",
            "options": {}, "answer": "", "analysis": ""}


@pytest.fixture
def install(tmp_path, monkeypatch):
    project = tmp_path / "训练器 安装"
    project.mkdir()
    (project / "data").mkdir()
    watched = tmp_path / "监听 项目"
    watched.mkdir()
    bank = project / "data" / "questions.json"
    bank.write_text(json.dumps([QUESTION]), encoding="utf-8")
    settings = project / "config.json"
    configuration = {"project_roots": [str(watched)], "db_path": "data/original.db",
                     "session_ingest": False, "custom_setting": {"keep": 1},
                     "llm": {"base_url": "https://example.invalid/v1", "model": "test",
                             "timeout_sec": 30, "api_key": ""}}
    settings.write_text(json.dumps(configuration), encoding="utf-8")
    database = project / "data" / "original.db"
    database.write_bytes(b"existing learning records must stay unchanged")
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.setattr(config, "USER_DATA_ROOT", project)
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(project))
    monkeypatch.setattr(config, "CONFIG_PATH", settings)
    monkeypatch.delenv(secrets.ENV_KEY, raising=False)
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    monkeypatch.setenv(secrets.ENV_KEY, "test-only-key")
    return SimpleNamespace(project=project, watched=watched, bank=bank,
                           settings=settings, configuration=configuration, database=database)


def write_configuration(install, configuration):
    install.settings.write_text(json.dumps(configuration), encoding="utf-8")


def mark_model_tested(install):
    """Isolate startup/engine retry tests from HTTP; HTTP is tested in test_first_use."""
    from bagu_trainer.application.settings import verification as onboarding
    from bagu_trainer.infrastructure.bank_storage import atomic_json
    llm = install.configuration["llm"]
    settings = onboarding.validate_settings(llm["base_url"], llm["model"], "test-only-key", llm["timeout_sec"])
    atomic_json(onboarding.state_path(), {"schema_version": 1, "model_verification": onboarding._receipt(settings)})


def checks():
    return {item.name: item.ready for item in startup.check_readiness().checks}


def test_ready_old_install_is_read_only_and_loads_engine_only_after_check(install):
    original = {p: p.read_bytes() for p in (install.bank, install.settings, install.database)}
    order = []
    engine = object()
    def checker():
        order.append("check")
        return startup.check_readiness()
    coordinator = startup.StartupCoordinator(checker, lambda: order.append("engine") or engine)
    assert coordinator.prepare() is engine
    assert order == ["check", "engine"]
    assert coordinator.report.ready
    assert {p: p.read_bytes() for p in original} == original
    assert not (install.project / "data" / "trainer.db").exists()


def test_first_launch_missing_all_inputs_does_not_load_engine(install):
    install.bank.unlink()
    install.settings.unlink()
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("engine loaded too early"))
    assert coordinator.prepare() is None
    assert coordinator.state == "initialization"
    assert checks() == {"bank": False, "model": False, "projects": False}
    assert not install.bank.exists() and not install.settings.exists()


@pytest.mark.parametrize("payload", ["[]", "{}", "null", "broken", '[{"id":"only-id"}]',
                                        json.dumps([QUESTION, QUESTION])])
def test_empty_invalid_or_duplicate_bank_blocks_engine(install, payload):
    install.bank.write_text(payload, encoding="utf-8")
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("engine loaded"))
    assert coordinator.prepare() is None
    assert checks() == {"bank": False, "model": True, "projects": True}


@pytest.mark.parametrize("change", [{"llm": None}, {"llm": {}},
    {"project_roots": []}, {"project_roots": "wrong"}, {"project_roots": [""]},
    {"project_roots": ["relative-project"]}, {"project_roots": [None]}])
def test_partial_configuration_is_not_hidden_by_defaults(install, change):
    write_configuration(install, {**install.configuration, **change})
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("engine loaded"))
    assert coordinator.prepare() is None
    assert not coordinator.report.ready


@pytest.mark.parametrize("change", [{"base_url": ""}, {"base_url": "file:///test"},
    {"base_url": "https://user:pass@example.invalid"}, {"base_url": None},
    {"model": " "}, {"model": 1}, {"timeout_sec": 0}, {"timeout_sec": -1},
    {"timeout_sec": "30"}, {"timeout_sec": True}, {"timeout_sec": float("inf")}])
def test_invalid_model_configuration(install, change):
    configuration = {**install.configuration, "llm": {**install.configuration["llm"], **change}}
    write_configuration(install, configuration)
    assert not checks()["model"]


def test_key_resolution_uses_existing_env_keyring_and_legacy_sources(install, monkeypatch):
    assert checks()["model"]
    monkeypatch.delenv(secrets.ENV_KEY)
    assert not checks()["model"]
    monkeypatch.setattr(secrets, "_load_keyring", lambda: SimpleNamespace(get_password=lambda *_: "stored-key"))
    assert checks()["model"]
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    install.configuration["llm"]["api_key"] = "legacy-test-key"
    write_configuration(install, install.configuration)
    assert checks()["model"]
    assert "legacy-test-key" not in repr(startup.check_readiness())
    assert json.loads(install.settings.read_text(encoding="utf-8"))["llm"]["api_key"] == "legacy-test-key"


@pytest.mark.parametrize("payload", ["broken", "[]", "null"])
def test_corrupt_config_remains_untouched_on_check_and_project_save(install, payload):
    install.settings.write_text(payload, encoding="utf-8")
    assert checks() == {"bank": True, "model": False, "projects": False}
    with pytest.raises(ValueError):
        startup.set_projects([install.watched])
    assert install.settings.read_text(encoding="utf-8") == payload


def test_project_repair_preserves_other_configuration_and_data(install):
    configuration = {**install.configuration, "project_roots": [str(install.project / "missing")]}
    write_configuration(install, configuration)
    original_bank, original_db = install.bank.read_bytes(), install.database.read_bytes()
    assert not checks()["projects"]
    startup.set_projects([install.watched, install.watched])
    assert startup.check_readiness().ready
    assert json.loads(install.settings.read_text(encoding="utf-8")) == install.configuration
    assert install.bank.read_bytes() == original_bank
    assert install.database.read_bytes() == original_db


def test_model_save_does_not_complete_project_selection_implicitly(install):
    install.settings.unlink()
    config.update_llm(install.configuration["llm"])
    assert checks() == {"bank": True, "model": True, "projects": False}
    configuration = {**install.configuration, "project_roots": [], "custom_setting": False}
    write_configuration(install, configuration)
    config.update_llm(configuration["llm"])
    assert json.loads(install.settings.read_text(encoding="utf-8")) == configuration
    assert not checks()["projects"]


def test_model_save_refuses_corrupt_config_before_touching_credentials(install, monkeypatch):
    install.settings.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(secrets, "save_api_key", lambda *_: pytest.fail("credential changed"))
    with pytest.raises(ValueError):
        config.update_llm({**install.configuration["llm"], "api_key": "new-test-key"})
    assert install.settings.read_text(encoding="utf-8") == "broken"


def test_every_watched_project_must_be_available(install):
    install.configuration["project_roots"].append(str(install.bank))  # File, not directory.
    write_configuration(install, install.configuration)
    assert not checks()["projects"]


def test_engine_failure_retry_and_restart(install):
    attempts = []
    engine = object()
    def load_engine():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("secret connection detail test-only-key")
        return engine
    coordinator = startup.StartupCoordinator(engine_loader=load_engine)
    assert coordinator.prepare() is None
    assert coordinator.state == "failed"
    assert "RuntimeError" in coordinator.report.error
    assert "test-only-key" not in coordinator.report.error
    assert coordinator.prepare() is engine
    assert coordinator.state == "ready" and coordinator.report.ready
    # No completion flag required on a new process/coordinator.
    assert startup.StartupCoordinator(engine_loader=lambda: engine).prepare() is engine


def test_retry_rechecks_files_instead_of_trusting_previous_success(install):
    engine = object()
    coordinator = startup.StartupCoordinator(engine_loader=lambda: engine)
    assert coordinator.prepare() is engine
    install.bank.unlink()
    assert coordinator.prepare() is None
    install.bank.write_text(json.dumps([QUESTION]), encoding="utf-8")
    assert coordinator.prepare() is engine


def test_initialization_window_commands_before_engine_load(install):
    coordinator = startup.StartupCoordinator()
    assert coordinator.window_command("_show_dashboard", {}) == {"ok": True}
    assert coordinator.events.get_nowait() == ("show", "dashboard")
    status = coordinator.window_command("_desktop_status", {})
    assert status["startup_state"] == "checking"
    assert not coordinator.window_command("submit_answer", {})["ok"]


def wait_for_ui(app, predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert predicate()


def test_import_and_missing_bank_probe_never_import_engine(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "isolated"
    shutil.copytree(source / "bagu_trainer", project / "bagu_trainer",
                    ignore=shutil.ignore_patterns("__pycache__"))
    probe = """
import sys
class BlockEngine:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'bagu_trainer.application.learning.engine':
            raise AssertionError('engine loaded before readiness')
sys.meta_path.insert(0, BlockEngine())
from bagu_trainer.desktop import main
from bagu_trainer.desktop.startup import StartupCoordinator
c = StartupCoordinator()
assert c.prepare() is None
assert c.state == 'initialization'
assert 'bagu_trainer.application.learning.engine' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(project)},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe and Tk integration")
def test_entrypoint_first_run_failure_repair_restart_and_single_instance(tmp_path):
    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "桌面 启动"
    for directory in ("bagu_trainer", "scripts"):
        shutil.copytree(source / directory, project / directory,
                        ignore=shutil.ignore_patterns("__pycache__"))
    # Execute in a clean process so import order and single-instance behavior are real.
    probe = r'''
import json
import sys
import time
from pathlib import Path
project = Path(sys.argv[1])
sys.path.insert(0, str(project))
from bagu_trainer import desktop
from bagu_trainer.infrastructure import desktop_instance
from bagu_trainer.desktop import startup
assert 'bagu_trainer.application.learning.engine' not in sys.modules
loads, windows, failures = [], [], []
original_init = startup.StartupCoordinator.__init__
def coordinator_init(self):
    original_init(self)
    loader = self.engine_loader
    def load():
        loads.append(1)
        if len(loads) == 1:
            raise RuntimeError('simulated initialization failure')
        return loader()
    self.engine_loader = load
startup.StartupCoordinator.__init__ = coordinator_init
original_app = desktop.DesktopApp.__init__
def app_init(self, *args, **kwargs):
    original_app(self, *args, **kwargs)
    windows.append('main')
    stage = [0]
    deadline = time.monotonic() + 10
    def step():
        try:
            assert time.monotonic() < deadline, self.status.get()
            c = self.coordinator
            if not c.report.checks:
                self.root.after(20, step)
                return
            assert desktop.main([]) == 0
            assert len(windows) == (1 if len(loads) < 2 else len(windows))
            status = desktop_instance.remote_call('_desktop_status', {})
            assert status['startup_state'] == 'running'
            if stage[0] == 0 and len(windows) == 1:
                assert not self.ready and not loads
                (project / 'data').mkdir(exist_ok=True)
                (project / 'data/questions.json').write_text(json.dumps([{
                    'id':'test-1', 'knowledge_point':'cache', 'question':'cache?',
                    'tags':['cache'], 'keywords':['redis'], 'difficulty':'easy'}]))
                (project / 'config.json').write_text(json.dumps({
                    'project_roots':[str(project)], 'db_path':'data/learning.db',
                    'llm':{'base_url':'https://example.invalid/v1','model':'test'}}))
                self.jobs.put(('prepare', None))
                stage[0] = 1
            elif stage[0] == 1 and c.report.error:
                if 'RuntimeError' not in self.readiness_text.get():
                    self.root.after(20, step)
                    return
                assert 'RuntimeError' in self.readiness_text.get()
                assert 'bagu_trainer.application.learning.engine' not in sys.modules
                self.jobs.put(('prepare', None))
                stage[0] = 2
            elif (stage[0] == 2 or len(windows) > 1) and self.ready:
                assert self.monitor.engine is not None
                assert desktop.main(['--configure']) == 0
                self.events.put(('show','model'))
                self.root.after(250, self._close)
                return
            self.root.after(20, step)
        except Exception as error:
            failures.append(repr(error))
            self._close()
    self.root.after(50, step)
desktop.DesktopApp.__init__ = app_init
assert desktop.main(['--background']) == 0
assert not failures, failures
assert windows == ['main'] and len(loads) == 2
assert not (project / 'runtime/bridge-credentials.json').exists()
saved = (project / 'config.json').read_bytes()
bank = (project / 'data/questions.json').read_bytes()
assert desktop.main([]) == 0
assert windows == ['main','main'] and len(loads) == 3
assert (project / 'config.json').read_bytes() == saved
assert (project / 'data/questions.json').read_bytes() == bank
assert not failures, failures
print('first run, retry, restart and singleton passed')
'''
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                   "BAGU_LLM_API_KEY": "test-key", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
                   "BAGU_USER_DATA_DIR": str(project)}
    result = subprocess.run([sys.executable, "-B", "-c", probe, str(project)],
                            cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "first run, retry, restart and singleton passed" in result.stdout


def test_entrypoint_releases_owner_when_setup_closes_or_startup_raises(monkeypatch):
    from bagu_trainer import desktop
    from bagu_trainer.infrastructure import desktop_instance
    calls = []
    class Owner:
        def acquire(self):
            calls.append("lock")
            return True
        def close(self):
            calls.append("unlock")
    class Broker:
        def __init__(self, dispatch, owner):
            self.owner = owner
        def start(self):
            calls.append("broker")
        def close(self):
            self.owner.close()
    class CancelledStartup:
        def window_command(self, *_):
            return {"ok": True}
        def run(self, background, page="dashboard"):
            calls.append("setup closed")
    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setattr(desktop_instance, "OwnerLock", Owner)
    monkeypatch.setattr(desktop_instance, "WindowBroker", Broker)
    monkeypatch.setattr(startup, "StartupCoordinator", CancelledStartup)
    assert desktop.main([]) == 0
    assert calls == ["lock", "broker", "setup closed", "unlock"]
    def fail(_self, _background, page="dashboard"):
        raise RuntimeError("startup failed")
    monkeypatch.setattr(CancelledStartup, "run", fail)
    with pytest.raises(RuntimeError):
        desktop.main([])
    assert calls[-1] == "unlock"
