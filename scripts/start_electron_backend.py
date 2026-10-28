"""Private JSON-lines transport; stdout must contain protocol messages only."""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from multiprocessing import freeze_support
    freeze_support()
    if Path(sys.executable).name.casefold() == "bagucleanup.exe":
        from bagu_trainer.cleanup_cli import main as cleanup_main
        return cleanup_main()
    # Frozen Python ignores PYTHONIOENCODING; the pipe protocol is always UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    channel = sys.stdout
    # Python diagnostics and libraries cannot corrupt the protocol stream.
    sys.stdout = sys.stderr
    from bagu_trainer.runtime import configure_logs
    configure_logs()
    from bagu_trainer.desktop_instance import OwnerLock, WindowBroker
    from bagu_trainer.desktop_service import DesktopService, public_value
    from bagu_trainer.onboarding import SetupError
    lock = threading.Lock()

    def send(value):
        with lock:
            channel.write(json.dumps(public_value(value), ensure_ascii=False) + "\n")
            channel.flush()

    from bagu_trainer.app_cleanup import maintenance_active
    from bagu_trainer.cleanup_cli import journal_path
    if maintenance_active() or journal_path().is_file():
        send({"type": "fatal", "data": {"message": "应用清理尚未完成，请先运行原卸载工具完成恢复。"}})
        return 2
    owner = OwnerLock()
    if not owner.acquire():
        send({"type": "fatal", "data": {"message": "另一个训练器正在运行，请先退出旧窗口。"}})
        return 2
    from bagu_trainer.cleanup_manifest import register_profile
    try:
        register_profile(global_index=True)
    except (OSError, ValueError):
        pass  # Keep broken configuration visible in the workbench.
    service = DesktopService(send)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bagu-commands")
    readers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bagu-readers")
    input_closed = threading.Event()
    history_gate = threading.RLock()

    def execute(request):
        request_id = request.get("id")
        try:
            method = request.get("method")
            if method in {"history.list", "history.detail", "bank.inventory", "bank.clean", "shutdown"}:
                with history_gate:
                    result = service.command(method, request.get("params"))
            else:
                result = service.command(method, request.get("params"))
            send({"id": request_id, "result": result})
        except Exception as error:
            # Never echo model credentials, raw transport bodies or tracebacks.
            message = str(error) if isinstance(error, (ValueError, SetupError)) else f"操作未完成（{type(error).__name__}），请检查设置后重试。"
            from bagu_trainer import secrets
            message = secrets.redact_text(message, secrets.known_keys())
            send({"id": request_id, "error": message})

    def window_command(name, arguments):
        if name in {"_show_dashboard", "_navigate"}:
            send({"type": "show", "data": arguments.get("page", "dashboard")})
            return {"ok": True}
        return {"ok": True, "startup_state": "running", "window_state": "visible"}

    bridge = WindowBroker(window_command, owner)
    bridge.start()
    worker.submit(execute, {"method": "prepare", "id": "startup"})

    def ticking():
        while not input_closed.wait(3):
            if service.stop.is_set():
                return
            # The queue serializes content application and ordinary commands.
            worker.submit(service.tick)

    timer = threading.Thread(target=ticking, daemon=True)
    timer.start()
    send({"type": "connected", "data": {"protocol": 1}})
    try:
        for line in sys.stdin:
            if len(line) > 256000:
                send({"id": None, "error": "请求超过大小限制。"})
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or not isinstance(request.get("id"), (str, int)):
                    raise ValueError()
            except (ValueError, TypeError):
                send({"id": None, "error": "无效的桌面请求。"})
                continue
            lane = readers if request.get("method") in {"history.list", "history.detail", "bank.inventory"} else worker
            lane.submit(execute, request)
            if request.get("method") == "shutdown":
                break
    finally:
        # Stop future ticks, but drain accepted writes before closing the session.
        input_closed.set()
        timer.join()
        readers.shutdown(wait=True)
        worker.shutdown(wait=True)
        service.close()
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
