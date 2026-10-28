"""Privacy scanner regressions use generated synthetic bytes, never credentials."""
import io
import zipfile
import json

from scripts.audit_release import Scanner


def test_unknown_token_is_reported_without_echoing_matched_content():
    token = b"sk-" + b"A" * 31
    scanner = Scanner(markers={})
    scanner.scan("source/tests/unknown.py", token)
    assert scanner.findings == [{"member": "source/tests/unknown.py", "rule": "provider_key"}]
    assert token.decode() not in repr(scanner.findings)


def test_test_directory_is_not_a_blanket_secret_exemption():
    scanner = Scanner(markers={})
    scanner.scan("source/tests/test_privacy.py", b"sk-" + b"B" * 31)
    assert scanner.findings and scanner.synthetic_fixture_matches == 0


def test_machine_path_is_detected_in_utf16_and_nested_zip():
    value = "C:/Users/Synthetic/private"
    scanner = Scanner(markers={"local_machine_path": [value.encode("utf-16-le").lower()]})
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("fixture.txt", value.encode("utf-16-le"))
    scanner.scan("payload/library.zip", raw.getvalue())
    assert any(item["member"] == "payload/library.zip!/fixture.txt" for item in scanner.findings)


def test_compiled_code_filenames_and_constants_are_scanned():
    scanner = Scanner(markers={"local_machine_path": [b"synthetic-private-root"]})
    scanner.code("module", compile("def inner(): return 'synthetic-private-root'", "synthetic-private-root/module.py", "exec"))
    assert scanner.code_objects_scanned == 2 and scanner.findings


def test_electron_audit_checks_both_python_archives_instead_of_chromium(tmp_path, monkeypatch):
    (tmp_path / "BUILD_MANIFEST.json").write_text(json.dumps({"layout": "Electron with Python engine"}), encoding="utf-8")
    seen = []
    scanner = Scanner(markers={})
    monkeypatch.setattr(scanner, "frozen_exe", lambda path: seen.append(path.name))
    scanner.frozen_bundle(tmp_path)
    assert seen == ["BaguBackend.exe", "BaguCleanup.exe"]


def test_legacy_bundle_still_audits_its_main_python_archive(tmp_path, monkeypatch):
    (tmp_path / "BUILD_MANIFEST.json").write_text(json.dumps({"layout": "PyInstaller onedir"}), encoding="utf-8")
    seen = []
    scanner = Scanner(markers={})
    monkeypatch.setattr(scanner, "frozen_exe", lambda path: seen.append(path.name))
    scanner.frozen_bundle(tmp_path)
    assert seen == ["BaguTrainer.exe"]
