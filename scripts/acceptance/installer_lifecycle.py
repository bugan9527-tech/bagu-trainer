"""Execute isolated Inno lifecycle fixtures, never the production installer.

Uses the production Pascal Script, but separate compile-time AppIds, redirected
shell folders/data, synthetic payloads and a new directory below build/. It does
not access Windows credentials or the signed-in user's BaguTrainer data.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bagu_trainer import login_startup
from scripts.build_installer import check_compiler
from scripts.build_portable import source_provenance
from bagu_trainer.version import __version__


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def shortcut(path, target, arguments="", cwd=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with login_startup._shell_link() as (link, persist, call):
        call(link, 20, [ctypes.c_wchar_p], str(target))
        call(link, 11, [ctypes.c_wchar_p], arguments)
        call(link, 9, [ctypes.c_wchar_p], str(cwd or target.parent))
        call(link, 17, [ctypes.c_wchar_p, ctypes.c_int], str(target), 0)
        call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(path), 1)


def registered(identity):
    assert identity.startswith("BaguTrainer.Lifecycle.")
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{identity}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            return winreg.QueryValueEx(handle, "DisplayVersion")[0]
    except FileNotFoundError:
        return None


def compile_fixture(iscc, case, identity, version, executable, legacy=False, wait_dialog=False):
    assert identity.startswith("BaguTrainer.Lifecycle.")
    suffix = version + ("-wait-dialog" if wait_dialog else "")
    payload = case / ("payload-" + suffix)
    payload.mkdir()
    shutil.copyfile(executable, payload / "BaguTrainer.exe")
    (payload / "中文使用说明.md").write_text("Synthetic Unicode payload fixture", encoding="utf-8")
    (payload / "_internal").mkdir()
    (payload / "_internal" / "version.txt").write_text(version, encoding="ascii")
    (payload / "_internal" / ("removed-in-v2.txt" if version == "0.1.0" else "added-in-v2.txt")).write_text("synthetic", encoding="ascii")
    files = [path for path in payload.rglob("*") if path.is_file()]
    inventory = case / ("inventory-" + suffix + ".txt")
    inventory.write_text("\n".join(str(path.relative_to(payload)) for path in files) + "\n", encoding="utf-8-sig")
    include = case / ("payload-" + suffix + ".iss")
    lines = ["[Files]"]
    for path in files:
        parent = path.relative_to(payload).parent
        dest = "{app}" if parent == Path(".") else "{app}\\" + str(parent)
        lines.append(f'Source: "{path}"; DestDir: "{dest}"; Flags: ignoreversion')
    lines.append(f'Source: "{inventory}"; DestDir: "{{app}}"; DestName: "BaguTrainer.install-files.txt"; Flags: ignoreversion')
    include.write_text("\n".join(lines), encoding="utf-8-sig")
    output = case / ("compiled-" + suffix)
    output.mkdir()
    command = [str(iscc), "/Q", f"/DAppVersion={version}", f"/DPayloadInclude={include}",
               f"/DOutputPath={output}", f"/DLifecycleTestRoot={case}", f"/DLifecycleTestAppId={identity}"]
    if legacy:
        command.append("/DLifecycleTestLegacyIcons=1")
    if wait_dialog:
        command.append("/DLifecycleTestForceWaitDialog=1")
    subprocess.run([*command, str(ROOT / "packaging" / "windows-installer.iss")], check=True)
    return output / f"BaguTrainer-{version}-windows-x64-Setup.exe"


def environment(data):
    value = dict(os.environ, BAGU_USER_DATA_DIR=str(data), PYTHONIOENCODING="utf-8",
                 PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    value.pop("BAGU_LLM_API_KEY", None)
    return value


def command(executable, case, name, *, setup=False, wait=0, desktop=False):
    result = [str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
              f"/MAINTENANCEWAIT={wait}", f"/LOG={case / (name + '.log')}"]
    if setup:
        result.append("/DIR=" + str(case / "installed"))
        if desktop:
            result.append("/TASKS=desktopicon")
    return result


def execute(cmd, data, expected=0):
    print("Run:", Path(cmd[0]).name, next((s for s in cmd if s.startswith('/LOG=')), ''), flush=True)
    result = subprocess.run(cmd, env=environment(data), capture_output=True, timeout=40)
    if expected == 0:
        assert result.returncode == 0, (cmd[0], result.returncode)
    else:
        assert result.returncode != 0, "Maintenance unexpectedly succeeded while locked"


def owner(data):
    source = """
import sys
from pathlib import Path
from bagu_trainer.desktop_instance import OwnerLock
lock = OwnerLock(Path(sys.argv[1]) / 'runtime')
assert lock.acquire()
print('locked', flush=True)
sys.stdin.readline()
lock.close()
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", source, str(data)], cwd=ROOT,
        env=environment(data), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    return process


def release(process):
    if process.poll() is None:
        process.communicate("exit\n", timeout=8)


def wait_until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except FileNotFoundError:
            pass
        time.sleep(.05)
    raise AssertionError("Timed out waiting for native uninstall finalization")


def wait_then_release(cmd, data, holder, unchanged):
    process = subprocess.Popen(cmd, env=environment(data), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(1)
        assert process.poll() is None, "Installer did not wait for the owner to exit"
        unchanged()
        release(holder)
        process.communicate(timeout=40)
        assert process.returncode == 0, process.returncode
    finally:
        release(holder)
        if process.poll() is None:
            process.communicate(timeout=40)


def run_case(root, iscc, executable, foreign):
    case = root / ("foreign-links" if foreign else "owned-links")
    case.mkdir()
    identity = "BaguTrainer.Lifecycle." + uuid.uuid4().hex
    data = case / "user-data"
    (data / "data").mkdir(parents=True)
    (data / "config.json").write_text('{"synthetic":true}', encoding="utf-8")
    (data / "data" / "questions.json").write_text('[{"id":"synthetic-only"}]', encoding="utf-8")
    with sqlite3.connect(data / "data" / "trainer.db") as database:
        database.execute("CREATE TABLE history (answer TEXT)")
        database.execute("INSERT INTO history VALUES ('synthetic answer')")
    (data / "synthetic-vault.marker").write_text("Synthetic credential fixture; not a Windows vault entry", encoding="ascii")
    before_data = {path: digest(path) for path in data.rglob("*") if path.is_file()}
    portable = case / "portable"
    portable.mkdir()
    portable_exe = portable / "BaguTrainer.exe"
    shutil.copyfile(executable, portable_exe)
    portable_before = digest(portable_exe)
    setup1 = compile_fixture(iscc, case, identity, "0.1.0", executable, legacy=True)
    setup2 = compile_fixture(iscc, case, identity, "0.2.0", executable)
    setup_dialog = compile_fixture(iscc, case, identity, "0.2.0", executable, wait_dialog=True)
    installed = case / "installed"
    target = installed / "BaguTrainer.exe"
    desktop = case / "desktop" / "AI 八股训练器.lnk"
    menu = case / "start-menu" / "AI 八股训练器" / "AI 八股训练器.lnk"
    startup = case / "startup" / "BaguTrainer.lnk"
    processes = []
    try:
        execute(command(setup1, case, "install", setup=True, desktop=True), data)
        assert registered(identity) == "0.1.0"
        assert not startup.exists(), "Installation enabled startup without consent"
        shortcut(startup, target, "--background", portable)  # Enabled, with stale working directory.
        unknown = installed / "_internal" / "unknown-user-note.txt"
        unknown.write_text("keep this unknown file", encoding="ascii")
        holder = owner(data)
        processes.append(holder)
        execute(command(setup2, case, "blocked-owner", setup=True), data, expected=1)
        assert (installed / "_internal" / "version.txt").read_text() == "0.1.0"
        wait_then_release(command(setup_dialog, case, "upgrade-wait", setup=True, wait=8), data, holder,
                          lambda: assert_old_version(installed))
        assert '等待界面已显示' in (case / "upgrade-wait.log").read_text(encoding="utf-8-sig")
        assert registered(identity) == "0.2.0"
        assert not (installed / "_internal" / "removed-in-v2.txt").exists()
        assert (installed / "_internal" / "added-in-v2.txt").exists()
        for link, args in ((desktop, ""), (menu, ""), (startup, "--background")):
            metadata = login_startup.read_shortcut(link)
            assert Path(metadata["target"]) == target and metadata["arguments"] == args
            assert Path(metadata["cwd"]) == installed
        # Same-name processes are never a kill criterion. Only installed file
        # occupation blocks; the unrelated portable fixture remains running.
        other = subprocess.Popen([str(portable_exe)], env=environment(data), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        processes.append(other)
        assert other.stdout.readline().strip() == "fixture-ready"
        running = subprocess.Popen([str(target)], env=environment(data), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        processes.append(running)
        assert running.stdout.readline().strip() == "fixture-ready"
        execute(command(setup2, case, "blocked-image", setup=True), data, expected=1)
        assert running.poll() is None and other.poll() is None
        release(running)
        execute(command(setup2, case, "repair", setup=True), data)
        assert other.poll() is None
        if not foreign:
            execute([*command(setup2, case, "desktop-off", setup=True), '/TASKS='], data)
            assert not desktop.exists() and menu.exists() and startup.exists()
            execute(command(setup2, case, "desktop-on", setup=True, desktop=True), data)
            assert desktop.exists()
        foreign_hashes = {}
        if foreign:
            for link, args in ((desktop, ""), (menu, ""), (startup, "--background")):
                shortcut(link, portable_exe, args)
                foreign_hashes[link] = digest(link)
            execute(command(setup2, case, "foreign-repair", setup=True), data)
            for link, expected_hash in foreign_hashes.items():
                assert digest(link) == expected_hash, "Upgrade took over another copy's shortcut"
        uninstaller = next(installed.glob("unins*.exe"))
        holder = owner(data)
        processes.append(holder)
        execute(command(uninstaller, case, "blocked-uninstall"), data, expected=1)
        assert target.exists() and registered(identity) == "0.2.0"
        wait_then_release(command(uninstaller, case, "uninstall-wait", wait=8), data, holder,
                          lambda: assert_installed(target))
        # Inno's first-phase launcher can return before final script cleanup.
        # Wait for the actual log closure and restored legacy links, not just PID exit.
        wait_until(lambda: "Log closed." in (case / "uninstall-wait.log").read_text(encoding="utf-8-sig"))
        if foreign:
            wait_until(lambda: all(link.exists() for link in foreign_hashes))
        assert not target.exists() and registered(identity) is None
        assert unknown.read_text() == "keep this unknown file"
        if foreign:
            for link, expected_hash in foreign_hashes.items():
                assert digest(link) == expected_hash, "Uninstall removed a foreign/retargeted entry"
        else:
            assert not any(link.exists() for link in (desktop, menu, startup))
        for path, expected_hash in before_data.items():
            assert digest(path) == expected_hash, "Personal fixture data was altered"
        assert digest(portable_exe) == portable_before and other.poll() is None
        return {"passed": True, "isolated_app_id": identity, "foreign_shortcuts": foreign,
                "interactive_wait_dialog_resumed": True,
                "cooperative_wait_upgrade_and_uninstall": True, "mapped_image_blocks_upgrade": True,
                "same_name_portable_process_preserved": True, "data_and_synthetic_credential_preserved": True,
                "legacy_icon_log_compatible": True, "test_registration_removed": True}
    finally:
        for process in processes:
            release(process)


def assert_old_version(installed):
    assert (installed / "_internal" / "version.txt").read_text() == "0.1.0"


def assert_installed(target):
    assert target.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True, help="New directory below this project's build/")
    args = parser.parse_args()
    root = args.work.resolve()
    build_root = (ROOT / "build").resolve()
    if build_root not in root.parents or root.exists():
        raise SystemExit("--work must be a NEW dedicated directory below the project build/ folder")
    root.mkdir(parents=True)
    check_compiler(args.iscc)
    source = root / "Fixture.cs"
    source.write_text('class Fixture { static void Main() { System.Console.WriteLine("fixture-ready"); System.Console.ReadLine(); } }', encoding="ascii")
    executable = root / "BaguTrainer.exe"
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    subprocess.run([str(csc), "/nologo", "/platform:x64", "/target:exe", "/out:" + str(executable), str(source)], check=True)
    report = {"passed": False, "production_installer_executed": False, "windows_credentials_accessed": False,
              "version": __version__,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip()),
              "source_tree_sha256": source_provenance()["sha256"],
              "paths_redirected_at_compile_time": True,
              "lifecycle_sha256": digest(ROOT / "packaging" / "windows-installer-lifecycle.iss"),
              "installer_script_sha256": digest(ROOT / "packaging" / "windows-installer.iss"), "cases": []}
    try:
        for foreign in (False, True):
            report["cases"].append(run_case(root, args.iscc.resolve(), executable, foreign))
        report["passed"] = True
    finally:
        (root / "INSTALLER_LIFECYCLE.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(root / "INSTALLER_LIFECYCLE.json")


if __name__ == "__main__":
    main()
