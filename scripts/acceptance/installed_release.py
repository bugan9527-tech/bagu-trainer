"""Exercise the final production Setup only on an empty GitHub-hosted Windows VM.

Never run this on a workstation or self-hosted runner. Local lifecycle coverage
uses installer_lifecycle.py's compile-time redirected fixtures instead.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import winreg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bagu_trainer import login_startup
from bagu_trainer.version import __version__
from scripts.build_installer import APP_ID, sha256, validate_bundle
from scripts.acceptance.installer_lifecycle import environment, wait_until

UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"


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


def run_native(executable, work, name, data, *extra):
    log = work / (name + ".log")
    subprocess.run([str(executable), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/MAINTENANCEWAIT=10",
                    "/LOG=" + str(log), *extra], env=environment(data), check=True, timeout=180)
    # The native uninstall launcher may return before its second phase ends.
    wait_until(lambda: "Log closed." in log.read_text(encoding="utf-8-sig"), timeout=30)


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


def check_pristine_start(installed, setup, work, data, bundle, entries):
    from bagu_trainer import desktop_instance
    from scripts.acceptance.desktop_acceptance import capture_window
    desktop_instance.RUNTIME = data / "runtime"
    check(not (data / "config.json").exists() and not list(data.rglob("*.db")), "Fresh profile already contains settings/history")
    child = subprocess.Popen([str(installed / "BaguTrainer.exe")], env=environment(data),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        last_status = {}
        def is_first_use():
            nonlocal last_status
            check(child.poll() is None, "Installed EXE exited before first-use wizard")
            try:
                status = desktop_instance.remote_call("_desktop_status", {})
                # Retain only the public state and readiness booleans for diagnostics.
                last_status = {"startup_state": status.get("startup_state"),
                               "checks": status.get("checks")}
                return status.get("startup_state") == "initialization" and status.get("checks") == {
                    "bank": False, "model": False, "projects": False}
            except (OSError, ValueError):
                return False
        try:
            wait_until(is_first_use, timeout=30)
        except AssertionError as error:
            raise AssertionError("Installed first-use readiness timed out: " + json.dumps(last_status)) from error
        screenshot = capture_window(work, "installed-pristine-first-use", pid=child.pid,
                                    expected_title="AI 八股训练器 · 首次使用向导")
        check(screenshot["captured"], "Could not capture installed first-use wizard")
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
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=15)


def run(setup, bundle, driver, work, output):
    require_hosted_runner()
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    work = work.resolve()
    check(runner_temp in work.parents and not work.exists(), "Work must be a NEW directory below RUNNER_TEMP")
    manifest, entries = validate_bundle(bundle)
    target_commit = os.environ["GITHUB_SHA"]
    check(manifest["source_dirty"] is False and manifest["source_commit"] == target_commit, "Production payload must match clean CI commit")
    metadata = json.loads((setup.parent / "INSTALLER_MANIFEST.json").read_text(encoding="utf-8"))
    check(metadata["installer"]["sha256"] == sha256(setup), "Production Setup digest differs")
    default_data = folder(28) / "BaguTrainer"
    desktop = folder(16) / "AI 八股训练器.lnk"
    menu = folder(2) / "AI 八股训练器" / "AI 八股训练器.lnk"
    startup = folder(7) / "BaguTrainer.lnk"
    installed = folder(28) / "Programs" / "BaguTrainer"
    check(registration() is None and not default_data.exists() and not installed.exists() and
          not any(path.exists() for path in (desktop, menu, startup)), "Runner already has application state; refusing to touch it")
    work.mkdir(parents=True)
    data = work / "bootstrap-profile"
    report = {"passed": False, "version": __version__, "source_commit": target_commit, "source_dirty": False,
              "source_tree_sha256": manifest["source_tree"]["sha256"], "installer_sha256": sha256(setup),
              "payload_manifest_sha256": sha256(bundle / "BUILD_MANIFEST.json"),
              "production_installer_executed": True, "runner": "github-hosted Windows",
              "credential_backend": "memory fixtures / null diagnostic backend", "real_model_requests": False,
              "personal_data_isolated": True, "windows_credentials_accessed": False}
    try:
        run_native(setup, work, "install", data, "/TASKS=desktopicon")
        registered = registration()
        check(registered and registered["DisplayName"] == "AI 八股训练器" and registered["DisplayVersion"] == __version__
              and Path(registered["InstallLocation"]) == installed
              and str(installed / "BaguTrainer.exe") in registered["DisplayIcon"]
              and str(installed) in registered["UninstallString"], "Installed-app registration differs")
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, "")), installed)
        check(not startup.exists(), "Setup unexpectedly enabled login startup")
        report["installation"] = report["registration"] = report["shortcuts"] = True
        report["default_install_path"] = True
        pristine = work / "pristine-profile"
        report["pristine_screenshot"] = check_pristine_start(installed, setup, work, pristine, bundle, entries)
        report["pristine_first_use"] = report["no_preset_projects_history_or_key"] = True
        report["running_upgrade_blocked"] = report["running_uninstall_blocked"] = True
        run_native(setup, work, "repeat-install", data, "/DIR=" + str(installed))
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, "")), installed)
        report["repeat_install"] = True
        # Reuse the unchanged full frozen first-use/history/model test against
        # the installed production EXE. Its only HTTP server is localhost.
        subprocess.run([str(driver), "--work", str(work / "frozen"), "--production-exe",
                        str(installed / "BaguTrainer.exe"), "--memory-credentials"],
                       env=environment(data), check=True, timeout=300)
        from scripts.release_portable import acceptance_summary
        frozen = acceptance_summary(work / "frozen" / "ACCEPTANCE.json", target_commit)
        check(frozen["production_source_tree"] == manifest["source_tree"]["sha256"], "Installed frozen source differs")
        check(all(frozen[phase]["credential_backend"] == "memory fixture" for phase in ("first", "restart")),
              "Installed frozen acceptance did not isolate credentials")
        data = work / "frozen" / "用户 数据"
        report["installed_frozen_acceptance"] = True
        keep = {path: sha256(path) for path in data.rglob("*") if path.is_file()
                and path.suffix in {".json", ".db"} and "runtime" not in path.relative_to(data).parts}
        check(keep and any(path.suffix == ".db" for path in keep), "No synthetic learning data was tested")
        subprocess.run([str(installed / "BaguTrainer.exe"), "--install-startup", "--startup-directory", str(startup.parent)],
                       env=environment(data), check=True, timeout=30)
        run_native(setup, work, "overwrite", data, "/DIR=" + str(installed))
        check_payload(installed, bundle, entries)
        check_links(((desktop, ""), (menu, ""), (startup, "--background")), installed)
        check(all(sha256(path) == digest for path, digest in keep.items()), "Overwrite changed synthetic personal data")
        diagnostic = work / "installed-package.json"
        subprocess.run([str(installed / "BaguTrainer.exe"), "--check-package", str(diagnostic)],
                       env=environment(data), check=True, timeout=45)
        package = json.loads(diagnostic.read_text(encoding="utf-8"))
        check(package["frozen"] and package["version"] == __version__ and Path(package["installation"]) == installed
              and Path(package["user_data"]) == data and package["keyring_backend"] == "keyring.backends.null.Keyring",
              "Installed runtime/version/profile/credential isolation differs")
        report["same_version_overwrite"] = report["enabled_startup_repaired"] = report["installed_runtime"] = True
        run_native(next(installed.glob("unins*.exe")), work, "uninstall", data)
        check(registration() is None and not (installed / "BaguTrainer.exe").exists()
              and not any(path.exists() for path in (desktop, menu, startup)), "Uninstall left owned application/registration/shortcuts")
        check(all(sha256(path) == digest for path, digest in keep.items()), "Uninstall changed synthetic personal data")
        report["uninstall"] = report["personal_data_preserved"] = report["registration_removed"] = True
        report["passed"] = True
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist" / "BaguTrainer")
    parser.add_argument("--driver", type=Path, default=ROOT / "dist" / "BaguTrainerAcceptance" / "BaguTrainerAcceptance.exe")
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "validation" / "INSTALLED_ACCEPTANCE.json")
    args = parser.parse_args()
    run(args.setup.resolve(), args.bundle.resolve(), args.driver.resolve(), args.work, args.output)


if __name__ == "__main__":
    main()
