"""Exercise real old/new Setups only on an empty GitHub-hosted Windows VM.

Never run this on a workstation or self-hosted runner. Local lifecycle coverage
uses installer_lifecycle.py's compile-time redirected fixtures instead.
"""
import argparse
import ctypes
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bagu_trainer import login_startup
from bagu_trainer.version import __version__
from scripts.build_installer import APP_ID, sha256, safe_path
from scripts.build_portable import audit, source_provenance
from scripts.acceptance.installer_lifecycle import wait_until
from scripts.acceptance.contract import INSTALLED_CASES, complete_installed_lifecycle
from scripts.portable_artifacts import require_new, resolve_target, verify_archive

UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"


def environment(data):
    # Reuse the fixture environment shape, but deliberately exercise WinCred in
    # the disposable account. No inherited real Key or test instance is allowed.
    from scripts.acceptance.installer_lifecycle import environment as fixture_environment
    value = fixture_environment(data)
    value["PYTHON_KEYRING_BACKEND"] = "keyring.backends.Windows.WinVaultKeyring"
    value.pop("BAGU_TEST_INSTANCE", None)
    return value


def version_tuple(value):
    parts = value.split(".")
    check(len(parts) == 3 and all(p.isdigit() for p in parts), "Version must have three numeric components")
    return tuple(map(int, parts))


def installer_input(setup, bundle, *, previous=False, allow_archived_source=False):
    """Static audit only. No Setup/app/credential or shell-folder API is run."""
    setup, bundle = Path(setup).resolve(), Path(bundle).resolve()
    manifest_path = bundle / "BUILD_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest["version"]
    check(version_tuple(version) < version_tuple(__version__) if previous else version == __version__,
          "Previous Setup must be older; target Setup must match version.py")
    check(manifest.get("application") == "BaguTrainer" and manifest.get("architecture") == "windows-x64",
          "Unexpected application/architecture")
    tree = manifest.get("source_tree", {})
    check(bool(tree.get("files")) and tree.get("sha256") == hashlib.sha256(
        json.dumps(tree["files"], sort_keys=True).encode("utf-8")).hexdigest(), "Invalid source fingerprint")
    matches_checkout = tree == source_provenance()
    check(previous or allow_archived_source or matches_checkout,
          "Source fingerprint differs; rebuild or explicitly select --allow-archived-source for an archived release")
    expected = {}
    for item in manifest["files"]:
        safe_path(item["path"])
        check(item["path"].casefold() not in expected, "Duplicate manifest path")
        expected[item["path"].casefold()] = item
    actual = [item for item in audit(bundle) if item["path"] != "BUILD_MANIFEST.json"]
    check(len(actual) == len(expected) and all(expected.get(item["path"].casefold()) == item for item in actual),
          "Payload differs from BUILD_MANIFEST")
    check((bundle / "BaguTrainer.exe").is_file(), "Missing production executable")
    if not previous:
        check((bundle / "BaguCleanup.exe").is_file(), "0.4.0 requires the real cleanup executable")
    archive = setup.parent / f"BaguTrainer-{version}-windows-x64.zip"
    archive_record = verify_archive(bundle, archive)
    checksum = Path(str(setup) + ".sha256")
    digest = sha256(setup)
    check(checksum.read_text(encoding="ascii").strip() == digest + "  " + setup.name, "Setup checksum attachment mismatch")
    metadata_path = setup.parent / "INSTALLER_MANIFEST.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    check(metadata.get("app_id") == APP_ID and metadata.get("version") == version
          and metadata.get("source_tree") == tree and metadata.get("source_commit") == manifest.get("source_commit")
          and metadata.get("source_dirty") == manifest.get("source_dirty")
          and metadata.get("payload_manifest_sha256") == sha256(manifest_path)
          and metadata.get("installer") == {"name": setup.name, "sha256": digest}
          and metadata.get("inspection", {}).get("all_hashes_match") is True,
          "Setup metadata does not bind the exact audited payload")
    return {"path": str(setup), "sha256": digest, "version": version, "bundle": str(bundle),
            "manifest_sha256": sha256(manifest_path), "metadata_sha256": sha256(metadata_path),
            "source_commit": manifest.get("source_commit"), "source_dirty": manifest.get("source_dirty"),
            "source_tree_sha256": tree["sha256"], "source_matches_checkout": matches_checkout,
            "archive": archive_record}, manifest, [*actual, {"path": "BUILD_MANIFEST.json",
                "bytes": manifest_path.stat().st_size, "sha256": sha256(manifest_path)}]


def require_hosted_runner():
    if (sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise ValueError("Production Setup acceptance requires a disposable GitHub-hosted Windows runner")


def folder(identifier):
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, identifier, None, 0, buffer) != 0:
        raise OSError("Cannot locate runner shell folder")
    return Path(buffer.value)


def registration():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in
                    ("DisplayName", "DisplayVersion", "InstallLocation", "DisplayIcon", "UninstallString")}
    except FileNotFoundError:
        return None


def check(condition, message):
    if not condition:
        raise ValueError(message)


def run_native(executable, work, name, data, *extra, succeeds=True):
    log = work / (name + ".log")
    require_new(log)
    process = subprocess.run([str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/MAINTENANCEWAIT=10",
                    "/LOG=" + str(log), *extra], env=environment(data), timeout=180)
    # The native uninstall launcher may return before its second phase ends.
    wait_until(lambda: "Log closed." in log.read_text(encoding="utf-8-sig"), timeout=30)
    check((process.returncode == 0) == succeeds, "Unexpected installer result for " + name)
    return {"exit_code": process.returncode, "log": str(log), "log_sha256": sha256(log)}


def check_payload(installed, bundle, entries):
    for entry in entries:
        target = installed / entry["path"]
        check(target.is_file() and target.stat().st_size == entry["bytes"] and sha256(target) == entry["sha256"],
              "Installed production payload differs: " + entry["path"])
    check(sha256(installed / "BUILD_MANIFEST.json") == sha256(bundle / "BUILD_MANIFEST.json"), "Installed build manifest changed")


def check_links(links, installed):
    for link, arguments in links:
        metadata = login_startup.read_shortcut(link)
        check(Path(metadata["target"]) == installed / "BaguTrainer.exe" and metadata["arguments"] == arguments
              and Path(metadata["cwd"]) == installed, "Installed shortcut target/arguments/working directory differ")


def close_owned_child(child):
    """Request normal window close for this exact test-owned PID, then wait."""
    if child.poll() is not None:
        return
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def close(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == child.pid:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, never another process
        return True
    user32.EnumWindows(close, 0)
    try:
        child.wait(timeout=25)
    except subprocess.TimeoutExpired:
        child.terminate()  # emergency cleanup of our own PID; scenario fails
        child.wait(timeout=10)
        raise ValueError("Test-owned application did not complete normal shutdown") from None


def check_pristine_start(installed, setup, work, data, bundle, entries):
    from bagu_trainer import desktop_instance
    from scripts.acceptance.desktop_acceptance import capture_window
    desktop_instance.RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "BaguTrainer" / "runtime"
    check(not (data / "config.json").exists() and not list(data.rglob("*.db")), "Fresh profile already contains settings/history")
    child = subprocess.Popen([str(installed / "BaguTrainer.exe")], env=environment(data),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        last_status = {}
        def is_first_use():
            nonlocal last_status
            check(child.poll() is None, "Installed EXE exited before showing the 0.4.0 workspace")
            try:
                status = desktop_instance.remote_call("_desktop_status", {})
                # Retain only the public state and readiness booleans for diagnostics.
                last_status = {"startup_state": status.get("startup_state"),
                               "checks": status.get("checks")}
                return status.get("startup_state") == "running" and status.get("checks") == {
                    "bank": False, "model": False, "projects": False}
            except (OSError, ValueError):
                return False
        try:
            wait_until(is_first_use, timeout=30)
        except AssertionError as error:
            raise AssertionError("Installed first-use readiness timed out: " + json.dumps(last_status)) from error
        screenshot = capture_window(work, "installed-pristine-first-use", pid=child.pid,
                                    expected_title="AI 八股训练器")
        check(screenshot["captured"], "Could not capture installed unconfigured workspace")
        # The real production EXE holds the existing OwnerLock. The installer
        # must fail while it runs and must leave that owned process alive.
        for executable, label in ((setup, "blocked-running-upgrade"),
                                  (next(installed.glob("unins*.exe")), "blocked-running-uninstall")):
            command = [str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                       "/LOG=" + str(work / (label + ".log"))]
            if executable == setup:
                command.append("/DIR=" + str(installed))
            result = subprocess.run(command, env=environment(data), timeout=60)
            check(result.returncode != 0 and child.poll() is None, "Running maintenance was allowed or installer ended the app")
            check_payload(installed, bundle, entries)
        check(not (data / "config.json").exists() and not list(data.rglob("*.db")), "First use created preset settings/history")
        return screenshot
    finally:
        # Only the exact test-owned process is cleaned up. Installer never kills it.
        close_owned_child(child)


def check_restored_start(installed, data, work, label, *, legacy=False):
    from bagu_trainer import desktop_instance
    from scripts.acceptance.desktop_acceptance import capture_window
    desktop_instance.RUNTIME = Path(os.environ["LOCALAPPDATA"]) / "BaguTrainer/runtime"
    child = subprocess.Popen([str(installed / "BaguTrainer.exe")], env=environment(data),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def ready():
            check(child.poll() is None, "Restored application exited before loading synthetic settings")
            try:
                status = desktop_instance.remote_call("_desktop_status", {})
                if legacy:
                    # The old coordinator exposes "running" only after its
                    # readiness gate loaded the engine; it has no checks field
                    # once running. Exercise its own credential/config resolver.
                    return status.get("startup_state") == "running"
                return status.get("checks") == {"bank": True, "model": True, "projects": True} and status.get("preparation_state") == "ready"
            except (OSError, ValueError):
                return False
        wait_until(ready, timeout=30)
        screenshot = capture_window(work, label, pid=child.pid)
        check(screenshot["captured"], "Could not capture restored workspace")
        return screenshot
    finally:
        close_owned_child(child)


class TestVault:
    """Only instantiated after the disposable-host and empty-account gates."""
    def __init__(self, data, backend=None):
        if backend is None:
            from keyring.backends.Windows import WinVaultKeyring
            backend = WinVaultKeyring()
        self.backend = backend
        normalized = os.path.normcase(str(data.resolve()))
        self.service = "bagu-trainer.profile." + hashlib.sha256(normalized.encode()).hexdigest()[:24]
        self.unrelated = "bagu-lifecycle-unrelated-" + uuid.uuid4().hex
        self.value = "synthetic-lifecycle-" + uuid.uuid4().hex
        self.other_value = "unrelated-synthetic-" + uuid.uuid4().hex

    def ensure_empty(self):
        for service in ("bagu-trainer", self.service, self.unrelated):
            check(self.backend.get_password(service, "llm_api_key") is None,
                  "Disposable account already has a credential in a reserved test slot")

    def seed(self):
        for service, value in ((self.service, self.value), (self.unrelated, self.other_value)):
            self.backend.set_password(service, "llm_api_key", value)
            check(self.backend.get_password(service, "llm_api_key") == value, "Native test credential readback failed")

    def preserved(self):
        check(self.backend.get_password(self.service, "llm_api_key") == self.value, "Upgrade/repair changed test credential")
        self.unrelated_preserved()

    def unrelated_preserved(self):
        check(self.backend.get_password(self.unrelated, "llm_api_key") == self.other_value, "Unrelated credential changed")

    def cleared(self):
        check(self.backend.get_password(self.service, "llm_api_key") is None, "Managed test credential remains after uninstall")
        self.unrelated_preserved()

    def retire_unrelated(self):
        self.unrelated_preserved()
        self.backend.delete_password(self.unrelated, "llm_api_key")
        check(self.backend.get_password(self.unrelated, "llm_api_key") is None, "Could not retire unrelated synthetic fixture")


def seed_profile(data, work):
    """Real application schema with entirely synthetic config/bank/attempts."""
    from bagu_trainer.bank_storage import atomic_json
    from bagu_trainer.store import Store
    from bagu_trainer.scheduler import next_state
    project = work / "synthetic-project"
    project.mkdir()
    (project / "query.py").write_text("query = 'SELECT id FROM synthetic_table'\n", encoding="utf-8")
    atomic_json(data / "config.json", {"db_path": "data/trainer.db", "project_roots": [str(project)],
        "session_ingest": False, "lifecycle_marker": "synthetic-preserve-me",
        "llm": {"base_url": "http://127.0.0.1:9/v1", "model": "synthetic-no-network", "api_key": "", "timeout_sec": 1}})
    generation = uuid.uuid4().hex
    bank = data / "data" / "banks" / generation
    atomic_json(bank / "questions.json", [{"id": "synthetic-kp", "knowledge_point": "索引", "question": "合成索引说明",
                                           "tags": ["synthetic"], "keywords": ["index"]}])
    atomic_json(bank / "sources.json", {"sources": [{"repository": "synthetic/lifecycle", "commit": "a" * 40}]})
    atomic_json(data / "data/active-bank.json", {"generation": generation})
    store = Store(data / "data/trainer.db")
    try:
        store.create_quiz("synthetic-history", "synthetic-kp", {"question": "合成历史题", "answer": "A"},
                          "answered", str(project), "synthetic lifecycle", 1800000000, 1800003600,
                          delivered_at=1800000000, answered_at=1800000010)
        store.record_attempt("synthetic-history", "synthetic-kp", "A", "A", True, 1800000010,
                             next_state(True, None, 1800000010))
    finally:
        store.close()
    return project


def profile_snapshot(data):
    database = data / "data/trainer.db"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {name: connection.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
                  for name in ("quizzes", "attempts", "concept_state", "quiz_sources")}
    check(len(tables["attempts"]) == 1, "Synthetic learning attempt is missing")
    return {"config_sha256": sha256(data / "config.json"),
            "bank_files": {str(p.relative_to(data)): sha256(p) for p in (data / "data").rglob("*.json")},
            "learning_sha256": hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest(),
            "attempts": len(tables["attempts"])}


@contextmanager
def deny_delete(path):
    """A real Windows sharing violation on a test-owned DB, not OwnerLock."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)  # no FILE_SHARE_DELETE
    check(handle != ctypes.c_void_p(-1).value, "Could not lock synthetic DB for failure injection")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def new_report(target, previous):
    return {"schema_version": 2, "passed": False, "production_installer_executed": False,
            "version": target["version"], "source_commit": target["source_commit"], "source_dirty": target["source_dirty"],
            "source_tree_sha256": target["source_tree_sha256"], "installer_sha256": target["sha256"],
            "payload_manifest_sha256": target["manifest_sha256"], "target_installer": target, "previous_installer": previous,
            "runner": "not executed", "personal_data_isolated": True, "real_user_credentials_accessed": False,
            "windows_credentials_accessed": False, "test_credentials_only": True,
            "credential_backend": "native Windows vault / synthetic entries in disposable account",
            "credential_values_recorded": False, "real_model_requests": False, "installed_frozen_acceptance": False,
            "harness": {"script_sha256": sha256(Path(__file__)), "source_tree_sha256": source_provenance()["sha256"]},
            "cases": {name: {"status": "not_run", "passed": False, "reason": "Awaiting disposable Windows runner"}
                      for name in INSTALLED_CASES}, "logs": []}


def record_case(report, name, action, save):
    report["cases"][name] = {"status": "running", "passed": False}
    save()
    try:
        detail = action() or {}
        report["cases"][name] = {"status": "passed", "passed": True, **detail}
    except Exception as error:
        report["cases"][name] = {"status": "failed", "passed": False, "error_type": type(error).__name__,
                                 "error": str(error) if isinstance(error, ValueError) else "See native/application logs"}
        save()
        return False
    save()
    return True


def check_environment_empty():
    for hive, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                for name in ("BAGU_LLM_API_KEY", "BAGU_USER_DATA_DIR"):
                    try:
                        winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    raise ValueError("Disposable account/machine has pre-existing BAGU environment configuration")
        except FileNotFoundError:
            pass


def run(setup, bundle, driver, work, output, *, previous_setup, previous_bundle,
        allow_archived_source=False, run_frozen=False):
    require_hosted_runner()  # MUST precede any shell-folder, registry or vault access.
    require_new(output, work)
    work = work.resolve()
    check(Path(os.environ["RUNNER_TEMP"]).resolve() in work.parents, "Work must be below RUNNER_TEMP")
    target, manifest, entries = installer_input(setup, bundle, allow_archived_source=allow_archived_source)
    previous, _, old_entries = installer_input(previous_setup, previous_bundle, previous=True)
    if run_frozen:
        from scripts.portable_artifacts import validate_target
        validate_target(resolve_target(setup.parent, bundle=bundle), expected_source=manifest, driver=driver)
    if not allow_archived_source:
        check(manifest["source_dirty"] is False and manifest["source_commit"] == os.environ["GITHUB_SHA"],
              "CI payload must match the clean target commit")
    data = work / "synthetic-profile"
    default_data = folder(28) / "BaguTrainer"
    installed = folder(28) / "Programs/BaguTrainer"
    desktop = folder(16) / "AI 八股训练器.lnk"
    menu = folder(2) / "AI 八股训练器/AI 八股训练器.lnk"
    startup = folder(7) / "BaguTrainer.lnk"
    journal = folder(28) / "BaguTrainer.uninstall.json"
    locations = folder(28) / "BaguTrainer.locations.json"
    check(registration() is None and not any(p.exists() for p in
        (default_data, installed, desktop, menu, startup, journal, locations)),
        "Account already has application state; refusing to touch it")
    check_environment_empty()
    vault = TestVault(data)
    vault.ensure_empty()
    work.mkdir(parents=True)
    report = new_report(target, previous)
    report.update(runner="github-hosted Windows", windows_credentials_accessed=True, environment={
        "os": platform.platform(), "architecture": platform.machine(),
        "runner_environment": os.environ["RUNNER_ENVIRONMENT"],
        "account_sid": subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True).strip(),
        "work": str(work), "installed": str(installed), "profile": str(data),
        "default_profile": str(default_data), "existing_state_rejected": True})
    def save():
        report["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["logs"] = [{"path": str(p), "sha256": sha256(p)} for p in sorted(work.rglob("*.log*")) if p.is_file()]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def registered_version(version):
        value = registration()
        check(value and value["DisplayVersion"] == version and Path(value["InstallLocation"]) == installed,
              "Installed version/default path differs")
    def native_runtime(label):
        from scripts.build_installer import pe_resources
        pe_version = pe_resources(installed / "BaguTrainer.exe", production=True)["ProductVersion"]
        diagnostic = work / (label + "-package.json")
        subprocess.run([str(installed / "BaguTrainer.exe"), "--check-package", str(diagnostic)],
                       env=environment(data), check=True, timeout=45)
        value = json.loads(diagnostic.read_text(encoding="utf-8"))
        check(value["frozen"] and value["version"] == __version__ and Path(value["installation"]) == installed
              and Path(value["user_data"]) == data and value["keyring_backend"].endswith(".WinVaultKeyring"),
              "Actual installed EXE version/runtime/native credential backend differs")
        return {"path": str(diagnostic), "sha256": sha256(diagnostic), "pe_product_version": pe_version,
                "runtime_version": value["version"]}
    def application_logs(label):
        source = data / "logs"
        if source.is_dir():
            destination = work / "application-logs" / label
            destination.mkdir(parents=True)
            import shutil
            for original in source.glob("desktop.log*"):
                shutil.copyfile(original, destination / original.name)
    state = {}
    def first_install():
        report["production_installer_executed"] = True
        result = run_native(setup, work, "first-install", data, "/TASKS=desktopicon")
        registered_version(__version__)
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, "")), installed)
        check(not startup.exists(), "Setup enabled login startup without consent")
        diagnostic = native_runtime("first-install")
        screenshot = check_pristine_start(installed, setup, work, data, bundle, entries)
        application_logs("first-install")
        report.update(installation=True, registration=True, shortcuts=True, default_install_path=True,
                      pristine_first_use=True, no_preset_projects_history_or_key=True,
                      running_upgrade_blocked=True, running_uninstall_blocked=True, installed_runtime=True)
        return {"native": result, "runtime": diagnostic, "screenshot": screenshot,
                "visual_review": "capture retained; human inspection required"}
    def cross_version_upgrade():
        # Start with a real prior production install, not a recompiled fixture.
        run_native(next(installed.glob("unins*.exe")), work, "remove-pristine-install", data)
        check(registration() is None and not (installed / "BaguTrainer.exe").exists(), "Pristine uninstall failed")
        run_native(previous_setup, work, "install-previous-production", data, "/TASKS=desktopicon")
        registered_version(previous["version"])
        check_payload(installed, previous_bundle, old_entries)
        state["project"] = seed_profile(data, work)
        state["project_sha256"] = sha256(state["project"] / "query.py")
        report["windows_credentials_accessed"] = True
        vault.seed()
        report["credential_scope"] = {"managed_service": vault.service, "unrelated_service": vault.unrelated,
                                      "synthetic_only": True, "values_recorded": False}
        old_screenshot = check_restored_start(installed, data, work, "previous-version-configured", legacy=True)
        state["before"] = profile_snapshot(data)
        # Unknown files and an unrelated native credential must survive.
        state["notes"] = [data / "unrelated-user-note.txt", installed / "unrelated-user-note.txt"]
        for note in state["notes"]:
            note.write_text("synthetic unrelated file: preserve", encoding="utf-8")
        state["note_hashes"] = {str(p): sha256(p) for p in state["notes"]}
        from scripts.acceptance.installer_lifecycle import shortcut
        state["unrelated_link"] = desktop.parent / ("BaguLifecycle-unrelated-" + uuid.uuid4().hex + ".lnk")
        require_new(state["unrelated_link"])
        shortcut(state["unrelated_link"], state["project"] / "query.py")
        state["link_sha256"] = sha256(state["unrelated_link"])
        result = run_native(setup, work, "upgrade-production", data, "/DIR=" + str(installed))
        registered_version(__version__)
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, "")), installed)
        screenshot = check_restored_start(installed, data, work, "upgraded-restored-workspace")
        check(profile_snapshot(data) == state["before"], "Cross-version upgrade changed config/bank/learning rows")
        vault.preserved()
        report["cross_version_upgrade"] = report["upgrade_data_preserved"] = report["upgrade_credentials_preserved"] = True
        return {"from_version": previous["version"], "to_version": __version__, "native": result,
                "preserved": state["before"], "native_test_credential_preserved": True, "screenshot": screenshot,
                "previous_configured_screenshot": old_screenshot,
                "unrelated_file_sha256": state["note_hashes"],
                "unrelated_shortcut": {"path": str(state["unrelated_link"]), "sha256": state["link_sha256"]}}
    def same_version_repair():
        subprocess.run([str(installed / "BaguTrainer.exe"), "--install-startup", "--startup-directory", str(startup.parent)],
                       env=environment(data), check=True, timeout=30)
        result = run_native(setup, work, "same-version-repair", data, "/DIR=" + str(installed))
        check_payload(installed, bundle, entries)
        registered_version(__version__)
        check_links(((desktop, ""), (menu, ""), (startup, "--background")), installed)
        screenshot = check_restored_start(installed, data, work, "repaired-restored-workspace")
        check(profile_snapshot(data) == state["before"], "Repair changed config/bank/learning rows")
        vault.preserved()
        diagnostic = native_runtime("after-repair")
        report["same_version_overwrite"] = report["enabled_startup_repaired"] = report["repair_data_preserved"] = True
        report["repair_credentials_preserved"] = True
        return {"native": result, "runtime": diagnostic, "preserved": state["before"], "screenshot": screenshot,
                "native_test_credential_preserved": True}
    def cleanup_failure_retry():
        application_logs("before-uninstall")
        state["managed_files"] = [p for p in data.rglob("*") if p.is_file() and p not in state["notes"]]
        uninstaller = next(installed.glob("unins*.exe"))
        locked = data / "data/trainer.db"
        with deny_delete(locked):
            failure = run_native(uninstaller, work, "cleanup-sharing-failure", data, succeeds=False)
            registered_version(__version__)
            check_payload(installed, bundle, entries)
            check(locked.is_file() and journal.is_file(), "Cleanup failure did not retain the DB and retry journal")
            saved = json.loads(journal.read_text(encoding="utf-8"))
            check(saved.get("failures") and any(str(locked) in item for item in saved["failures"]),
                  "Uninstall was not blocked by the injected data-cleanup sharing violation")
            (work / "cleanup-failure-journal.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            check(all(Path(p).is_file() and sha256(Path(p)) == digest for p, digest in state["note_hashes"].items()),
                  "Failed cleanup changed unrelated files")
            vault.unrelated_preserved()
        # Do not reseed or replace the journal; exercise the real retry path.
        retry = run_native(uninstaller, work, "cleanup-retry-after-unlock", data)
        report["cleanup_failure_blocked"] = report["cleanup_retry_succeeded"] = True
        return {"failure": failure, "retry": retry, "fault": "Win32 file handle denies deletion of synthetic learning DB",
                "partial_cleanup_is_not_rollback": True}
    def complete_uninstall():
        check(registration() is None and not (installed / "BaguTrainer.exe").exists(), "Uninstall left application registration/files")
        check(not any(p.exists() for p in (desktop, menu, startup, journal, locations)), "Uninstall left owned shortcuts/retry metadata")
        check(not any(p.exists() for p in state["managed_files"]), "Uninstall left managed synthetic data")
        check(not any(p.is_file() for p in default_data.rglob("*")), "Uninstall left default runtime/profile files")
        check(sha256(state["unrelated_link"]) == state["link_sha256"], "Uninstall changed unrelated shortcut")
        check(all(Path(p).is_file() and sha256(Path(p)) == digest for p, digest in state["note_hashes"].items()),
              "Uninstall changed unrelated files")
        vault.cleared()
        report.update(uninstall=True, managed_data_removed=True, test_credential_removed=True,
                      unrelated_files_preserved=True, unrelated_credential_preserved=True, registration_removed=True,
                      owned_shortcuts_removed=True)
        return {"managed_files_verified_absent": len(state["managed_files"]), "native_test_credential_removed": True,
                "unrelated_files_and_native_credential_preserved": True}
    def reinstall_pristine():
        # Retire only our verified sentinel after proving protection. New installs
        # intentionally reject a nonempty program directory; never recursively clear it.
        note = state["notes"][1]
        check(sha256(note) == state["note_hashes"][str(note)], "Sentinel identity changed")
        note.unlink()
        if installed.exists():
            installed.rmdir()  # fails closed if anything unexpected remains
        result = run_native(setup, work, "reinstall-after-complete-uninstall", data, "/TASKS=desktopicon")
        registered_version(__version__)
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, "")), installed)
        check(not startup.exists(), "Reinstall unexpectedly restored login startup")
        check(vault.backend.get_password(vault.service, "llm_api_key") is None, "Reinstall restored removed credential")
        (work / "reinstalled").mkdir()
        screenshot = check_pristine_start(installed, setup, work / "reinstalled", data, bundle, entries)
        application_logs("reinstalled")
        # Return this disposable account to uninstalled state, also preserving the
        # unrelated data sentinel and watch-project files outside managed trees.
        run_native(next(installed.glob("unins*.exe")), work, "final-test-owned-uninstall", data)
        check(registration() is None and not any(p.exists() for p in (desktop, menu, startup)), "Final test cleanup incomplete")
        check(sha256(state["notes"][0]) == state["note_hashes"][str(state["notes"][0])], "Unrelated profile file changed")
        check(sha256(state["project"] / "query.py") == state["project_sha256"], "Uninstall changed watched source project")
        check(sha256(state["unrelated_link"]) == state["link_sha256"], "Final uninstall changed unrelated shortcut")
        state["unrelated_link"].unlink()  # only after its exact synthetic content was verified
        vault.cleared()
        vault.retire_unrelated()
        report["repeat_install"] = report["reinstall_pristine"] = True
        return {"native": result, "screenshot": screenshot, "initial_checks": {"bank": False, "model": False, "projects": False},
                "unrelated_test_credential_retired_after_verification": True}
    save()
    actions = (first_install, cross_version_upgrade, same_version_repair, cleanup_failure_retry, complete_uninstall, reinstall_pristine)
    try:
        for name, action in zip(INSTALLED_CASES, actions):
            if not record_case(report, name, action, save):
                for pending in report["cases"].values():
                    if pending["status"] == "not_run":
                        pending["reason"] = "Prerequisite failed; see earlier case and logs"
                return report
            if name == "same_version_repair" and run_frozen:
                report["frozen_requested"] = True
                try:
                    subprocess.run([str(driver), "--work", str(work / "frozen"), "--production-exe",
                        str(bundle / "BaguTrainer.exe"), "--installed-exe", str(installed / "BaguTrainer.exe"),
                        "--memory-credentials", "--output-dir", str(setup.parent)],
                        env=environment(data), check=True, timeout=300)
                    from scripts.release_portable import acceptance_summary
                    acceptance_summary(work / "frozen/ACCEPTANCE.json", manifest["source_commit"])
                    report["installed_frozen_acceptance"] = True
                except Exception as error:
                    report["frozen_error_type"] = type(error).__name__
                    report["frozen_error_report"] = str(work / "frozen/ERROR.json")
                save()  # UI failure must not prevent uninstall/failure/reinstall coverage.
        report["passed"] = complete_installed_lifecycle(report) and (not run_frozen or report["installed_frozen_acceptance"])
        return report
    finally:
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--previous-setup", required=True, type=Path, help="An actual older production Setup, never a compiled fixture")
    parser.add_argument("--previous-bundle", type=Path, help="Extracted older portable onedir; defaults beside --previous-setup")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--run-frozen", action="store_true", help="Also run the existing full frozen UI acceptance; never substitute lifecycle smoke")
    parser.add_argument("--allow-archived-source", action="store_true", help="Explicitly test an archived 0.4.0 payload; record source mismatch, never relabel it")
    parser.add_argument("--plan-only", action="store_true", help="Static artifact audit and NOT RUN checklist; never install or access credentials")
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "validation" / "INSTALLED_ACCEPTANCE.json")
    args = parser.parse_args()
    target = resolve_target(args.output_dir, bundle=args.bundle)
    previous_bundle = (args.previous_bundle or args.previous_setup.parent / "BaguTrainer").resolve()
    if args.plan_only:
        require_new(args.output)
        current, _, _ = installer_input(args.setup, target.bundle, allow_archived_source=args.allow_archived_source)
        previous, _, _ = installer_input(args.previous_setup, previous_bundle, previous=True)
        report = new_report(current, previous)
        report.update(plan_only=True, execution_status="not_run", reason="No disposable Windows environment used",
                      planned_work=str(args.work.resolve()), environment={"inspected_on": platform.platform(),
                          "isolation_claim": False})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Static audit complete. All six real installation cases are NOT RUN: " + str(args.output))
        return 0
    report = run(args.setup.resolve(), target.bundle, (args.driver or target.driver).resolve(), args.work, args.output,
                 previous_setup=args.previous_setup.resolve(), previous_bundle=previous_bundle,
                 allow_archived_source=args.allow_archived_source, run_frozen=args.run_frozen)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
