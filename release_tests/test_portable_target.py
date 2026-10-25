"""Portable gates use small synthetic artifacts; no application is executed."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from bagu_trainer.version import __version__
from release_tests.test_installer_bundle import production_fixture
from scripts import portable_artifacts as artifacts, run_release_tests as runner
from scripts.build_portable import source_provenance


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def zip_directory(directory, archive):
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in directory.rglob("*"):
            if path.is_file():
                zipped.write(path, directory.name + "/" + path.relative_to(directory).as_posix())
    Path(str(archive) + ".sha256").write_text(artifacts.sha256(archive) + "  " + archive.name + "\n", encoding="ascii")


@pytest.fixture
def target(tmp_path, monkeypatch):
    for name in ("BAGU_PORTABLE_BUNDLE", "BAGU_PORTABLE_ARCHIVE", "BAGU_PORTABLE_OUTPUT_DIR"):
        monkeypatch.delenv(name, raising=False)
    target = artifacts.resolve_target(tmp_path / "new build")
    production_fixture(target.bundle)
    path = target.bundle / "BUILD_MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source_tree"] = source_provenance()
    write(path, manifest)
    zip_directory(target.bundle, target.archive)
    target.driver.parent.mkdir()
    target.driver.write_bytes(b"synthetic acceptance driver")
    provenance = target.driver.parent / "_internal" / "acceptance-provenance.json"
    provenance.parent.mkdir()
    write(provenance, {key: manifest[key] for key in ("version", "source_commit", "source_dirty", "source_tree")})
    zip_directory(target.driver.parent, target.output_dir / "BaguTrainerAcceptance-windows-x64.zip")
    return target


def test_explicit_output_dir_wins_over_ambient_paths(target, monkeypatch, tmp_path):
    monkeypatch.setenv("BAGU_PORTABLE_BUNDLE", str(tmp_path / "old" / "BaguTrainer"))
    monkeypatch.setenv("BAGU_PORTABLE_ARCHIVE", str(tmp_path / "old.zip"))
    assert artifacts.resolve_target(target.output_dir) == target


def test_single_environment_override_derives_companion_and_never_scans(target, monkeypatch):
    monkeypatch.setenv("BAGU_PORTABLE_BUNDLE", str(target.bundle))
    assert artifacts.resolve_target() == target
    monkeypatch.delenv("BAGU_PORTABLE_BUNDLE")
    monkeypatch.setenv("BAGU_PORTABLE_ARCHIVE", str(target.archive))
    assert artifacts.resolve_target() == target
    monkeypatch.delenv("BAGU_PORTABLE_ARCHIVE")
    monkeypatch.setenv("BAGU_PORTABLE_OUTPUT_DIR", str(target.output_dir))
    assert artifacts.resolve_target() == target


def test_normal_target_records_paths_version_and_hashes(target):
    result = artifacts.validate_target(target, driver=target.driver)
    assert result["passed"] and result["version"] == __version__
    assert result["bundle"] == str(target.bundle)
    assert result["archive"] == str(target.archive)
    assert result["manifest_sha256"] == artifacts.sha256(target.bundle / "BUILD_MANIFEST.json")
    assert result["portable"]["sha256"] == artifacts.sha256(target.archive)
    assert result["exe_sha256"] == artifacts.sha256(target.bundle / "BaguTrainer.exe")
    assert result["driver"]["sha256"] == artifacts.sha256(target.driver)


@pytest.mark.parametrize("name", ["version", "fingerprint", "inventory"])
def test_old_or_changed_target_is_rejected(target, name):
    path = target.bundle / "BUILD_MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if name == "version":
        manifest["version"] = "0.3.0"
    elif name == "fingerprint":
        manifest["source_tree"]["sha256"] = "0" * 64
    else:
        (target.bundle / "BaguTrainer.exe").write_bytes(b"changed")
    write(path, manifest)
    with pytest.raises(ValueError, match="version|fingerprint|BUILD_MANIFEST"):
        artifacts.validate_target(target)


@pytest.mark.parametrize("name", ["portable_zip", "portable_checksum", "driver", "driver_provenance", "driver_zip", "driver_checksum"])
def test_missing_attachment_or_driver_fails_closed(target, name):
    driver_zip = target.output_dir / "BaguTrainerAcceptance-windows-x64.zip"
    path = {"portable_zip": target.archive, "portable_checksum": Path(str(target.archive) + ".sha256"),
            "driver": target.driver, "driver_provenance": target.driver.parent / "_internal/acceptance-provenance.json",
            "driver_zip": driver_zip, "driver_checksum": Path(str(driver_zip) + ".sha256")}[name]
    path.unlink()
    with pytest.raises((ValueError, OSError)):
        artifacts.validate_target(target, driver=target.driver)


@pytest.mark.parametrize("mutation", ["checksum", "payload", "duplicate", "manifest"])
def test_zip_and_checksum_must_bind_to_exact_directory(target, mutation):
    if mutation == "checksum":
        Path(str(target.archive) + ".sha256").write_text("0" * 64 + "  wrong.zip", encoding="ascii")
    else:
        with zipfile.ZipFile(target.archive, "a") as zipped:
            if mutation == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate"):
                    zipped.writestr("BaguTrainer/BaguTrainer.exe", b"another")
            else:
                # Recreate ZIP to preserve a valid checksum while changing an entry.
                entries = {name: zipped.read(name) for name in zipped.namelist()}
        if mutation != "duplicate":
            key = "BaguTrainer/" + ("BaguTrainer.exe" if mutation == "payload" else "BUILD_MANIFEST.json")
            entries[key] = b"different build"
            with zipfile.ZipFile(target.archive, "w") as zipped:
                for name, data in entries.items():
                    zipped.writestr(name, data)
        Path(str(target.archive) + ".sha256").write_text(artifacts.sha256(target.archive) + "  " + target.archive.name, encoding="ascii")
    with pytest.raises(ValueError, match="SHA-256|ZIP"):
        artifacts.validate_target(target)


@pytest.mark.parametrize("key,value", [("version", "0.3.0"), ("source_tree", {}), ("source_commit", "b" * 40)])
def test_driver_from_another_build_is_rejected(target, key, value):
    path = target.driver.parent / "_internal/acceptance-provenance.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record[key] = value
    write(path, record)
    with pytest.raises(ValueError, match="driver.*differs"):
        artifacts.validate_target(target, driver=target.driver)


def test_frozen_driver_uses_embedded_source_instead_of_host_checkout(target, monkeypatch):
    record = json.loads((target.driver.parent / "_internal/acceptance-provenance.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("scripts.build_portable.source_provenance", lambda: pytest.fail("Guest has no checkout"))
    assert artifacts.validate_target(target, expected_source=record, driver=target.driver)["passed"]


def test_renamed_extracted_directory_still_binds_to_canonical_zip(target, tmp_path):
    renamed = tmp_path / "中文 程序"
    shutil.copytree(target.bundle, renamed)
    assert artifacts.validate_target(artifacts.resolve_target(target.output_dir, bundle=renamed))["passed"]


def test_runner_binds_children_and_report_to_validated_target(target, tmp_path, monkeypatch):
    output = tmp_path / "reports/tests.xml"
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--output-dir", str(target.output_dir),
        "--output", str(output), "--module", "test_release_runner"])
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs["env"])
        report = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")))
        report.write_text('<testsuite tests="1"><testcase name="fixture"/></testsuite>', encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.main() == 0
    assert calls[0]["BAGU_PORTABLE_BUNDLE"] == str(target.bundle)
    assert calls[0]["BAGU_PORTABLE_ARCHIVE"] == str(target.archive)
    report = json.loads(output.with_suffix(".artifact.json").read_text(encoding="utf-8"))
    assert report["bundle"] == str(target.bundle) and report["version"] == __version__
    with pytest.raises(ValueError, match="overwrite"):
        runner.main()


def test_builder_refuses_existing_artifacts_before_invoking_pyinstaller(target, monkeypatch):
    from scripts import build_portable
    monkeypatch.setattr(build_portable.sys, "argv", ["builder", "--output-dir", str(target.output_dir)])
    monkeypatch.setattr(build_portable.subprocess, "run", lambda *a, **k: pytest.fail("Existing build would be overwritten"))
    with pytest.raises(ValueError, match="overwrite"):
        build_portable.main()


@pytest.mark.parametrize("mutation", ["old", "missing_zip"])
def test_desktop_acceptance_gates_before_work_or_gui(target, tmp_path, monkeypatch, mutation):
    from scripts.acceptance import desktop_acceptance
    if mutation == "old":
        path = target.bundle / "BUILD_MANIFEST.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["version"] = "0.3.0"
        write(path, record)
    else:
        target.archive.unlink()
    work = tmp_path / "acceptance-work"
    monkeypatch.setattr(desktop_acceptance.subprocess, "run", lambda *a, **k: pytest.fail("GUI started before validation"))
    options = SimpleNamespace(work=work, production_exe=None, output_dir=target.output_dir, archive=None)
    with pytest.raises(ValueError):
        desktop_acceptance.orchestrate(options)
    assert not work.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell sandbox preparation")
def test_sandbox_copies_only_verified_target_and_preserves_previous_evidence(target, tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "sandbox evidence"
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(root / "scripts/acceptance/create-sandbox.ps1"), "-BuildOutputDirectory", str(target.output_dir),
               "-OutputDirectory", str(output), "-Python", sys.executable]
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "PORTABLE_TARGET.json").read_text(encoding="utf-8"))
    assert report["bundle"] == str(target.bundle) and report["passed"]
    copied = output / "clean-windows-inputs" / target.archive.name
    assert artifacts.sha256(copied) == artifacts.sha256(target.archive)
    before = artifacts.sha256(output / "PORTABLE_TARGET.json")
    assert subprocess.run(command, capture_output=True, timeout=30).returncode != 0
    assert artifacts.sha256(output / "PORTABLE_TARGET.json") == before
