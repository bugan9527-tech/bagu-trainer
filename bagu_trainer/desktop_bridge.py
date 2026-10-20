"""Same-user Windows named-pipe bridge: desktop and MCP share one engine.

No TCP port is opened. Discovery credentials live in an ignored runtime folder;
an OS file lock, rather than a stale PID or discovery file, identifies the owner.
"""
import functools
import inspect
import json
import os
import secrets
import threading
import uuid
from pathlib import Path
from multiprocessing.connection import Client, Listener

RUNTIME = Path(__file__).resolve().parent.parent / "data" / ".desktop-runtime"
_OWNER = False
TOOLS = frozenset({
    "_show_dashboard", "_desktop_status",
    "list_knowledge_points", "report_ai_activity", "check_pending_quiz",
    "open_config_window", "configure_llm", "generate_quiz", "submit_answer",
    "get_learning_stats",
})


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
    if _OWNER or os.name != "nt" or not (RUNTIME / "owner.lock").exists():
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
        if not connection.poll(120):
            raise TimeoutError("Desktop engine did not respond")
        return json.loads(connection.recv_bytes(4 * 1024 * 1024).decode("utf-8"))


def forward_to_desktop(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if not desktop_running():
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        try:
            return remote_call(function.__name__, bound.arguments)
        except Exception:
            # Fail closed: never run a second generator when the desktop owns it.
            return {"ok": False, "has_pending": False, "desktop_mode": True,
                    "note": "桌面训练器正在启动或暂时不可用，请稍后重试。"}

    return wrapped


class BridgeServer:
    def __init__(self, dispatch, owner):
        global _OWNER
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
        _OWNER = True

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="bagu-desktop-bridge").start()

    def _serve(self):
        while not self.closed:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                continue
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        with connection:
            try:
                if not connection.poll(5):
                    return
                request = json.loads(connection.recv_bytes(64 * 1024).decode("utf-8"))
                if request["name"] not in TOOLS:
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
        global _OWNER
        self.closed = True
        self.listener.close()
        self.endpoint.unlink(missing_ok=True)
        _OWNER = False
        self.owner.close()
