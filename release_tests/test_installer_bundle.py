"""Installer inputs are synthetic; no Setup, app or real credentials are run."""
import json
from pathlib import Path
import shutil
import subprocess
from copy import deepcopy

import pytest

from scripts import build_installer as builder
from scripts.build_portable import audit


def production_fixture(root):
    for name in ("BaguTrainer.exe", "BaguCleanup.exe", "使用说明.md", "_internal/LICENSE", "_internal/THIRD_PARTY_NOTICES.txt",
                 "_internal/interview-banks/sources.json", "_internal/licenses/Python-LICENSE.txt",
                 "_internal/_tcl_data/init.tcl", "_internal/_tk_data/tk.tcl", "_internal/runtime.dll"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic build fixture")
    manifest = {"application": "BaguTrainer", "version": builder.__version__, "architecture": "windows-x64",
                "layout": "PyInstaller onedir", "source_commit": "a" * 40, "source_dirty": True,
                "source_tree": {}, "files": audit(root)}
    (root / "BUILD_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def validate(root):
    return builder.validate_bundle(root, current_source=False, check_exe=False)


def test_matching_production_tree_is_accepted(tmp_path):
    manifest, entries = validate(production_fixture(tmp_path))
    assert manifest["version"] == builder.__version__
    assert "BUILD_MANIFEST.json" in {item["path"] for item in entries}


@pytest.mark.parametrize("mutation", ["added", "modified", "missing"])
def test_payload_must_exactly_match_original_inventory(tmp_path, mutation):
    production_fixture(tmp_path)
    path = tmp_path / "_internal/runtime.dll"
    if mutation == "added":
        (tmp_path / "unexpected.txt").write_text("fixture")
    elif mutation == "modified":
        path.write_bytes(b"changed")
    else:
        path.unlink()
    with pytest.raises(ValueError, match="清单|不一致"):
        validate(tmp_path)


@pytest.mark.parametrize("name", ["CONFIG.JSON", ".env.production", "secret.key", "api_key.txt",
    "Key.txt", "credentials.json", "data/trainer.db-wal", "desktop.log.1", "Logs/debug.txt",
    "BaguTrainerAcceptance.exe", "_internal/acceptance-provenance.json", "validation/ACCEPTANCE.json",
    "_internal/INSTALLER_LIFECYCLE.json", "_internal/INSTALLED_ACCEPTANCE.json", "RELEASE_VALIDATION.json",
    "INSTALLER_MANIFEST.json"])
def test_private_or_acceptance_files_rejected_even_if_manifest_includes_them(tmp_path, name):
    production_fixture(tmp_path)
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic; never a real secret")
    manifest_path = tmp_path / "BUILD_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({"path": name, "bytes": target.stat().st_size, "sha256": builder.sha256(target)})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="禁止收录"):
        validate(tmp_path)


@pytest.mark.parametrize("path", ["../history.db", "C:/users/key", "_internal/../key", "{userappdata}/key",
                                  "_internal/foo:bar", "_internal/FOO.", "x\ny", "a\\b", "a//b"])
def test_unsafe_manifest_paths_are_rejected(path):
    with pytest.raises(ValueError, match="不安全"):
        builder.safe_path(path)


def test_duplicate_case_insensitive_manifest_is_rejected(tmp_path):
    production_fixture(tmp_path)
    path = tmp_path / "BUILD_MANIFEST.json"
    manifest = json.loads(path.read_text())
    manifest["files"].append({**manifest["files"][0], "path": manifest["files"][0]["path"].upper()})
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="重复"):
        validate(tmp_path)


def test_stale_source_cannot_be_repackaged(tmp_path):
    production_fixture(tmp_path)
    with pytest.raises(ValueError, match="旧源码"):
        builder.validate_bundle(tmp_path, check_exe=False)


def test_missing_tool_has_actionable_error(tmp_path):
    with pytest.raises(RuntimeError, match="找不到 ISCC"):
        builder.find_tool(tmp_path / "missing.exe", "ISCC.exe", [])


def test_exact_include_does_not_glob_dist_or_collect_sibling_driver(tmp_path):
    bundle = production_fixture(tmp_path / "payload")
    _, entries = validate(bundle)
    work = tmp_path / "work"
    work.mkdir()
    include, inventory = builder.write_payload_include(bundle, entries, work)
    text = include.read_text(encoding="utf-8-sig")
    assert "*" not in text
    assert text.count("Source:") == len(entries) + 1
    assert "BUILD_MANIFEST.json" in inventory.read_text(encoding="utf-8-sig")


def test_corrupted_extracted_installer_payload_is_rejected(tmp_path, monkeypatch):
    bundle = production_fixture(tmp_path / "payload")
    _, entries = validate(bundle)
    work = tmp_path / "work"
    work.mkdir()
    _, inventory = builder.write_payload_include(bundle, entries, work)
    destination = tmp_path / "extract"
    app = destination / "{app}"
    shutil.copytree(bundle, app)
    shutil.copyfile(inventory, app / inventory.name)
    (app / "BaguTrainer.exe").write_bytes(b"corrupted")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="hash"):
        builder.inspect_installer(Path("not-run.exe"), Path("not-run-unpacker.exe"), entries, inventory, destination)


def test_windows_lockfile_matches_msvcrt_owner_lock(tmp_path):
    # Test exactly the Win32 primitive used by the Pascal Script, against the
    # existing Python lock primitive, only in a temporary synthetic directory.
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows byte-range locking")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LockFile.argtypes = [wintypes.HANDLE] + [wintypes.DWORD] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path = tmp_path / "owner.lock"
    path.write_bytes(b"0")
    handle = kernel.CreateFileW(str(path), 0xC0000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert kernel.LockFile(handle, 0, 0, 1, 0)
        with path.open("r+b") as owner:
            with pytest.raises(OSError):
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        kernel.CloseHandle(handle)
        handle = None
        with path.open("r+b") as owner:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            probe = kernel.CreateFileW(str(path), 0xC0000000, 3, None, 3, 0x80, None)
            try:
                assert not kernel.LockFile(probe, 0, 0, 1, 0)
            finally:
                kernel.CloseHandle(probe)
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        if handle:
            kernel.CloseHandle(handle)


def lifecycle_report():
    case = {name: True for name in ("passed", "interactive_wait_dialog_resumed", "cooperative_wait_upgrade_and_uninstall",
        "mapped_image_blocks_upgrade", "same_name_portable_process_preserved", "data_and_synthetic_credential_preserved",
        "legacy_icon_log_compatible", "test_registration_removed")}
    return {"passed": True, "production_installer_executed": False, "windows_credentials_accessed": False,
            "paths_redirected_at_compile_time": True,
            "lifecycle_sha256": builder.sha256(builder.ROOT / "packaging/windows-installer-lifecycle.iss"),
            "installer_script_sha256": builder.sha256(builder.SCRIPT),
            "cases": [{**case, "foreign_shortcuts": foreign, "isolated_app_id": "BaguTrainer.Lifecycle.fixture"}
                      for foreign in (False, True)]}


def test_current_complete_isolated_lifecycle_report_is_accepted(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(lifecycle_report()), encoding="utf-8")
    assert builder.lifecycle_summary(path)["cases_passed"] == 2


@pytest.mark.parametrize("failure", ["failed", "stale", "real-credentials", "unremoved-registration", "missing-foreign-case", "wrong-identity"])
def test_unsafe_or_stale_lifecycle_report_is_rejected(tmp_path, failure):
    report = deepcopy(lifecycle_report())
    if failure == "failed":
        report["passed"] = False
    elif failure == "stale":
        report["lifecycle_sha256"] = "0" * 64
    elif failure == "real-credentials":
        report["windows_credentials_accessed"] = True
    elif failure == "unremoved-registration":
        report["cases"][0]["test_registration_removed"] = False
    elif failure == "missing-foreign-case":
        report["cases"].pop()
    else:
        report["cases"][0]["isolated_app_id"] = builder.APP_ID
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="生命周期"):
        builder.lifecycle_summary(path)
