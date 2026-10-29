"""First use, local restoration and incomplete setup: synthetic data/vault only."""
import json
import sys
import urllib.request
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure import login_startup
from bagu_trainer.application.settings import verification as onboarding
from bagu_trainer.infrastructure import secrets
from bagu_trainer.desktop import startup
from release_tests.test_first_use import KEY, installation, seed_bank, wait_ui


def fail_automatic_action(*args, **kwargs):
    raise AssertionError("Unexpected automatic network, migration or credential mutation")


@pytest.fixture
def local_only(installation, monkeypatch):
    attempts = []
    def denied(*args, **kwargs):
        attempts.append("unexpected action")
        fail_automatic_action()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    from bagu_trainer.infrastructure import migration
    monkeypatch.setattr(migration, "migrate_legacy", denied)
    monkeypatch.setattr(secrets, "save_api_key", denied)
    monkeypatch.setattr(secrets, "clear_api_key", denied)
    monkeypatch.setattr(installation.credentials, "set_password", denied)
    monkeypatch.setattr(installation.credentials, "delete_password", denied, raising=False)
    monkeypatch.setattr(login_startup, "shortcut_directory", lambda: installation.project / "Synthetic Startup")
    yield
    assert attempts == []  # Caught/discarded background errors must not hide requests or writes.


def seed_configuration(installation, *, verified=False):
    seed_bank(installation)
    installation.credentials.value = KEY
    settings = onboarding.validate_settings("https://example.invalid/v1", "synthetic-model", KEY, 15)
    configuration = {"llm": settings.public(), "project_roots": [str(installation.watched)],
                     "db_path": "data/trainer.db", "session_ingest": False, "extra": {"preserve": True}}
    config._write_config_atomic(configuration)
    if verified:
        onboarding.state_path().write_text(json.dumps({"schema_version": 1,
            "model_verification": onboarding._receipt(settings)}), encoding="utf-8")
    return configuration


def capture_data(installation):
    return {path: path.read_bytes() for path in (config.CONFIG_PATH, installation.bank, installation.database)
            if path.exists()}


def assert_preserved(before, installation):
    for path, value in before.items():
        assert path.read_bytes() == value
    assert installation.credentials.value == KEY


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk guidance")
@pytest.mark.parametrize("verified", [False, True], ids=["legacy-without-receipt", "verified-configuration"])
def test_restored_configuration_notice_and_existing_page_actions(installation, local_only, monkeypatch, verified):
    from bagu_trainer.desktop import DesktopApp
    seed_configuration(installation, verified=verified)
    before = capture_data(installation)
    opened, apps = [], []
    monkeypatch.setattr("bagu_trainer.desktop.settings_page.os.startfile", opened.append)

    def run(app):
        apps.append(app)
        app.root.withdraw()
        try:
            app._start_worker()
            wait_ui(app, lambda: app.restored_notice is not None)
            assert app.restored_message.get() == "已恢复当前 Windows 用户保存在本机的配置。"
            notice = app.restored_notice
            app.show_restored_configuration()
            assert app.restored_notice is notice
            app.status.set("监听状态发生变化")
            assert app.restored_message.get() == "已恢复当前 Windows 用户保存在本机的配置。"
            app.view_data_button.invoke()
            assert app.shell.current_page == "settings"
            settings = app._cached_pages["settings"]
            assert settings.data_dir.get() == str(config.USER_DATA_ROOT)
            from bagu_trainer.infrastructure import paths
            assert settings.install_dir.get() == str(paths.INSTALL_ROOT)
            assert not opened
            settings.open_data_button.invoke()
            assert opened == [str(config.USER_DATA_ROOT)]
            settings.model_button.invoke()
            assert app.shell.current_page == "model"
            panel = app._cached_pages["model"]
            assert panel.api_key.get() == "" and KEY not in panel.key_hint.get()
            assert "当前 Windows 用户的凭据管理器" in panel.key_hint.get()
            assert panel.save_button.instate(["disabled"]) and panel.verified is None
            assert not panel.busy and panel.task is None
            app._navigate_page("dashboard")
            app.edit_restored_button.invoke()
            assert app.shell.current_page == "model" and app._cached_pages["model"] is panel
        finally:
            app.stop.set()
            app.worker_thread.join(3)
            app.root.destroy()

    monkeypatch.setattr(DesktopApp, "run", run)
    from release_tests.fakes import LearningFake
    engine = SimpleNamespace(load_config=config.load_config, PROJECT_ROOT=installation.project,
                             get_learning_stats=lambda: {"ok": False})
    engine.learning = LearningFake(engine)
    coordinator = startup.StartupCoordinator(
        engine_loader=lambda: engine)
    coordinator.run()
    assert len(apps) == 1 and coordinator.state == "stopped"
    assert_preserved(before, installation)
    assert onboarding.state_path().exists() is verified  # Legacy config is not auto-migrated.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk guidance")
@pytest.mark.parametrize("missing", ["bank", "model", "projects"])
def test_incomplete_configuration_resumes_missing_step_and_only_prunes_deleted_projects(installation, local_only, monkeypatch, missing):
    cfg = seed_configuration(installation, verified=True)
    if missing == "bank":
        installation.bank.unlink()
    elif missing == "model":
        cfg["llm"]["model"] = ""
        config._write_config_atomic(cfg)
    else:
        cfg["project_roots"] = [str(installation.watched / "missing")]
        config._write_config_atomic(cfg)
    before = capture_data(installation)
    coordinator = startup.StartupCoordinator(engine_loader=fail_automatic_action)
    assert coordinator.prepare() is None
    if missing == "projects":
        assert config.read_user_config() == {**cfg, "project_roots": []}
        before.pop(config.CONFIG_PATH)  # Only automatic watch-list repair may write.
    assert not next(check.ready for check in coordinator.report.checks if check.name == missing)
    assert_preserved(before, installation)


@pytest.mark.parametrize("source", ["env", "keyring", "legacy", "none", "vault-error"])
def test_source_labels_follow_existing_priority_without_returning_secret(installation, monkeypatch, source, capsys):
    from bagu_trainer.desktop.model_panel import ModelPanel
    llm = {"api_key": KEY if source == "legacy" else ""}
    installation.credentials.value = KEY if source in {"env", "keyring"} else None
    if source == "env":
        monkeypatch.setenv(secrets.ENV_KEY, KEY)
    if source == "vault-error":
        monkeypatch.setattr(installation.credentials, "get_password", lambda *_: (_ for _ in ()).throw(OSError("fixture")))
    result = secrets.api_key_source(llm)
    assert result == ("none" if source == "vault-error" else source)
    assert bool(secrets.resolve_api_key(llm)) == (result != "none")
    from types import SimpleNamespace
    labels = []
    ModelPanel.show_key_source(SimpleNamespace(key_hint=SimpleNamespace(set=labels.append)), result)
    assert len(labels) == 1 and KEY not in labels[0]
    assert KEY not in "".join(capsys.readouterr())
