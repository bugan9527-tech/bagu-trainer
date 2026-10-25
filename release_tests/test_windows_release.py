"""Synthetic dual-artifact gates; these tests never install or contact GitHub."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts import build_installer as builder, release_portable as release
from scripts import prepare_installer_tools as tools
from scripts.acceptance import installed_release as installed
from scripts.build_portable import source_provenance
from scripts.acceptance.contract import FIRST_MILESTONES, RESTART_MILESTONES, REQUESTS, INSTALLED_CASES

SHA = "a" * 40


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(root):
    dist, evidence = root / "dist", root / "validation"
    dist.mkdir()
    evidence.mkdir()
    tree = source_provenance()
    binary = b"synthetic production EXE"
    manifest = {"version": release.__version__, "architecture": "windows-x64", "source_commit": SHA,
                "source_dirty": False, "source_tree": tree,
                "files": [{"path": "BaguTrainer.exe", "bytes": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}]}
    raw = json.dumps(manifest).encode()
    archive = dist / f"BaguTrainer-{release.__version__}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("BaguTrainer/BaguTrainer.exe", binary)
        bundle.writestr("BaguTrainer/BUILD_MANIFEST.json", raw)
    Path(str(archive) + ".sha256").write_text(builder.sha256(archive) + "  " + archive.name, encoding="ascii")
    setup = dist / f"BaguTrainer-{release.__version__}-windows-x64-Setup.exe"
    setup.write_bytes(b"synthetic Setup; never executed")
    Path(str(setup) + ".sha256").write_text(builder.sha256(setup) + "  " + setup.name, encoding="ascii")
    source = {"version": release.__version__, "source_commit": SHA, "source_dirty": False, "source_tree_sha256": tree["sha256"]}
    case = {name: True for name in ("passed", "interactive_wait_dialog_resumed", "cooperative_wait_upgrade_and_uninstall",
        "mapped_image_blocks_upgrade", "same_name_portable_process_preserved", "data_and_synthetic_credential_preserved",
        "legacy_icon_log_compatible", "test_registration_removed")}
    lifecycle = {**source, "passed": True, "production_installer_executed": False, "windows_credentials_accessed": False,
        "paths_redirected_at_compile_time": True, "lifecycle_sha256": builder.sha256(builder.ROOT / "packaging/windows-installer-lifecycle.iss"),
        "installer_script_sha256": builder.sha256(builder.SCRIPT),
        "cases": [{**case, "foreign_shortcuts": foreign, "isolated_app_id": "BaguTrainer.Lifecycle." + str(foreign)} for foreign in (False, True)]}
    write(dist / "INSTALLER_LIFECYCLE.json", lifecycle)
    inputs = (builder.SCRIPT, builder.ROOT / "packaging/windows-installer-lifecycle.iss", builder.TOOLCHAIN,
        builder.ROOT / "scripts/build_installer.py", builder.ROOT / "packaging/BaguTrainer.ico",
        builder.ROOT / "packaging/languages/ChineseSimplified.isl", builder.ROOT / "LICENSE")
    metadata = {"application": "BaguTrainer", "version": release.__version__, "architecture": "windows-x64", "app_id": builder.APP_ID,
        "source_commit": SHA, "source_dirty": False, "source_tree": tree, "payload_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "installer": {"name": setup.name, "sha256": builder.sha256(setup)},
        "iscc_version": json.loads(builder.TOOLCHAIN.read_text(encoding="utf-8"))["inno_setup"]["version"],
        "build_inputs": {p.relative_to(builder.ROOT).as_posix(): builder.sha256(p) for p in inputs},
        "inspection": {"method": "innounp extract; no installation", "files_verified": 3, "all_hashes_match": True, "application_executed": False},
        "lifecycle_acceptance": builder.lifecycle_summary(dist / "INSTALLER_LIFECYCLE.json")}
    write(dist / "INSTALLER_MANIFEST.json", metadata)
    production = {**source, "installer_sha256": builder.sha256(setup), "payload_manifest_sha256": metadata["payload_manifest_sha256"],
        "runner": "github-hosted Windows", "credential_backend": "native Windows vault / synthetic entries in disposable account",
        "windows_credentials_accessed": True, "real_model_requests": False, "schema_version": 2,
        "test_credentials_only": True, "real_user_credentials_accessed": False,
        "previous_installer": {"version": "0.3.1", "sha256": "c" * 64},
        "cases": {name: {"status": "passed", "passed": True} for name in INSTALLED_CASES}}
    production.update({name: True for name in ("passed", "production_installer_executed", "personal_data_isolated", "installation",
        "registration", "shortcuts", "installed_frozen_acceptance", "same_version_overwrite", "enabled_startup_repaired",
        "installed_runtime", "uninstall", "managed_data_removed", "test_credential_removed", "registration_removed", "pristine_first_use",
        "no_preset_projects_history_or_key", "repeat_install", "running_upgrade_blocked", "running_uninstall_blocked", "default_install_path",
        "upgrade_data_preserved", "upgrade_credentials_preserved", "repair_data_preserved", "repair_credentials_preserved",
        "cleanup_failure_blocked", "cleanup_retry_succeeded", "unrelated_files_preserved", "unrelated_credential_preserved",
        "owned_shortcuts_removed", "reinstall_pristine")})
    write(evidence / "INSTALLED_ACCEPTANCE.json", production)
    frozen = {"passed": True, "frozen": True, "source_commit": SHA, "source_dirty": False, "production_commit": SHA,
        "production_dirty": False, "source_tree": tree, "production_source_tree": tree["sha256"],
        "first": {"passed": True, "credential_backend": "memory fixture"}, "restart": {"passed": True, "credential_backend": "memory fixture"},
        "production_exe": {"first_start": "running", "upgraded_start": "running", "history_retained": True}}
    frozen["first"]["milestones"] = sorted(FIRST_MILESTONES)
    frozen["restart"]["milestones"] = sorted(RESTART_MILESTONES)
    frozen["requests"] = REQUESTS
    write(evidence / "ACCEPTANCE.json", frozen)
    (evidence / "release-tests.xml").write_text('<testsuites><testsuite><testcase classname="release" name="fixture" time="1"/></testsuite></testsuites>', encoding="utf-8")
    add_privacy(dist, evidence)
    return dist, evidence


def add_privacy(dist, evidence, metadata=None):
    metadata = metadata or dist
    names = ("release-tests.xml", "ACCEPTANCE.json", "INSTALLED_ACCEPTANCE.json", "INSTALLER_MANIFEST.json", "INSTALLER_LIFECYCLE.json")
    report = {"schema_version": 1, "version": release.__version__, "source_commit": SHA,
        "source_tree_sha256": source_provenance()["sha256"], "source_files": 1, "source_only": False,
        "credentials_accessed": False, "findings_include_matched_values": False,
        "artifacts": {p.name: builder.sha256(p) for p in dist.iterdir() if p.suffix in {".exe", ".zip"}},
        "attachment_files": {name: builder.sha256((evidence if (evidence/name).is_file() else metadata)/name) for name in names},
        "files_and_constants_scanned": 100, "code_objects_scanned": 1, "reviewed_synthetic_fixture_matches": 0,
        "findings": [], "passed": True}
    write(evidence / "PRIVACY_AUDIT.json", report)


def validate(dist, evidence, **kwargs):
    return release.validate_dual_release(dist, "v" + release.__version__, SHA, evidence, **kwargs)


def test_both_artifacts_and_current_evidence_pass_and_notes_prioritize_setup(tmp_path):
    dist, evidence = fixture(tmp_path)
    result = validate(dist, evidence)
    notes = release.render_notes("owner/repo", "v" + release.__version__, SHA, result["archive"], result["digest"],
                                 result["setup"], result["setup_digest"], tests=result["tests"])
    assert notes.index(result["setup"].name) < notes.index(result["archive"].name)
    assert "草稿" in notes and "无需管理员" in notes and result["setup_digest"] in notes


def test_legacy_uninstall_preserves_data_report_cannot_certify_040(tmp_path):
    dist, evidence = fixture(tmp_path)
    path = evidence / "INSTALLED_ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    for key in ("schema_version", "cases", "previous_installer", "test_credentials_only", "real_user_credentials_accessed"):
        report.pop(key, None)
    report["personal_data_preserved"] = True
    write(path, report)
    with pytest.raises(ValueError, match="acceptance"):
        validate(dist, evidence)


@pytest.mark.parametrize("case", INSTALLED_CASES)
@pytest.mark.parametrize("status", ["not_run", "running", "failed", "missing"])
def test_each_real_install_lifecycle_case_is_required(tmp_path, case, status):
    dist, evidence = fixture(tmp_path)
    path = evidence / "INSTALLED_ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if status == "missing":
        del report["cases"][case]
    else:
        report["cases"][case]["status"] = status
    write(path, report)
    with pytest.raises(ValueError, match="acceptance"):
        validate(dist, evidence)


@pytest.mark.parametrize("key,value", [("test_credentials_only", False), ("real_user_credentials_accessed", True),
    ("previous_installer", {"version": "0.4.0", "sha256": "c" * 64}), ("schema_version", 1)])
def test_native_credential_and_previous_package_scope_is_mandatory(tmp_path, key, value):
    dist, evidence = fixture(tmp_path)
    path = evidence / "INSTALLED_ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report[key] = value
    write(path, report)
    with pytest.raises(ValueError, match="acceptance"):
        validate(dist, evidence)


@pytest.mark.parametrize("key,value", [("source_commit", "b" * 40), ("source_dirty", True), ("version", "9.9.9"),
    ("payload_manifest_sha256", "0" * 64), ("inspection", {}), ("lifecycle_acceptance", {}), ("build_inputs", {}), ("source_tree", {})])
def test_setup_metadata_must_match_zip_clean_source_and_inspection(tmp_path, key, value):
    dist, evidence = fixture(tmp_path)
    path = dist / "INSTALLER_MANIFEST.json"
    value2 = json.loads(path.read_text(encoding="utf-8"))
    value2[key] = value
    write(path, value2)
    with pytest.raises(ValueError):
        validate(dist, evidence)


@pytest.mark.parametrize("key", ["passed", "production_installer_executed", "shortcuts", "installed_frozen_acceptance",
    "same_version_overwrite", "enabled_startup_repaired", "uninstall", "registration_removed", "managed_data_removed", "personal_data_isolated",
    "pristine_first_use", "no_preset_projects_history_or_key", "repeat_install", "running_upgrade_blocked", "running_uninstall_blocked", "default_install_path"])
def test_every_production_install_gate_is_mandatory(tmp_path, key):
    dist, evidence = fixture(tmp_path)
    path = evidence / "INSTALLED_ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report[key] = False
    write(path, report)
    with pytest.raises(ValueError, match="acceptance"):
        validate(dist, evidence)


@pytest.mark.parametrize("key,value", [("installer_sha256", "0" * 64), ("source_commit", "b" * 40), ("source_dirty", True),
    ("source_tree_sha256", "0" * 64), ("windows_credentials_accessed", False), ("real_model_requests", True),
    ("runner", "self-hosted"), ("credential_backend", "Windows vault")])
def test_stale_or_unsafe_installer_run_blocks_release(tmp_path, key, value):
    dist, evidence = fixture(tmp_path)
    path = evidence / "INSTALLED_ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report[key] = value
    write(path, report)
    with pytest.raises(ValueError):
        validate(dist, evidence)


@pytest.mark.parametrize("which", ["setup", "zip", "setup-checksum", "missing-install-report", "missing-lifecycle", "stale-lifecycle"])
def test_corrupt_or_missing_artifact_and_report_fail(tmp_path, which):
    dist, evidence = fixture(tmp_path)
    if which in {"setup", "zip"}:
        next(dist.glob("*Setup.exe" if which == "setup" else "*.zip")).write_bytes(b"tampered")
    elif which == "setup-checksum":
        next(dist.glob("*.exe.sha256")).write_text("0" * 64)
    elif which == "missing-install-report":
        (evidence / "INSTALLED_ACCEPTANCE.json").unlink()
    elif which == "missing-lifecycle":
        (dist / "INSTALLER_LIFECYCLE.json").unlink()
    else:
        path = dist / "INSTALLER_LIFECYCLE.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["source_dirty"] = True
        write(path, report)
    with pytest.raises((ValueError, FileNotFoundError)):
        validate(dist, evidence)


def test_public_reports_remove_machine_paths_credentials_and_test_output_and_revalidate(tmp_path):
    dist, evidence = fixture(tmp_path)
    marker = r"C:\Users\PRIVATE-RUNNER\secret-profile"
    for name in ("ACCEPTANCE.json", "INSTALLED_ACCEPTANCE.json"):
        path = evidence / name
        report = json.loads(path.read_text(encoding="utf-8"))
        report["private_path"] = marker
        report["synthetic_key"] = "test-only-credential-must-disappear"
        if name == "INSTALLED_ACCEPTANCE.json":
            report["cases"]["first_install"].update(private_path=marker, synthetic_key="test-only-credential-must-disappear")
        write(path, report)
    for name in ("INSTALLER_MANIFEST.json", "INSTALLER_LIFECYCLE.json"):
        path = dist / name
        report = json.loads(path.read_text(encoding="utf-8"))
        report["private_path"] = marker
        write(path, report)
    path = dist / "INSTALLER_MANIFEST.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["lifecycle_acceptance"] = builder.lifecycle_summary(dist / "INSTALLER_LIFECYCLE.json")
    write(path, metadata)
    (evidence / "release-tests.xml").write_text('<testsuites><testsuite hostname="PRIVATE-RUNNER"><properties><property name="path" value="secret-profile"/></properties><testcase classname="release" name="fixture[synthetic-param-must-disappear]"><system-out>test-only-credential-must-disappear</system-out></testcase></testsuite></testsuites>')
    result = validate(dist, evidence, require_privacy=False)
    public = evidence / "public"
    release.write_public_evidence(result, evidence, public, SHA)
    for path in public.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "PRIVATE-RUNNER" not in text and "secret-profile" not in text and "test-only-credential" not in text
        assert "synthetic-param-must-disappear" not in text
    import xml.etree.ElementTree as ET
    assert ET.parse(public / "release-tests.xml").find(".//testcase").get("name") == "fixture"
    add_privacy(dist, public, public)
    assert release.validate_dual_release(dist, "v" + release.__version__, SHA, public, public)["tests"] == {"passed": 1, "skipped": 0}


@pytest.mark.parametrize("variable,value", [("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted")])
def test_production_setup_refuses_workstation_and_self_hosted(monkeypatch, variable, value):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match="disposable"):
        installed.require_hosted_runner()


def test_tool_download_rejects_wrong_hash_before_tool_execution(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"corrupted download"))
    with pytest.raises(ValueError, match="SHA-256"):
        tools.download({"url": "https://example.invalid/pinned-1.zip", "sha256": "0" * 64}, tmp_path / "tool.zip")


def test_junit_suite_error_without_testcase_error_still_blocks_release(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text('<testsuites errors="1"><testsuite><testcase name="seems-passing"/></testsuite></testsuites>')
    with pytest.raises(ValueError, match="without skips"):
        release.test_summary(report)


def test_frozen_report_cannot_skip_the_existing_no_page_stealing_milestone(tmp_path):
    dist, evidence = fixture(tmp_path)
    path = evidence / "ACCEPTANCE.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["first"]["milestones"].remove("new_quiz_updates_pending_without_stealing_page")
    write(path, report)
    with pytest.raises(ValueError, match="incomplete"):
        validate(dist, evidence)


def test_workflow_keeps_all_gates_and_never_runs_production_installer_on_local_machine():
    workflow = (builder.ROOT / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    for command in ("build_portable.py --require-clean", "run_release_tests.py", "build_acceptance.py --verify --memory-credentials",
        "prepare_installer_tools.py", "installer_lifecycle.py", "build_installer.py", "installed_release.py", "release_portable.py"):
        assert command in workflow
    assert "continue-on-error" not in workflow and "pull_request_target" not in workflow
    assert "needs: build-and-test" in workflow and "--publish-draft" in workflow
    assert "dist/BaguTrainer-*-windows-x64-Setup.exe.sha256" in workflow
    assert "windows-release-${{ github.run_id }}-${{ github.run_attempt }}" in workflow


def test_cli_uploads_both_production_artifacts_and_only_public_reports(tmp_path, monkeypatch):
    dist, evidence = fixture(tmp_path)
    public = evidence / "public"
    calls = []
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(release, "verify_checkout", lambda _target: None)
    monkeypatch.setattr(release.sys, "argv", ["release_portable.py", "--tag", "v" + release.__version__, "--target", SHA,
        "--repository", "owner/repo", "--dist", str(dist), "--evidence", str(evidence),
        "--output", str(public / "RELEASE_NOTES.md"), "--publish-draft"])
    monkeypatch.setattr(release, "publish_draft", lambda *_args: calls.append(_args))
    assert release.main() == 0
    assets = calls[0][-1]
    assert len(assets) == 11
    assert sum(path.suffix == ".exe" for path in assets) == 1
    assert sum(path.suffix == ".zip" for path in assets) == 1
    assert all(path.parent == public for path in assets if path.suffix in {".json", ".xml"})
    # Re-run the draft job's exact validation against sanitized inputs.
    release.validate_dual_release(dist, "v" + release.__version__, SHA, public, public)


@pytest.mark.parametrize("change", [{"passed": False}, {"source_only": True}, {"credentials_accessed": True},
    {"source_commit": "b" * 40}, {"attachment_files": {}}, {"findings": [{"rule": "local_machine_path"}]}])
def test_missing_unsafe_or_mismatched_privacy_proof_blocks_release(tmp_path, change):
    dist, evidence = fixture(tmp_path)
    path = evidence / "PRIVACY_AUDIT.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report.update(change)
    write(path, report)
    with pytest.raises(ValueError, match="Privacy"):
        validate(dist, evidence)


@pytest.mark.parametrize("failure", ["wrong-checkout", "dirty", "untracked-source"])
def test_release_checkout_guard_rejects_wrong_dirty_or_untracked_source(monkeypatch, failure):
    files = sorted(source_provenance()["files"])
    replies = iter(["b" * 40 if failure == "wrong-checkout" else SHA,
                    " M scripts/release_portable.py" if failure == "dirty" else "",
                    "\n".join(files[:-1] if failure == "untracked-source" else files)])
    monkeypatch.setattr(release.subprocess, "check_output", lambda *_a, **_k: next(replies))
    with pytest.raises(ValueError, match="Release"):
        release.verify_checkout(SHA)
