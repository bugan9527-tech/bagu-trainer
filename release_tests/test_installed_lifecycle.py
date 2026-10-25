"""Script regressions only: synthetic files/fake vault; never install a Setup."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile

import pytest

from scripts.acceptance import installed_release as installed
from scripts.acceptance.contract import INSTALLED_CASES, complete_installed_lifecycle
from scripts.build_portable import source_provenance
from release_tests.test_installer_bundle import production_fixture


def artifact(root, version):
    bundle = production_fixture(root / "BaguTrainer")
    manifest_path = bundle / "BUILD_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(version=version, source_tree=source_provenance(), source_dirty=False)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / f"BaguTrainer-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in bundle.rglob("*"):
            if path.is_file():
                zipped.write(path, "BaguTrainer/" + path.relative_to(bundle).as_posix())
    Path(str(archive) + ".sha256").write_text(installed.sha256(archive) + "  " + archive.name, encoding="ascii")
    setup = root / f"BaguTrainer-{version}-windows-x64-Setup.exe"
    setup.write_bytes(b"synthetic installer fixture: never executable")
    Path(str(setup) + ".sha256").write_text(installed.sha256(setup) + "  " + setup.name, encoding="ascii")
    metadata = {key: manifest[key] for key in ("version", "source_commit", "source_dirty", "source_tree")}
    metadata.update(app_id=installed.APP_ID, payload_manifest_sha256=installed.sha256(manifest_path),
                    installer={"name": setup.name, "sha256": installed.sha256(setup)}, inspection={"all_hashes_match": True})
    (root / "INSTALLER_MANIFEST.json").write_text(json.dumps(metadata), encoding="utf-8")
    return setup, bundle


def test_actual_mode_refuses_host_before_any_folder_vault_or_install_access(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    for name in ("folder", "registration", "TestVault", "run_native", "installer_input"):
        monkeypatch.setattr(installed, name, lambda *a, **k: pytest.fail("Host safety gate was bypassed"))
    with pytest.raises(ValueError, match="disposable"):
        installed.run(Path("never.exe"), Path("never"), Path("never-driver"), tmp_path / "work", tmp_path / "report.json",
                      previous_setup=Path("old-never.exe"), previous_bundle=Path("old-never"))
    assert not list(tmp_path.iterdir())


def test_child_environment_uses_synthetic_profile_without_mutating_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGU_LLM_API_KEY", "synthetic-inherited-key")
    monkeypatch.setenv("BAGU_TEST_INSTANCE", "synthetic-source-test")
    child = installed.environment(tmp_path)
    assert child["BAGU_USER_DATA_DIR"] == str(tmp_path)
    assert child["PYTHON_KEYRING_BACKEND"] == "keyring.backends.Windows.WinVaultKeyring"
    assert "BAGU_LLM_API_KEY" not in child and "BAGU_TEST_INSTANCE" not in child
    assert installed.os.environ["BAGU_LLM_API_KEY"] == "synthetic-inherited-key"


def test_plan_audits_both_packages_and_marks_every_real_case_not_run(tmp_path, monkeypatch):
    setup, bundle = artifact(tmp_path / "new", installed.__version__)
    old_setup, old_bundle = artifact(tmp_path / "old", "0.3.1")
    report = tmp_path / "planned.json"
    monkeypatch.setattr(installed.sys, "argv", ["installed_release.py", "--setup", str(setup),
        "--bundle", str(bundle), "--previous-setup", str(old_setup), "--previous-bundle", str(old_bundle),
        "--plan-only", "--work", str(tmp_path / "unused-work"), "--output", str(report)])
    for name in ("require_hosted_runner", "folder", "TestVault", "run_native"):
        monkeypatch.setattr(installed, name, lambda *a, **k: pytest.fail("Plan accessed native account state"))
    assert installed.main() == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["plan_only"] and result["passed"] is False
    assert result["production_installer_executed"] is False and result["windows_credentials_accessed"] is False
    assert result["installer_sha256"] == installed.sha256(setup)
    assert result["previous_installer"]["sha256"] == installed.sha256(old_setup)
    assert all(case["status"] == "not_run" and not case["passed"] for case in result["cases"].values())
    assert not complete_installed_lifecycle(result)
    assert not (tmp_path / "unused-work").exists()
    with pytest.raises(ValueError, match="overwrite"):
        installed.main()


@pytest.mark.parametrize("mutation", ["setup", "checksum", "manifest_binding", "missing_zip", "same_version"])
def test_wrong_or_missing_installer_attachments_block_native_execution(tmp_path, mutation):
    setup, bundle = artifact(tmp_path / "package", "0.3.1")
    if mutation == "setup":
        setup.write_bytes(b"changed")
    elif mutation == "checksum":
        Path(str(setup) + ".sha256").unlink()
    elif mutation == "manifest_binding":
        path = setup.parent / "INSTALLER_MANIFEST.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["payload_manifest_sha256"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
    elif mutation == "missing_zip":
        next(setup.parent.glob("*.zip")).unlink()
    else:
        setup, bundle = artifact(tmp_path / "same", installed.__version__)
    with pytest.raises((ValueError, OSError)):
        installed.installer_input(setup, bundle, previous=True)


def test_archived_current_version_requires_explicit_opt_in_and_keeps_identity(tmp_path, monkeypatch):
    setup, bundle = artifact(tmp_path, installed.__version__)
    monkeypatch.setattr(installed, "source_provenance", lambda: {"sha256": "different", "files": {}})
    with pytest.raises(ValueError, match="fingerprint differs"):
        installed.installer_input(setup, bundle)
    record, _, _ = installed.installer_input(setup, bundle, allow_archived_source=True)
    assert record["source_matches_checkout"] is False
    assert record["manifest_sha256"] == installed.sha256(bundle / "BUILD_MANIFEST.json")


def test_case_failure_is_saved_without_becoming_passed_or_running_later_cases():
    report = {"cases": {name: {"status": "not_run", "passed": False} for name in INSTALLED_CASES}}
    saved = []
    def fail():
        raise ValueError("synthetic sharing failure")
    assert installed.record_case(report, "cleanup_failure_retry", fail, lambda: saved.append(deepcopy(report))) is False
    assert saved[0]["cases"]["cleanup_failure_retry"]["status"] == "running"
    assert saved[-1]["cases"]["cleanup_failure_retry"]["status"] == "failed"
    assert report["cases"]["complete_uninstall"]["status"] == "not_run"


def test_snapshot_detects_config_bank_and_real_learning_row_changes(tmp_path):
    data = tmp_path / "profile"
    installed.seed_profile(data, tmp_path)
    before = installed.profile_snapshot(data)
    assert before["attempts"] == 1
    import sqlite3
    with sqlite3.connect(data / "data/trainer.db") as connection:
        connection.execute("UPDATE attempts SET correct=0")
    assert installed.profile_snapshot(data)["learning_sha256"] != before["learning_sha256"]
    path = data / "config.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert installed.profile_snapshot(data)["config_sha256"] != before["config_sha256"]


def test_vault_helper_uses_only_known_test_slots_and_checks_unrelated_preservation(tmp_path):
    class FakeVault:
        def __init__(self):
            self.values = {}
        def get_password(self, service, user):
            return self.values.get((service, user))
        def set_password(self, service, user, value):
            self.values[service, user] = value
        def delete_password(self, service, user):
            del self.values[service, user]
    backend = FakeVault()
    vault = installed.TestVault(tmp_path, backend=backend)
    vault.ensure_empty()
    vault.seed()
    vault.preserved()
    backend.delete_password(vault.service, "llm_api_key")
    vault.cleared()
    vault.retire_unrelated()
    assert backend.values == {}
    backend.set_password("bagu-trainer", "llm_api_key", "pre-existing synthetic fixture")
    with pytest.raises(ValueError, match="already has a credential"):
        vault.ensure_empty()


@pytest.mark.skipif(installed.sys.platform != "win32", reason="Windows sharing primitive")
def test_real_failure_injection_locks_only_a_temp_file_and_can_be_released(tmp_path):
    target = tmp_path / "synthetic.db"
    target.write_bytes(b"synthetic sharing test, not a real user database")
    with installed.deny_delete(target):
        with pytest.raises(PermissionError):
            target.unlink()
    target.unlink()
    assert not target.exists()
