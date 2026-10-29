"""Read-only privacy scan of source, production payloads and release attachments.

Findings contain rule names and relative member names, never matched secrets.
Does not read the user's configuration, credential store or environment Key.
"""
import argparse
import io
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_portable import audit, source_provenance
from scripts.build_installer import sha256, safe_path, check_unpacker
from bagu_trainer.version import __version__

SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("provider_key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
)
# Reviewed self-authored privacy test fixtures. Exact-file hashes mean that
# adding another token, even in these files, invalidates the exemption.
# Hashes use the LF bytes enforced by .gitattributes (identical Git blobs).
SYNTHETIC_TEST_FILES = {
    "source/release_tests/test_first_use.py": "834e36ec40681c966a07945e50216ea2b65e44f24a87075f4be1261c6bb423fc",
    "source/tests/test_privacy.py": "1a1bc6be67a7dfe39221a19a48757690b7d7bf9197f070b01ddf57dde33347b8",
    "source/tests/test_secret_migration.py": "8f5f504fd788c9b0db80b5b988426535ecb00fa8a9fc190558b8cdc2c3f88c0a",
}


def local_markers():
    # No Key values are queried. Only the build machine's path/host identifiers.
    values = {str(ROOT), str(Path.home())}
    host = os.environ.get("COMPUTERNAME", "")
    if len(host) >= 6:
        values.add(host)
    return {"local_machine_path": [value.replace("\\", slash).encode(encoding).lower()
            for value in values for slash in ("/", "\\") for encoding in ("utf-8", "utf-16-le")]}


class Scanner:
    def __init__(self, markers=None):
        self.markers = local_markers() if markers is None else markers
        self.findings = []
        self.files_scanned = 0
        self.code_objects_scanned = 0
        self.synthetic_fixture_matches = 0

    def scan(self, name, value, *, nested=True):
        self.files_scanned += 1
        lower = value.lower()
        for rule, needles in self.markers.items():
            if any(needle and needle in lower for needle in needles):
                self.findings.append({"member": name, "rule": rule})
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                if SYNTHETIC_TEST_FILES.get(name) == hashlib.sha256(value).hexdigest():
                    self.synthetic_fixture_matches += 1
                else:
                    self.findings.append({"member": name, "rule": rule})
        if nested and value[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(value)):
            with zipfile.ZipFile(io.BytesIO(value)) as archive:
                for item in archive.infolist():
                    if not item.is_dir():
                        self.scan(name + "!/" + item.filename, archive.read(item), nested=False)

    def code(self, name, code):
        if not isinstance(code, types.CodeType):
            return
        self.code_objects_scanned += 1
        self.scan(name + ":filename", code.co_filename.encode("utf-8"), nested=False)
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                self.code(name, value)
            elif isinstance(value, str):
                self.scan(name + ":constant", value.encode("utf-8"), nested=False)

    def frozen_exe(self, path):
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(path))
        for name, entry in archive.toc.items():
            if entry[-1] == "z":
                pyz = archive.open_embedded_archive(name)
                for module in pyz.toc:
                    self.code("frozen/" + module, pyz.extract(module))
            elif entry[-1] == "s":
                import marshal
                self.code("frozen/" + name, marshal.loads(archive.extract(name)))

    def frozen_bundle(self, bundle):
        """Inspect the Python archives behind either supported desktop shell."""
        manifest = json.loads((bundle / "BUILD_MANIFEST.json").read_text(encoding="utf-8"))
        names = ("BaguBackend.exe", "BaguCleanup.exe") if manifest.get("layout") == "Electron with Python engine" else ("BaguTrainer.exe",)
        for name in names:
            self.frozen_exe(bundle / name)


def scan_source(scanner):
    raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    names = sorted(set(item.decode("utf-8") for item in raw.split(b"\0") if item))
    private_names = {"config.json", "window.json", "owner.lock", "bridge-credentials.json", "questions.json"}
    for name in names:
        path = ROOT / name
        if path.name.lower() in private_names or path.suffix.lower() in {".db", ".sqlite", ".key", ".pem", ".log"}:
            scanner.findings.append({"member": "source/" + name, "rule": "private_source_filename_not_read"})
            continue
        if path.is_file():
            scanner.scan("source/" + name, path.read_bytes())
    return len(names)


def run(bundle=None, archive=None, setup=None, unpacker=None, attachments=None, source_only=False):
    scanner = Scanner()
    report = {"schema_version": 1, "version": __version__, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_tree_sha256": source_provenance()["sha256"], "source_files": scan_source(scanner),
              "source_only": source_only,
              "credentials_accessed": False, "findings_include_matched_values": False, "artifacts": {}, "attachment_files": {}}
    if not source_only:
        if not all((bundle, archive, setup, unpacker)):
            raise ValueError("Full scan requires --bundle, --zip, --setup and --innounp")
        for entry in audit(bundle):
            scanner.scan("payload/" + entry["path"], (bundle / entry["path"]).read_bytes())
        scanner.frozen_bundle(bundle)
        with zipfile.ZipFile(archive) as zipped:
            for item in zipped.infolist():
                if item.is_dir():
                    continue
                safe_path(item.filename)
                scanner.scan("zip/" + item.filename, zipped.read(item))
        check_unpacker(unpacker)
        with tempfile.TemporaryDirectory(prefix="privacy-inspection-", dir=ROOT / "build") as temporary:
            dest = Path(temporary)
            subprocess.run([str(unpacker.resolve()), "-x", "-m", "-a", "-y", "-b", "-d" + str(dest), str(setup.resolve())],
                           check=True, stdout=subprocess.DEVNULL)
            audit(dest / "{app}")
            for path in dest.rglob("*"):
                if path.is_file():
                    scanner.scan("installer/" + path.relative_to(dest).as_posix(), path.read_bytes())
        for path in (archive, setup):
            report["artifacts"][path.name] = sha256(path)
        if attachments:
            for path in attachments.rglob("*"):
                if path.is_file():
                    scanner.scan("attachment/" + path.relative_to(attachments).as_posix(), path.read_bytes())
                    if path.name not in {"PRIVACY_AUDIT.json", "RELEASE_VALIDATION.json", "RELEASE_NOTES.md"}:
                        report["attachment_files"][path.relative_to(attachments).as_posix()] = sha256(path)
    report.update({"files_and_constants_scanned": scanner.files_scanned, "code_objects_scanned": scanner.code_objects_scanned,
                   "reviewed_synthetic_fixture_matches": scanner.synthetic_fixture_matches,
                   "findings": scanner.findings, "passed": not scanner.findings})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--innounp", type=Path)
    parser.add_argument("--attachments", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.bundle, args.zip, args.setup, args.innounp, args.attachments, args.source_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "findings": report["findings"],
                      "files_and_constants_scanned": report["files_and_constants_scanned"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
