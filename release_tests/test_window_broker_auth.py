"""Private Windows pipe authentication regressions using temporary runtimes."""
import hashlib
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, CHALLENGE, FAILURE, answer_challenge, deliver_challenge

import pytest

from bagu_trainer import desktop_instance as bridge


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")


@pytest.fixture
def service(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(bridge, "RUNTIME", runtime)
    owner = bridge.OwnerLock(runtime)
    assert owner.acquire()
    state = SimpleNamespace(dispatched=[], handled=[], errors=[], threads=[], accepts=0,
                            condition=threading.Condition(), started=threading.Event(), after_accept=None)
    def dispatch(name, arguments):
        state.dispatched.append(name)
        return {"ok": True, "name": name, "value": arguments.get("value")}
    broker = bridge.WindowBroker(dispatch, owner)
    state.broker, state.owner, state.runtime = broker, owner, runtime
    state.signature = hashlib.sha256(broker.endpoint.read_bytes()).digest()
    accept = broker.listener.accept
    def observe_accept():
        with state.condition:
            state.accepts += 1
            state.condition.notify_all()
        connection = accept()
        if state.after_accept:
            state.after_accept(connection)
        return connection
    monkeypatch.setattr(broker.listener, "accept", observe_accept)
    serve_original, handle_original = broker._serve, broker._handle
    def serve():
        state.threads.append(threading.current_thread())
        state.started.set()
        try:
            serve_original()
        except BaseException as error:
            state.errors.append(type(error).__name__)
    def handle(connection):
        state.handled.append(threading.current_thread())
        handle_original(connection)
    monkeypatch.setattr(broker, "_serve", serve)
    monkeypatch.setattr(broker, "_handle", handle)
    def wait_accept(count):
        with state.condition:
            return state.condition.wait_for(lambda: state.accepts >= count, timeout=1)
    state.wait_accept = wait_accept
    broker.start()  # Exercise the actual application startup path.
    assert state.started.wait(1) and wait_accept(1)
    state.thread = state.threads[0]
    try:
        yield state
    finally:
        broker.close()
        state.thread.join(1)
        for worker in state.handled:
            worker.join(1)
        assert not state.thread.is_alive(), "closed broker remained blocked waiting for a pipe connection"
        assert not state.errors


def reject_bad_key(service):
    before = service.accepts
    with pytest.raises(AuthenticationError):
        Client(service.broker.address, family="AF_PIPE", authkey=b"wrong-local-fixture-auth")
    assert service.wait_accept(before + 1), f"service stopped after rejected authentication: {service.errors}"
    assert service.thread.is_alive()


def test_bad_auth_does_not_stop_window_service(service):
    reject_bad_key(service)
    assert not service.dispatched and not service.handled
    assert bridge.remote_call("_desktop_status", {})["ok"]
    assert bridge.remote_call("_show_dashboard", {})["ok"]
    assert not service.errors


@pytest.mark.parametrize("count", [3, 20])
def test_repeated_bad_auth_never_dispatches_or_changes_identity(service, count):
    for _ in range(count):
        reject_bad_key(service)
    assert not service.dispatched and not service.handled
    assert len(service.broker.auth) == 32
    assert hashlib.sha256(service.broker.endpoint.read_bytes()).digest() == service.signature
    assert bridge.WINDOW_COMMANDS == {"_show_dashboard", "_desktop_status", "_navigate"}
    assert bridge.remote_call("_desktop_status", {"value": "中文状态"})["value"] == "中文状态"


def test_mutual_authentication_failure_is_also_isolated(service):
    before = service.accepts
    with Client(service.broker.address, family="AF_PIPE", authkey=None) as connection:
        answer_challenge(connection, service.broker.auth)
        with pytest.raises(AuthenticationError):
            deliver_challenge(connection, b"wrong-local-fixture-auth")
    assert service.wait_accept(before + 1) and not service.dispatched
    assert bridge.remote_call("_desktop_status", {})["ok"]


@pytest.mark.parametrize("reply", [False, True])
def test_missing_or_invalid_authentication_cannot_reach_handler(service, reply):
    before = service.accepts
    with Client(service.broker.address, family="AF_PIPE", authkey=None) as connection:
        assert connection.recv_bytes(256).startswith(CHALLENGE)
        if reply:
            connection.send_bytes(b"not-the-required-auth-response")
            assert connection.recv_bytes(256) == FAILURE
    assert service.wait_accept(before + 1)
    assert not service.dispatched and not service.handled
    assert bridge.remote_call("_show_dashboard", {})["ok"]


@pytest.mark.parametrize("name", ["submit_answer", "create_quiz", "get_learning_stats", "unknown", "__getattribute__"])
def test_authenticated_forbidden_commands_still_rejected(service, name):
    reject_bad_key(service)
    result = bridge.remote_call(name, {"value": "fixture"})
    assert result["ok"] is False and "不支持" in result["note"]
    assert service.dispatched == []
    assert bridge.remote_call("_desktop_status", {})["ok"]


def test_malformed_json_does_not_stop_authenticated_service(service):
    with Client(service.broker.address, family="AF_PIPE", authkey=service.broker.auth) as connection:
        connection.send_bytes(b"{not-json}")
        assert connection.poll(1)
        assert json.loads(connection.recv_bytes()) == {"ok": False, "note": "desktop request failed"}
    assert not service.dispatched
    assert bridge.remote_call("_desktop_status", {})["ok"]


def test_single_instance_lock_remains_owned_after_bad_auth(service):
    reject_bad_key(service)
    assert bridge.desktop_running()
    other = bridge.OwnerLock(service.runtime)
    assert not other.acquire() and other.handle is None
    probe = "from bagu_trainer.desktop_instance import OwnerLock; import sys; lock=OwnerLock(sys.argv[1]); assert not lock.acquire(); print('owner retained')"
    process = subprocess.run([sys.executable, "-B", "-c", probe, str(service.runtime)],
                             capture_output=True, text=True, timeout=5)
    assert process.returncode == 0, "separate process unexpectedly acquired the instance lock"
    assert process.stdout.strip() == "owner retained"
    assert bridge.remote_call("_desktop_status", {})["ok"]


def test_duplicate_startup_queries_and_activates_existing_window_after_bad_auth(service, monkeypatch):
    from bagu_trainer import startup
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("duplicate loaded the engine"))
    service.broker.dispatch = coordinator.window_command
    assert coordinator.events.empty()
    child = r'''
import json, sys
from pathlib import Path
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from bagu_trainer import desktop, desktop_instance as bridge, startup
bridge.RUNTIME = Path(sys.argv[1])
endpoint = json.loads((bridge.RUNTIME / "bridge-credentials.json").read_text(encoding="utf-8"))
try:
    Client(endpoint["address"], family="AF_PIPE", authkey=b"wrong-local-fixture-auth")
except AuthenticationError:
    pass
else:
    raise AssertionError("wrong authentication was accepted")
assert bridge.remote_call("_desktop_status", {})["startup_state"] == "checking"
def forbidden_startup(*args, **kwargs):
    raise AssertionError("duplicate created another window")
startup.StartupCoordinator = forbidden_startup
assert desktop.main([]) == 0
print("rejected, queried, activated")
'''
    process = subprocess.run([sys.executable, "-B", "-c", child, str(service.runtime)],
                             capture_output=True, text=True, timeout=5)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "rejected, queried, activated"
    assert coordinator.events.get(timeout=1) == ("show", "dashboard")
    assert coordinator.events.empty()
    assert bridge.remote_call("_desktop_status", {})["ok"]


@pytest.mark.parametrize("bad_first", [False, True])
def test_close_stops_idle_accept_removes_discovery_and_releases_lock(service, bad_first):
    if bad_first:
        reject_bad_key(service)
    service.broker.close()
    service.thread.join(1)
    assert not service.thread.is_alive()
    assert not service.broker.endpoint.exists() and service.owner.handle is None
    assert not bridge.desktop_running()
    with pytest.raises(FileNotFoundError):
        bridge.remote_call("_desktop_status", {})
    other = bridge.OwnerLock(service.runtime)
    assert other.acquire()
    other.close()
    service.broker.close()  # Repeated cleanup stays safe.


def test_close_during_authenticated_handoff_does_not_dispatch(service):
    accepted, resume = threading.Event(), threading.Event()
    def pause(_connection):
        accepted.set()
        assert resume.wait(2)
    service.after_accept = pause
    try:
        with Client(service.broker.address, family="AF_PIPE", authkey=service.broker.auth):
            assert accepted.wait(1)
            service.broker.close()
            resume.set()
            service.thread.join(1)
            assert not service.thread.is_alive()
            assert not service.handled and not service.dispatched
    finally:
        resume.set()
