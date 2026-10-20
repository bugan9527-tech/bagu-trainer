"""Exercise the built Windows EXE, with isolated data and owned child processes."""
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from bagu_trainer import desktop_instance
from bagu_trainer.version import __version__
from release_tests.test_migration import assert_history, legacy_project


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "dist" / "BaguTrainer"
ARCHIVE = ROOT / "dist" / f"BaguTrainer-{__version__}-windows-x64.zip"
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not (BUNDLE / "BaguTrainer.exe").is_file(),
                                reason="build the Windows x64 onedir bundle first")


def environment(tmp_path):
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path / "本地 用户 数据"),
               BAGU_LLM_API_KEY="local-packaging-test-key", PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    env.pop("BAGU_USER_DATA_DIR", None)
    # The application must run with no external tools on PATH.
    env["PATH"] = ""
    return env


def invoke(exe, env, *args):
    return subprocess.run([str(exe), *map(str, args)], env=env, cwd=exe.parent,
                          capture_output=True, timeout=20, check=True)


@contextmanager
def running(exe, env, *args):
    child = subprocess.Popen([str(exe), *args], env=env, cwd=exe.parent,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def until(child, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert child.poll() is None, f"EXE exited unexpectedly: {child.returncode}"
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.08)
    pytest.fail("Timed out waiting for owned EXE")


def window_titles(pid):
    user32 = ctypes.windll.user32
    titles = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    @callback_type
    def collect(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            title = ctypes.create_unicode_buffer(1024)
            user32.GetWindowTextW(hwnd, title, 1024)
            if title.value:
                titles.append(title.value)
        return True
    user32.EnumWindows(collect, None)
    return titles


def await_state(child, state):
    def probe():
        status = desktop_instance.remote_call("_desktop_status", {})
        return status if status.get("startup_state") == state else None
    return until(child, probe)


def tree_hash(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


def test_bundle_inventory_contains_only_runtime_and_source_metadata():
    manifest = json.loads((BUNDLE / "BUILD_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["architecture"] == "windows-x64"
    assert manifest["layout"] == "PyInstaller onedir"
    files = tree_hash(BUNDLE)
    assert files.keys() == {item["path"] for item in manifest["files"]} | {"BUILD_MANIFEST.json"}
    for item in manifest["files"]:
        assert files[item["path"]] == item["sha256"]
    forbidden = {"config.json", "questions.json", "question_concepts.json", "fallback_quizzes.json",
                 "active-bank.json", "onboarding.json", "bridge-credentials.json"}
    for path in files:
        assert Path(path).name not in forbidden
        assert Path(path).suffix not in {".db", ".log", ".sqlite"}
        assert not set(Path(path).parts) & {"repos", "tests", "release_tests", "scripts", ".git"}
    assert "_internal/interview-banks/sources.json" in files
    assert "_internal/licenses/Python-LICENSE.txt" in files
    assert "_internal/THIRD_PARTY_NOTICES.txt" in files
    with zipfile.ZipFile(ARCHIVE) as archive:
        assert archive.testzip() is None
        archived_files = {name.removeprefix("BaguTrainer/"): hashlib.sha256(archive.read(name)).hexdigest()
                          for name in archive.namelist() if not name.endswith("/")}
        assert archived_files == files
    expected_archive_hash = Path(str(ARCHIVE) + ".sha256").read_text(encoding="ascii").split()[0]
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == expected_archive_hash


def test_chinese_spaces_readonly_install_singleton_and_configuration(tmp_path, monkeypatch):
    installation = tmp_path / "中文 空格 程序" / "训练器"
    shutil.copytree(BUNDLE, installation)
    exe = installation / "BaguTrainer.exe"
    before = tree_hash(installation)
    env = environment(tmp_path)
    user = Path(env["LOCALAPPDATA"]) / "BaguTrainer"
    monkeypatch.setattr(desktop_instance, "RUNTIME", user / "runtime")
    sid_text = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True)
    sid = re.search(r"S-1-[\d-]+", sid_text).group()
    # Apply and restore ACLs only on this test's verified temporary installation.
    installation.resolve().relative_to(tmp_path.resolve())
    try:
        # Generic W also denies SYNCHRONIZE on Windows, which prevents reads.
        # Deny only file/attribute writes while preserving loading/execution.
        subprocess.run(["icacls.exe", str(installation), "/deny", f"*{sid}:(OI)(CI)(WD,AD,WEA,WA)", "/T", "/Q"],
                       check=True, capture_output=True)
        with pytest.raises(PermissionError):
            (installation / "must-not-write.txt").write_text("probe")
        report = tmp_path / "运行环境.json"
        diagnostic_env = dict(env)
        diagnostic_env.pop("PYTHON_KEYRING_BACKEND")  # Probe actual Windows backend, without reading credentials.
        invoke(exe, diagnostic_env, "--check-package", report)
        observed = json.loads(report.read_text(encoding="utf-8"))
        assert observed["frozen"] and observed["architecture"] == 64
        assert Path(observed["user_data"]) == user
        assert Path(observed["config"]) == user / "config.json"
        assert Path(observed["database"]) == user / "data" / "trainer.db"
        assert Path(observed["resources"]) == installation / "_internal"
        assert observed["keyring_backend"] == "keyring.backends.Windows.WinVaultKeyring"
        assert observed["tcl_version"].startswith("8.6") and observed["sources"] > 0
        with running(exe, env) as first:
            status = await_state(first, "initialization")
            assert not status["checks"]["bank"]
            until(first, lambda: any("首次" in title for title in window_titles(first.pid)))
            credentials = (user / "runtime" / "bridge-credentials.json").read_bytes()
            invoke(exe, env)
            assert (user / "runtime" / "bridge-credentials.json").read_bytes() == credentials
            assert first.poll() is None
        # Independent frozen --configure entry must actually construct its Tk window.
        with running(exe, env, "--configure") as settings:
            until(settings, lambda: any("AI 接口配置" in title for title in window_titles(settings.pid)))
        assert tree_hash(installation) == before
        assert (user / "logs" / "desktop.log").is_file()
        assert env["BAGU_LLM_API_KEY"] not in (user / "logs" / "desktop.log").read_text(encoding="utf-8")
    finally:
        subprocess.run(["icacls.exe", str(installation), "/remove:d", f"*{sid}", "/T", "/Q"],
                       check=True, capture_output=True)


def test_native_startup_shortcut_points_to_exe(tmp_path):
    installation = tmp_path / "自启动 中文 空格"
    shutil.copytree(BUNDLE, installation)
    exe = installation / "BaguTrainer.exe"
    startup = tmp_path / "临时 Startup"
    startup.mkdir()
    env = environment(tmp_path)
    invoke(exe, env, "--install-startup", "--startup-directory", startup)
    shortcut = startup / "BaguTrainer.lnk"
    assert shortcut.is_file()
    from bagu_trainer.login_startup import read_shortcut
    details = read_shortcut(shortcut)
    assert Path(details["target"]) == exe
    assert details["arguments"] == "--background"
    assert Path(details["cwd"]) == installation
    invoke(exe, env, "--uninstall-startup", "--startup-directory", startup)
    assert not shortcut.exists()


def test_exe_migration_and_upgrade_keep_learning_history(tmp_path, monkeypatch):
    legacy = legacy_project(tmp_path / "旧版 项目")
    original = tree_hash(legacy)
    # Test the shipped ZIP itself, not only the build directory.
    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(tmp_path / "解压 版本 一")
    first = tmp_path / "解压 版本 一" / "BaguTrainer"
    env = environment(tmp_path)
    user = Path(env["LOCALAPPDATA"]) / "BaguTrainer"
    monkeypatch.setattr(desktop_instance, "RUNTIME", user / "runtime")
    report = tmp_path / "migration-report.json"
    invoke(first / "BaguTrainer.exe", env, "--migrate-from", legacy, "--check-package", report)
    assert_history(user / "data" / "trainer.db")
    assert_original_data_preserved(legacy, original)
    upgraded = tmp_path / "升级 版本 二"
    shutil.copytree(BUNDLE, upgraded)
    with running(first / "BaguTrainer.exe", env) as child:
        await_state(child, "running")
        until(child, lambda: "AI 八股训练器" in window_titles(child.pid))
        credentials = (user / "runtime" / "bridge-credentials.json").read_bytes()
        invoke(upgraded / "BaguTrainer.exe", env)
        assert (user / "runtime" / "bridge-credentials.json").read_bytes() == credentials
    assert_history(user / "data" / "trainer.db")
    # A second installation starts against the same user root and learning data.
    first.resolve().relative_to(tmp_path.resolve())
    shutil.rmtree(first)
    invoke(upgraded / "BaguTrainer.exe", env, "--check-package", report)
    assert Path(json.loads(report.read_text(encoding="utf-8"))["user_data"]) == user
    with running(upgraded / "BaguTrainer.exe", env) as child:
        await_state(child, "running")
        credentials = (user / "runtime" / "bridge-credentials.json").read_bytes()
        invoke(upgraded / "BaguTrainer.exe", env)
        assert (user / "runtime" / "bridge-credentials.json").read_bytes() == credentials
    assert_history(user / "data" / "trainer.db")
    assert_original_data_preserved(legacy, original)


def assert_original_data_preserved(directory, original):
    current = tree_hash(directory)
    assert all(current.get(name) == digest for name, digest in original.items())
    # Opening a WAL-mode SQLite database read-only can create shared-memory and
    # empty WAL coordination files; no original configuration, bank or DB changes.
    assert set(current) - set(original) <= {"data/trainer.db-wal", "data/trainer.db-shm"}
