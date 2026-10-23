"""Settings regression: real Tk/localhost HTTP, memory-only isolated credentials."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing

import pytest

from bagu_trainer import config, onboarding, secrets, startup
from bagu_trainer.settings_window import ModelSettingsWindow, build_window
from release_tests.test_first_use import (KEY, fill_model, installation, make_wizard,
    model_http, seed_bank, wait_ui)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk settings")


@pytest.fixture
def app(installation, model_http):
    window = ModelSettingsWindow()
    window.root.withdraw()
    window.url_var.set(model_http.base_url)
    window.model_var.set("updated-local-model")
    window.key_var.set(KEY)
    window.timeout_var.set("1")
    errors = []
    window.root.report_callback_exception = lambda *args: errors.append(args)
    try:
        yield window
    finally:
        window._close()
        if not window.closed:
            wait_ui(window, lambda: window.closed)
        assert not errors


def verify_and_wait(app):
    app._test()
    wait_ui(app, lambda: not app.busy)
    assert app.verified is not None, app.status.get()


def save_and_wait(app):
    app._save()
    wait_ui(app, lambda: not app.busy)


def test_completed_wizard_edit_save_and_new_process_opens_main(installation, model_http, app, capsys):
    from bagu_trainer.store import Store
    seed_bank(installation)
    installation.database.unlink()
    store = Store(installation.database)
    store.close()
    with closing(sqlite3.connect(installation.database)) as connection:
        connection.execute("INSERT INTO attempts(quiz_id,answer,normalized_answer,correct,answered_at) VALUES ('kept','A','A',1,1)")
        connection.commit()
    extra = {"session_ingest": False, "max_pending_quizzes": 7, "custom_setting": {"keep": True}}
    config._write_config_atomic(extra)
    wizard = make_wizard(installation)
    try:
        fill_model(wizard, model_http)
        wizard._test_model()
        wait_ui(wizard, lambda: not wizard.busy)
        wizard._next()
        wait_ui(wizard, lambda: not wizard.busy)
        wizard.projects = [str(installation.watched)]
        wizard._next()
        wait_ui(wizard, lambda: not wizard.busy)
        assert wizard.step == "finish"
        wizard._next()
        wait_ui(wizard, lambda: wizard.closed)
    finally:
        if not wizard.closed:
            wizard._close()
    bank_hash = hashlib.sha256(installation.bank.read_bytes()).hexdigest()
    db_hash = hashlib.sha256(installation.database.read_bytes()).hexdigest()
    previous_receipt = onboarding.read_state()["model_verification"]
    verify_and_wait(app)
    save_and_wait(app)
    assert onboarding.saved_model_verified()
    assert onboarding.read_state()["model_verification"] != previous_receipt
    assert app.key_var.get() == ""
    assert config.read_user_config()["llm"]["model"] == "updated-local-model"
    assert all(config.read_user_config()[k] == v for k, v in extra.items())
    assert hashlib.sha256(installation.database.read_bytes()).hexdigest() == db_hash
    probe = r'''
import json, sys, urllib.request
from bagu_trainer import config, desktop, first_use, onboarding, secrets
key = sys.stdin.read()
class Credentials:
    def get_password(self, *_): return key
    def set_password(self, *_): raise AssertionError('Restart must not save credentials')
secrets._load_keyring = lambda: Credentials()
assert secrets.KEYRING_SERVICE == secrets.credential_service()
assert secrets.KEYRING_SERVICE != 'bagu-trainer'
assert onboarding.saved_model_verified()
assert config.read_user_config()['llm']['model'] == 'updated-local-model'
def forbidden(*args, **kwargs): raise AssertionError('Restart requested setup or network')
first_use.FirstUseWizard.__init__ = forbidden
urllib.request.OpenerDirector.open = forbidden
old = desktop.DesktopApp.__init__
def dashboard(app, *args, **kwargs):
    old(app, *args, **kwargs)
    app.root.withdraw()
    def finish():
        assert app.monitor.engine.get_learning_stats()['total_attempts'] == 1
        app.stop.set()
        app.root.destroy()
        print('direct-main-history-preserved')
    app.root.after(200, finish)
desktop.DesktopApp.__init__ = dashboard
assert desktop.main([]) == 0
'''
    env = dict(os.environ, BAGU_USER_DATA_DIR=str(installation.project), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.pop(secrets.ENV_KEY, None)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-B", "-c", probe], input=KEY,
        cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "direct-main-history-preserved" in result.stdout
    assert len(model_http.requests) == 2  # wizard + explicit settings test, no restart request
    assert hashlib.sha256(installation.bank.read_bytes()).hexdigest() == bank_hash
    with closing(sqlite3.connect(installation.database)) as connection:
        assert connection.execute("SELECT answer,correct FROM attempts").fetchall() == [("A", 1)]
    for path in installation.project.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".log"}:
            assert KEY not in path.read_text(encoding="utf-8")
    assert KEY not in result.stdout + result.stderr + "".join(capsys.readouterr())


@pytest.mark.parametrize("field,value", [("url_var", "http://127.0.0.1:1/v1"),
    ("model_var", "different-model"), ("key_var", "different-local-key"), ("timeout_var", "2")])
def test_edit_after_success_requires_new_test_even_if_changed_back(app, field, value):
    verify_and_wait(app)
    variable = getattr(app, field)
    old = variable.get()
    variable.set(value)
    variable.set(old)
    assert app.verified is None and app.save_button.instate(["disabled"])
    app._save()  # Guard callbacks too, not just the disabled button.
    assert not config.CONFIG_PATH.exists()
    assert not onboarding.saved_model_verified()


def test_background_thread_responsive_and_duplicate_clicks_ignored(app, model_http, monkeypatch):
    model_http.delay = 0.2
    thread_ids, beats = [], []
    actual_test = onboarding.test_connection
    def record(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        return actual_test(*args, **kwargs)
    monkeypatch.setattr(onboarding, "test_connection", record)
    main_id = threading.get_ident()
    original_status = app.status.set
    def set_status(value):
        assert threading.get_ident() == main_id
        original_status(value)
    monkeypatch.setattr(app.status, "set", set_status)
    def heartbeat():
        beats.append(1)
        if not app.closed:
            app.root.after(10, heartbeat)
    app.root.after(10, heartbeat)
    app._test()
    app._test()
    app._save()
    assert app.test_button.instate(["disabled"]) and app.save_button.instate(["disabled"])
    wait_ui(app, lambda: not app.busy)
    assert len(beats) >= 5 and thread_ids == [thread_ids[0]] and thread_ids[0] != main_id
    assert len(model_http.requests) == 1
    app._save()
    app._save()
    wait_ui(app, lambda: not app.busy)
    assert onboarding.saved_model_verified() and app.key_var.get() == ""


def test_edit_during_request_discards_result(app, model_http):
    model_http.delay = 0.15
    app._test()
    app.model_var.set("edited-during-request")
    wait_ui(app, lambda: not app.busy)
    assert app.verified is None
    app._save()
    assert not config.CONFIG_PATH.exists()


@pytest.mark.parametrize("status", [401, 404, 500])
def test_failed_test_clears_previous_success_and_allows_retry(app, model_http, status, capsys):
    verify_and_wait(app)
    model_http.status = status
    app._test()
    wait_ui(app, lambda: not app.busy)
    assert app.verified is None and str(status) in app.status.get()
    app._save()
    assert not config.CONFIG_PATH.exists() and not onboarding.saved_model_verified()
    model_http.status = 200
    verify_and_wait(app)
    save_and_wait(app)
    assert onboarding.saved_model_verified()
    assert KEY not in app.status.get() + "".join(capsys.readouterr())


@pytest.mark.parametrize("failure", ["unavailable", "write", "readback"])
def test_credential_failure_never_saves_receipt_and_retry_works(app, installation, model_http, monkeypatch, failure, capsys):
    verify_and_wait(app)
    if failure == "unavailable":
        monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    elif failure == "write":
        installation.credentials.fail_write = True
    else:
        installation.credentials.fail_read = True
    save_and_wait(app)
    assert "凭据管理器" in app.status.get()
    assert not config.CONFIG_PATH.exists() and not onboarding.saved_model_verified()
    assert not onboarding.read_state().get("model_verification")
    assert app.verified is None and app.save_button.instate(["disabled"])
    installation.credentials.fail_write = installation.credentials.fail_read = False
    monkeypatch.setattr(secrets, "_load_keyring", lambda: installation.credentials)
    verify_and_wait(app)
    save_and_wait(app)
    assert onboarding.saved_model_verified()
    assert KEY not in "".join(capsys.readouterr())


@pytest.mark.parametrize("key_source", ["vault", "environment"])
def test_blank_key_reuses_effective_credentials(app, installation, model_http, monkeypatch, key_source):
    installation.credentials.value = KEY
    if key_source == "environment":
        monkeypatch.setenv(secrets.ENV_KEY, KEY)
        installation.credentials.value = "overridden-local-key"
    app.key_var.set("")
    verify_and_wait(app)
    save_and_wait(app)
    assert app.key_var.get() == "" and onboarding.saved_model_verified()
    assert installation.credentials.value == KEY
    assert config.read_user_config()["llm"]["api_key"] == ""
    assert model_http.requests[-1][2] == "Bearer " + KEY


def test_environment_override_is_rejected_before_network(app, model_http, monkeypatch):
    monkeypatch.setenv(secrets.ENV_KEY, "higher-priority-local-key")
    app._test()
    wait_ui(app, lambda: not app.busy)
    assert app.verified is None and "环境变量" in app.status.get()
    assert not model_http.requests and not config.CONFIG_PATH.exists()


@pytest.mark.parametrize("key_source", ["vault", "environment"])
def test_effective_blank_key_changed_since_test_requires_retest(app, installation, monkeypatch, key_source):
    installation.credentials.value = KEY
    app.key_var.set("")
    verify_and_wait(app)
    if key_source == "environment":
        monkeypatch.setenv(secrets.ENV_KEY, "changed-local-key")
    else:
        installation.credentials.value = "changed-local-key"
    save_and_wait(app)
    assert app.verified is None and "重新测试" in app.status.get()
    assert not config.CONFIG_PATH.exists() and not onboarding.saved_model_verified()


@pytest.mark.parametrize("timeout", ["bad", "0", "nan", "121"])
def test_invalid_timeout_fails_without_network(app, model_http, timeout):
    app.timeout_var.set(timeout)
    app._test()
    wait_ui(app, lambda: not app.busy)
    assert app.verified is None and "超时" in app.status.get()
    assert not model_http.requests and not config.CONFIG_PATH.exists()


def test_network_timeout_and_retry(app, model_http):
    app.timeout_var.set("0.03")
    model_http.delay = 0.15
    app._test()
    wait_ui(app, lambda: not app.busy)
    assert app.verified is None and "超时" in app.status.get()
    assert not config.CONFIG_PATH.exists()
    model_http.delay = 0
    app.timeout_var.set("1")
    verify_and_wait(app)
    save_and_wait(app)
    assert onboarding.saved_model_verified()


def test_close_during_request_never_touches_destroyed_tk_or_saves(app, model_http):
    model_http.delay = 0.2
    app._test()
    deadline = time.monotonic() + 2
    while not model_http.requests and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert model_http.requests
    start = time.monotonic()
    app._close()
    assert app.closed and time.monotonic() - start < 0.15
    assert app.cancel.is_set() and app.key_var.get() == ""
    app.worker.join(timeout=2)
    assert not app.worker.is_alive()
    assert not config.CONFIG_PATH.exists() and app.verified is None


def test_close_while_saving_completes_write_then_closes(app, monkeypatch):
    verify_and_wait(app)
    started, release = threading.Event(), threading.Event()
    actual_save = onboarding.save_verified_model
    def delayed_save(verified):
        started.set()
        assert release.wait(2)
        actual_save(verified)
    monkeypatch.setattr(onboarding, "save_verified_model", delayed_save)
    app._save()
    assert started.wait(2)
    app._close()
    assert app.closing and not app.closed
    release.set()
    wait_ui(app, lambda: app.closed)
    assert onboarding.saved_model_verified() and app.key_var.get() == ""


def test_legacy_build_window_returns_tk_and_external_destroy_cancels(installation):
    root = build_window()
    root.withdraw()
    root.update_idletasks()
    root.destroy()

def test_reopened_settings_never_prefills_saved_key_and_can_reverify(installation, model_http, app):
    verify_and_wait(app)
    save_and_wait(app)
    reopened = ModelSettingsWindow(parent=app.root)
    reopened.root.withdraw()
    try:
        assert reopened.key_var.get() == ""
        assert reopened.model_var.get() == "updated-local-model"
        reopened.model_var.set("another-local-model")
        verify_and_wait(reopened)
        save_and_wait(reopened)
        assert onboarding.saved_model_verified() and reopened.key_var.get() == ""
        assert config.read_user_config()["llm"]["model"] == "another-local-model"
    finally:
        reopened._close()
    assert app.root.winfo_exists()


def test_receipt_write_failure_does_not_claim_saved_verification(app, monkeypatch):
    onboarding.begin()
    verify_and_wait(app)
    write = onboarding.atomic_json
    def fail(*_):
        raise PermissionError()
    monkeypatch.setattr(onboarding, "atomic_json", fail)
    save_and_wait(app)
    assert app.verified is None and "保存失败" in app.status.get()
    assert not onboarding.saved_model_verified()
    assert config.read_user_config()["llm"]["api_key"] == ""
    monkeypatch.setattr(onboarding, "atomic_json", write)
    verify_and_wait(app)
    save_and_wait(app)
    assert onboarding.saved_model_verified()
