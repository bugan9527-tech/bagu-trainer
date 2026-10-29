"""Same-user single-instance window activation over a private Windows pipe.

Only window activation and status are accepted; learning operations stay in process.
"""
import json
import os
import secrets
import threading
import uuid
from pathlib import Path
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from bagu_trainer.infrastructure.config import USER_DATA_ROOT

def runtime_directory():
    # Production ownership is independent of EXE location and data profile.
    # Test identities must be explicit, never inferred from a custom data path.
    identity = os.environ.get("BAGU_TEST_INSTANCE", "")
    if identity:
        return Path(USER_DATA_ROOT) / "runtime"
    return Path(os.environ.get("LOCALAPPDATA", str(Path(USER_DATA_ROOT).parent))) / "BaguTrainer" / "runtime"


RUNTIME = runtime_directory()
WINDOW_COMMANDS = frozenset({"_show_dashboard", "_desktop_status", "_navigate"})


class OwnerLock:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else RUNTIME
        self.handle = None

    def acquire(self):
        import msvcrt
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / "owner.lock").open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def desktop_running():
    if os.name != "nt" or not (RUNTIME / "owner.lock").exists():
        return False
    probe = OwnerLock()
    if not probe.acquire():
        return True
    probe.close()
    return False


def remote_call(name, arguments):
    """Use JSON bytes (never unpickle messages received from a pipe)."""
    endpoint = json.loads((RUNTIME / "bridge-credentials.json").read_text(encoding="utf-8"))
    with Client(endpoint["address"], family="AF_PIPE",
                authkey=bytes.fromhex(endpoint["auth"])) as connection:
        connection.send_bytes(json.dumps({"name": name, "arguments": arguments}).encode("utf-8"))
        if not connection.poll(5):
            raise TimeoutError("Desktop window did not respond")
        return json.loads(connection.recv_bytes(4 * 1024 * 1024).decode("utf-8"))


class WindowBroker:
    def __init__(self, dispatch, owner):
        self.owner = owner
        self.dispatch = dispatch
        self.address = r"\\.\pipe\bagu-trainer-" + uuid.uuid4().hex
        self.auth = secrets.token_bytes(32)
        self.listener = Listener(self.address, family="AF_PIPE", authkey=self.auth)
        self.closed = False
        self.endpoint = owner.directory / "bridge-credentials.json"
        temporary = self.endpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps({"address": self.address, "auth": self.auth.hex()}), encoding="utf-8")
        os.replace(temporary, self.endpoint)

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="bagu-window-broker").start()

    def _serve(self):
        while not self.closed:
            try:
                connection = self.listener.accept()
            except (AuthenticationError, OSError, EOFError):
                # Listener.accept authenticates before returning a connection.
                # Reject this client without stopping the window service.
                continue
            if self.closed:
                connection.close()
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        with connection:
            try:
                if not connection.poll(5):
                    return
                request = json.loads(connection.recv_bytes(64 * 1024).decode("utf-8"))
                if request["name"] not in WINDOW_COMMANDS:
                    result = {"ok": False, "note": "不支持的操作"}
                else:
                    result = self.dispatch(request["name"], request.get("arguments") or {})
                connection.send_bytes(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception:
                try:
                    connection.send_bytes(b'{"ok":false,"note":"desktop request failed"}')
                except OSError:
                    pass

    def close(self):
        self.closed = True
        # PipeListener.close only closes queued handles, not the handle already
        # blocked in accept(). An immediately dropped, non-waiting transport
        # connection makes the normal handshake fail; it cannot reach a handler.
        # Listener authentication and the command allowlist stay unchanged.
        import _winapi
        try:
            handle = _winapi.CreateFile(
                self.address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
            _winapi.CloseHandle(handle)
        except OSError:
            pass  # Already closed/busy: never wait for a peer during shutdown.
        self.listener.close()
        self.endpoint.unlink(missing_ok=True)
        self.owner.close()
