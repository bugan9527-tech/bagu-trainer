"""First-use wizard integration: localhost HTTP and an in-memory credential store."""
import json
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bagu_trainer import config, onboarding, secrets, startup
from bagu_trainer.bank_build.service import BankBuildService
from release_tests.test_bank_build import COMMIT, REPOSITORY, http_archive


KEY = "sk-local-wizard-test-key-37ef9a"
QUESTION = {"id": "local-test", "knowledge_point": "缓存", "question": "缓存如何失效？",
            "tags": ["cache"], "keywords": ["redis"], "difficulty": "easy"}


class MemoryCredentials:
    def __init__(self):
        self.value = None
        self.fail_write = False
        self.fail_read = False
    def get_password(self, *_):
        return None if self.fail_read else self.value
    def set_password(self, _service, _user, value):
        if self.fail_write:
            raise RuntimeError("credential failure " + KEY)
        self.value = value


@pytest.fixture
def installation(tmp_path, monkeypatch):
    project = tmp_path / "首次使用 测试"
    (project / "data").mkdir(parents=True)
    sources = project / "interview-banks" / "sources.json"
    sources.parent.mkdir()
    sources.write_text(json.dumps({"sources": [{"repository": REPOSITORY, "directory": "JavaGuide",
        "audited_commit": COMMIT, "observed_license": "Apache-2.0"}]}), encoding="utf-8")
    watched = tmp_path / "监听代码"
    watched.mkdir()
    database = project / "data" / "trainer.db"
    database.write_bytes(b"existing learning database")
    credentials = MemoryCredentials()
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.setattr(config, "USER_DATA_ROOT", project)
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(project))
    monkeypatch.setattr(config, "CONFIG_PATH", project / "config.json")
    monkeypatch.setattr(secrets, "_load_keyring", lambda: credentials)
    monkeypatch.delenv(secrets.ENV_KEY, raising=False)
    return SimpleNamespace(project=project, bank=project / "data" / "questions.json", watched=watched,
                           database=database, credentials=credentials)


@pytest.fixture
def model_http():
    state = SimpleNamespace(status=200, delay=0, requests=[], redirected=0,
        body=json.dumps({"choices": [{"message": {"content": "OK " + KEY}}]}).encode())
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, payload, self.headers.get("Authorization")))
            if state.delay:
                time.sleep(state.delay)
            self.send_response(state.status, "reflected " + KEY)
            if 300 <= state.status < 400:
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirected")
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            try:
                self.wfile.write(state.body)
            except (BrokenPipeError, ConnectionError):
                pass
        def do_GET(self):
            state.redirected += 1
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings(server, timeout=1):
    return onboarding.validate_settings(server.base_url, "local-model", KEY, timeout)


def seed_bank(installation):
    installation.bank.write_text(json.dumps([QUESTION]), encoding="utf-8")


def verify_and_save(server):
    verified = onboarding.test_connection(settings(server))
    onboarding.save_verified_model(verified)
    return verified


def test_explicit_connection_save_and_restore_without_plaintext_key(installation, model_http, capsys):
    seed_bank(installation)
    onboarding.begin()
    assert not model_http.requests
    verified = verify_and_save(model_http)
    assert KEY not in repr(verified)
    assert installation.credentials.value == KEY
    cfg = config.read_user_config()
    assert cfg["llm"]["api_key"] == ""
    assert onboarding.saved_model_verified()
    report = startup.check_readiness()
    assert onboarding.snapshot(report).step == "projects"
    startup.set_projects([installation.watched])
    assert onboarding.snapshot(startup.check_readiness()).step == "finish"
    assert installation.database.read_bytes() == b"existing learning database"
    assert len(model_http.requests) == 1
    path, payload, authorization = model_http.requests[0]
    assert path == "/v1/chat/completions" and payload["model"] == "local-model"
    assert authorization == "Bearer " + KEY
    for file in (config.CONFIG_PATH, onboarding.state_path()):
        assert KEY not in file.read_text(encoding="utf-8")
    assert KEY not in "".join(capsys.readouterr())


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_http_failure_is_actionable_and_never_echoes_headers_reason_or_body(installation, model_http, status, capsys):
    model_http.status = status
    with pytest.raises(onboarding.SetupError) as error:
        onboarding.test_connection(settings(model_http))
    assert str(status) in str(error.value)
    assert "请" in str(error.value)
    assert KEY not in str(error.value)
    assert not onboarding.saved_model_verified()
    assert not config.CONFIG_PATH.exists()
    assert model_http.redirected == 0
    assert KEY not in "".join(capsys.readouterr())


@pytest.mark.parametrize("body", [b"not JSON", b"{}", b"[]", b'{"choices":[]}',
                                  b'{"choices":[{"message":"invalid"}]}', b"x" * 65537],
                         ids=["not-json", "no-choices", "array", "empty-choices", "invalid-message", "oversized"])
def test_invalid_response_is_not_saved(installation, model_http, body):
    model_http.body = body
    with pytest.raises(onboarding.SetupError) as error:
        onboarding.test_connection(settings(model_http))
    assert error.value.code == "response"
    assert not config.CONFIG_PATH.exists()


def test_model_timeout_and_cancellation(installation, model_http):
    model_http.delay = 0.2
    with pytest.raises(onboarding.SetupError) as error:
        onboarding.test_connection(settings(model_http, 0.03))
    assert error.value.code == "timeout"
    token = threading.Event()
    timer = threading.Timer(0.03, token.set)
    timer.start()
    try:
        with pytest.raises(onboarding.SetupError) as error:
            onboarding.test_connection(settings(model_http), cancel=token)
        assert error.value.code == "cancelled"
    finally:
        timer.join()


@pytest.mark.parametrize("failure", ["unavailable", "write", "readback"])
def test_credential_failure_cannot_complete_model_step(installation, model_http, monkeypatch, capsys, failure):
    onboarding.begin()
    verified = onboarding.test_connection(settings(model_http))
    if failure == "unavailable":
        monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    elif failure == "write":
        installation.credentials.fail_write = True
    else:
        installation.credentials.fail_read = True
    with pytest.raises(onboarding.SetupError, match="凭据管理器"):
        onboarding.save_verified_model(verified)
    assert not config.CONFIG_PATH.exists()
    assert not onboarding.saved_model_verified()
    assert KEY not in "".join(capsys.readouterr())


def test_modified_model_or_effective_key_requires_retesting(installation, model_http, monkeypatch):
    verify_and_save(model_http)
    assert onboarding.saved_model_verified()
    cfg = config.read_user_config()
    cfg["llm"]["model"] = "different-model"
    config._write_config_atomic(cfg)
    assert not onboarding.saved_model_verified()
    assert not next(check for check in startup.check_readiness().checks if check.name == "model").ready
    monkeypatch.setenv(secrets.ENV_KEY, "different-environment-key")
    with pytest.raises(onboarding.SetupError, match="环境变量"):
        onboarding.effective_settings(model_http.base_url, "local-model", KEY)


def test_verification_receipt_is_bound_to_tested_values(installation, model_http):
    verified = onboarding.test_connection(settings(model_http))
    changed = replace(verified, settings=replace(verified.settings, model="untested-model"))
    with pytest.raises(onboarding.SetupError, match="重新测试"):
        onboarding.save_verified_model(changed)
    assert not config.CONFIG_PATH.exists()


def test_failed_progress_write_requires_retry_and_preserves_credentials(installation, model_http, monkeypatch):
    onboarding.begin()
    verified = onboarding.test_connection(settings(model_http))
    real_write = onboarding.atomic_json
    monkeypatch.setattr(onboarding, "atomic_json", lambda *_: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(onboarding.SetupError, match="再次保存"):
        onboarding.save_verified_model(verified)
    assert installation.credentials.value == KEY
    assert not onboarding.saved_model_verified()
    monkeypatch.setattr(onboarding, "atomic_json", real_write)
    onboarding.save_verified_model(verified)
    assert onboarding.saved_model_verified()


@pytest.mark.parametrize("url,model,key,timeout", [("bad", "model", KEY, 1),
    ("https://example.invalid/v1/chat/completions", "model", KEY, 1),
    ("https://user:password@example.invalid", "model", KEY, 1),
    ("https://example.invalid", "", KEY, 1), ("https://example.invalid", "model", "", 1),
    ("https://example.invalid", "model", KEY, float("nan"))])
def test_invalid_fields_fail_before_network(url, model, key, timeout):
    with pytest.raises(onboarding.SetupError):
        onboarding.validate_settings(url, model, key, timeout)


def wait_ui(app, predicate):
    deadline = time.monotonic() + 8
    while not predicate() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert predicate()


def make_wizard(installation, http_archive=None, engine_loader=None):
    from bagu_trainer.initialization import InitializationApp
    coordinator = startup.StartupCoordinator(engine_loader=engine_loader or (lambda: object()))
    coordinator.prepare()
    service = BankBuildService(installation.project, opener=http_archive.opener) if http_archive else None
    app = InitializationApp(coordinator, bank_service=service)
    app.root.withdraw()
    wait_ui(app, lambda: not app.busy)
    return app


def fill_model(app, model_http):
    app.base_url.set(model_http.base_url)
    app.model_name.set("local-model")
    app.api_key.set(KEY)
    app.timeout.set("1")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_entire_wizard_build_test_save_projects_and_engine_handoff(installation, http_archive, model_http, capsys):
    engine, loads = object(), []
    app = make_wizard(installation, http_archive, lambda: loads.append(1) or engine)
    try:
        assert app.step == "bank" and not model_http.requests and not loads
        beats = []
        def heartbeat():
            beats.append(1)
            if not app.closed:
                app.root.after(15, heartbeat)
        app.root.after(15, heartbeat)
        http_archive.delay = 0.15
        app.bank_setup.source_list.selection_set(REPOSITORY)
        app.bank_setup._start()
        wait_ui(app, lambda: app.step == "model" and not app.busy)
        assert len(beats) >= 3
        assert not model_http.requests
        fill_model(app, model_http)
        app._next()
        assert not config.CONFIG_PATH.exists()
        model_http.delay = 0.15
        before = len(beats)
        app._test_model()
        assert app.start_button.instate(["disabled"])
        wait_ui(app, lambda: not app.busy)
        assert len(beats) - before >= 3
        assert app.verified is not None and not loads
        assert KEY not in app.status.get()
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "projects" and app.api_key.get() == ""
        assert installation.credentials.value == KEY
        assert onboarding.saved_model_verified()
        app.projects = [str(installation.watched)]
        app._render_projects()
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "finish" and not loads
        app._next()
        wait_ui(app, lambda: app.closed)
        assert app.engine is engine and loads == [1]
        assert len(model_http.requests) == 1
        assert installation.database.read_bytes() == b"existing learning database"
        assert KEY not in config.CONFIG_PATH.read_text(encoding="utf-8")
        assert KEY not in "".join(capsys.readouterr())
    finally:
        if not app.closed:
            app._close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_completed_steps_resume_and_missing_credentials_reopen_model_step(installation, model_http):
    seed_bank(installation)
    app = make_wizard(installation)
    assert app.step == "model"
    fill_model(app, model_http)
    app._test_model()
    wait_ui(app, lambda: not app.busy)
    app._next()
    wait_ui(app, lambda: not app.busy)
    assert app.step == "projects"
    app._close()
    resumed = make_wizard(installation)
    try:
        assert resumed.step == "projects"
        assert resumed.base_url.get() == model_http.base_url
        assert resumed.model_name.get() == "local-model"
        assert resumed.api_key.get() == ""
        assert len(model_http.requests) == 1  # Restoring does not test automatically.
    finally:
        resumed._close()
    installation.credentials.value = None
    missing = make_wizard(installation)
    try:
        assert missing.step == "model"
        assert len(model_http.requests) == 1
    finally:
        missing._close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_http_and_credential_failures_remain_retryable_in_wizard(installation, model_http):
    seed_bank(installation)
    app = make_wizard(installation)
    try:
        fill_model(app, model_http)
        model_http.status = 401
        app._test_model()
        wait_ui(app, lambda: not app.busy)
        assert "401" in app.status.get() and KEY not in app.status.get()
        assert app.verified is None and not app.test_button.instate(["disabled"])
        model_http.status = 200
        app._test_model()
        wait_ui(app, lambda: not app.busy)
        installation.credentials.fail_write = True
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "model" and "凭据管理器" in app.status.get()
        assert not config.CONFIG_PATH.exists()
        installation.credentials.fail_write = False
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "projects" and len(model_http.requests) == 2
    finally:
        app._close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_cancellation_and_editing_cannot_save_a_stale_test_result(installation, model_http):
    seed_bank(installation)
    app = make_wizard(installation)
    try:
        fill_model(app, model_http)
        model_http.delay = 0.15
        app._test_model()
        app._cancel_test()
        wait_ui(app, lambda: not app.busy)
        assert app.verified is None
        app._test_model()
        app.model_name.set("changed-during-test")
        wait_ui(app, lambda: not app.busy)
        assert app.verified is None
        app._next()
        assert not config.CONFIG_PATH.exists()
        assert app.step == "model"
    finally:
        app._close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_restore_failure_is_actionable_and_retryable(installation):
    onboarding.state_path().write_text("broken", encoding="utf-8")
    app = make_wizard(installation)
    try:
        assert app.step == "loading" and "onboarding.json" in app.status.get()
        onboarding.state_path().unlink()
        app._next()
        wait_ui(app, lambda: not app.busy)
        assert app.step == "bank"
    finally:
        app._close()


def test_first_use_gate_cannot_be_skipped_by_restarting_after_bank_build(installation, model_http):
    seed_bank(installation)
    installation.credentials.value = KEY
    config._write_config_atomic({"llm": settings(model_http).public(),
                                 "project_roots": [str(installation.watched)]})
    # Legacy users with complete config still go directly to main.
    assert startup.check_readiness().ready
    onboarding.begin()
    assert not startup.check_readiness().ready
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("unverified model was loaded"))
    assert coordinator.prepare() is None
    assert onboarding.snapshot(coordinator.report).step == "model"
    assert not model_http.requests
    verify_and_save(model_http)
    assert startup.check_readiness().ready


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_exit_during_connection_test_waits_without_opening_main(installation, model_http):
    seed_bank(installation)
    app = make_wizard(installation)
    fill_model(app, model_http)
    model_http.delay = 0.15
    app._test_model()
    app._close()
    assert app.closing
    wait_ui(app, lambda: app.closed)
    assert app.engine is None and app.verified is None
    assert not config.CONFIG_PATH.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk first-use wizard")
def test_exit_during_engine_load_does_not_open_main(installation, model_http):
    from bagu_trainer.initialization import InitializationApp
    seed_bank(installation)
    verify_and_save(model_http)
    startup.set_projects([installation.watched])
    def load():
        time.sleep(0.15)
        return object()
    coordinator = startup.StartupCoordinator(engine_loader=load)
    app = InitializationApp(coordinator)
    app.root.withdraw()
    wait_ui(app, lambda: not app.busy)
    assert app.step == "finish"
    app._next()
    app._close()
    wait_ui(app, lambda: app.closed)
    assert app.engine is None
