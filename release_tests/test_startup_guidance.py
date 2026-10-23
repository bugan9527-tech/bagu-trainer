"""First use, local restoration and incomplete setup: synthetic data/vault only."""
import json
import sys
import urllib.request

import pytest

from bagu_trainer import config, login_startup, onboarding, secrets, startup
from release_tests.test_first_use import (
    KEY, fill_model, installation, make_wizard, model_http, seed_bank, wait_ui,
)


def fail_automatic_action(*args, **kwargs):
    raise AssertionError("Unexpected automatic network, migration or credential mutation")


@pytest.fixture
def local_only(installation, monkeypatch):
    attempts = []
    def denied(*args, **kwargs):
        attempts.append("unexpected action")
        fail_automatic_action()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    from bagu_trainer import migration
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
def test_new_user_reuses_four_steps_and_only_explicit_test_sends_request(installation, model_http, monkeypatch):
    installation.database.unlink()  # Start with no personal configuration, bank or learning data.
    app = make_wizard(installation)
    app.model_opener = urllib.request.build_opener().open
    try:
        assert app.step == "bank" and not config.CONFIG_PATH.exists()
        assert list(app.TITLES.values()) == ["准备题库", "配置模型", "添加监听项目", "开始使用"]
        assert not model_http.requests and app.api_key.get() == ""
        assert "未配置" in app.key_hint.get()
        # A completed synthetic build feeds the existing bank success callback.
        seed_bank(installation)
        app._bank_ready(None)
        wait_ui(app, lambda: not app.busy)
        assert app.step == "model" and not model_http.requests
        assert app.start_button.instate(["disabled"])
        fill_model(app, model_http)
        app._test_model()
        wait_ui(app, lambda: not app.busy)
        assert len(model_http.requests) == 1 and app.verified is not None
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "projects" and app.api_key.get() == ""
        assert "当前 Windows 用户的凭据管理器" in app.key_hint.get()
        app.projects = [str(installation.watched)]
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "finish"
        assert len(model_http.requests) == 1
        app._next()
        wait_ui(app, lambda: app.closed)
        assert app.engine is not None
        assert not installation.database.exists()  # UI never creates/clears a learning DB.
        assert KEY not in config.CONFIG_PATH.read_text(encoding="utf-8")
    finally:
        if not app.closed:
            app._close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk guidance")
@pytest.mark.parametrize("verified", [False, True], ids=["legacy-without-receipt", "verified-configuration"])
def test_restored_configuration_notice_and_existing_page_actions(installation, local_only, monkeypatch, verified):
    from bagu_trainer.desktop import DesktopApp
    seed_configuration(installation, verified=verified)
    before = capture_data(installation)
    from bagu_trainer.first_use import FirstUseWizard
    monkeypatch.setattr(FirstUseWizard, "__init__", fail_automatic_action)
    opened, apps = [], []
    monkeypatch.setattr("bagu_trainer.settings_page.os.startfile", opened.append)

    def run(app):
        apps.append(app)
        app.root.withdraw()
        try:
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
            from bagu_trainer import paths
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
            app.root.destroy()

    monkeypatch.setattr(DesktopApp, "run", run)
    coordinator = startup.StartupCoordinator(engine_loader=lambda: object())
    coordinator.run()
    assert len(apps) == 1 and coordinator.state == "stopped"
    assert_preserved(before, installation)
    assert onboarding.state_path().exists() is verified  # Legacy config is not auto-migrated.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk guidance")
@pytest.mark.parametrize("missing", ["bank", "model", "projects"])
def test_incomplete_configuration_resumes_first_missing_step_without_writes(installation, local_only, monkeypatch, missing):
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
    from bagu_trainer.first_use import FirstUseWizard
    app = FirstUseWizard(coordinator)
    try:
        app.root.withdraw()
        wait_ui(app, lambda: not app.busy)
        assert app.step == missing
        assert app.api_key.get() == ""
        assert KEY not in app.key_hint.get() and KEY not in repr(onboarding.snapshot(coordinator.report))
        assert "当前 Windows 用户的凭据管理器" in app.key_hint.get()
        assert app.engine is None
        assert_preserved(before, installation)
    finally:
        app._close()


@pytest.mark.parametrize("source", ["env", "keyring", "legacy", "none", "vault-error"])
def test_source_labels_follow_existing_priority_without_returning_secret(installation, monkeypatch, source, capsys):
    from bagu_trainer.model_panel import ModelPanel
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
