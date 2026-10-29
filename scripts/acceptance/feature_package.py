"""Run the actual production EXE with test profiles; never install/uninstall it."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(bundle, work):
    from bagu_trainer.infrastructure import desktop_instance
    from scripts.acceptance.desktop_acceptance import capture_window
    work.mkdir(parents=True, exist_ok=False)
    exe = bundle / "BaguTrainer.exe"
    result = {"production_exe": str(exe), "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "installed_or_uninstalled": False, "real_credentials_accessed": False, "cases": []}
    for mode in ("fresh", "broken-config", "last-bank-deleted"):
        profile = work / mode
        profile.mkdir()
        if mode == "broken-config":
            (profile / "config.json").write_text("{broken synthetic configuration")
        elif mode == "last-bank-deleted":
            data = profile / "data"
            generation = uuid.uuid4().hex
            bank = data / "banks" / generation
            bank.mkdir(parents=True)
            for name, value in (("questions.json", []), ("question_concepts.json", {"profiles": {}}),
                    ("sources.json", {"sources": []}), ("empty-bank.json", {"reason": "sources_removed"})):
                (bank / name).write_text(json.dumps(value))
            (data / "active-bank.json").write_text(json.dumps({"schema_version": 1, "generation": generation}))
        environment = dict(os.environ, BAGU_USER_DATA_DIR=str(profile), BAGU_TEST_INSTANCE="frozen-feature-" + uuid.uuid4().hex,
            LOCALAPPDATA=str(work / "localappdata"), PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
        environment.pop("BAGU_LLM_API_KEY", None)
        desktop_instance.RUNTIME = profile / "runtime"
        child = subprocess.Popen([str(exe)], env=environment, cwd=bundle)
        record = {"mode": mode, "pid": child.pid, "screenshots": []}
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        def wait(predicate):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise AssertionError("Production EXE exited early")
                try:
                    value = predicate()
                    if value:
                        return value
                except (OSError, ValueError, EOFError):
                    pass
                time.sleep(.1)
            raise AssertionError("Production EXE state timeout: " + mode)
        def status():
            return desktop_instance.remote_call("_desktop_status", {})
        def windows():
            found = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            @callback_type
            def callback(hwnd, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == child.pid and user32.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value:
                        found.append((hwnd, title.value))
                return True
            user32.EnumWindows(callback, 0)
            return found
        try:
            ready = wait(lambda: (s if s.get("startup_state") == "running" and s.get("checks") else None) if (s := status()) else None)
            assert ready["checks"]["bank"] is False
            wait(lambda: windows())
            assert [title for _, title in windows()] == ["AI 八股训练器"]
            endpoint = (profile / "runtime" / "bridge-credentials.json").read_bytes()
            assert subprocess.run([str(exe)], env=environment, cwd=bundle, timeout=10).returncode == 0
            assert child.poll() is None and (profile / "runtime" / "bridge-credentials.json").read_bytes() == endpoint
            time.sleep(3)  # Capture the settled native layout, after initial paint.
            record["screenshots"].append(capture_window(work, mode + "-dashboard", pid=child.pid))
            assert desktop_instance.remote_call("_navigate", {"page": "bank"})["ok"]
            wait(lambda: status().get("page") == "bank")
            wait(lambda: status().get("bank_inventory_ready"))
            time.sleep(3)
            assert len(windows()) == 1
            record["screenshots"].append(capture_window(work, mode + "-bank", pid=child.pid))
            assert subprocess.run([str(exe), "--configure"], env=environment, cwd=bundle, timeout=10).returncode == 0
            wait(lambda: status().get("page") == "model")
            assert len(windows()) == 1
            assert not list(profile.rglob("*.zip"))
            if mode == "broken-config":
                assert (profile / "config.json").read_text() == "{broken synthetic configuration"
            record.update(passed=True, singleton=True, embedded_bank_page=True, configure_navigates_same_window=True)
        finally:
            for hwnd, _ in windows():
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()  # Only this exact test-owned PID, after recording failure.
                child.wait(timeout=5)
                raise AssertionError("Production EXE failed to close normally")
        assert child.returncode == 0
        assert all(s["captured"] for s in record["screenshots"])
        result["cases"].append(record)
    result["passed"] = True
    (work / "FEATURE_PACKAGE.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.bundle.resolve(), args.work.resolve()), ensure_ascii=False))
