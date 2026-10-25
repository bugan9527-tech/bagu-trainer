"""One explicit target for builds and acceptance; never search for a newest ZIP."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PortableTarget:
    output_dir: Path
    bundle: Path
    archive: Path

    @property
    def driver(self):
        return self.output_dir / "BaguTrainerAcceptance" / "BaguTrainerAcceptance.exe"

    def describe(self):
        result = {"output_dir": str(self.output_dir), "bundle": str(self.bundle),
                  "archive": str(self.archive), "expected_version": __version__}
        if self.archive.is_file() and not self.archive.is_symlink():
            result["archive_sha256"] = sha256(self.archive)
        manifest = self.bundle / "BUILD_MANIFEST.json"
        if manifest.is_file() and not manifest.is_symlink():
            result["manifest_sha256"] = sha256(manifest)
            try:
                record = json.loads(manifest.read_text(encoding="utf-8"))
                result.update(version=record.get("version"), source_tree_sha256=record.get("source_tree", {}).get("sha256"))
            except (ValueError, AttributeError):
                pass
        return result


def resolve_target(output_dir=None, *, bundle=None, archive=None):
    """CLI paths > explicit output dir > BAGU_PORTABLE_* > legacy dist.

    A bundle-only or archive-only override derives its companion from the same
    parent. An explicit output dir ignores ambient per-file environment overrides.
    """
    if output_dir is None:
        bundle = bundle or os.environ.get("BAGU_PORTABLE_BUNDLE")
        archive = archive or os.environ.get("BAGU_PORTABLE_ARCHIVE")
        output_dir = (os.environ.get("BAGU_PORTABLE_OUTPUT_DIR") or
                      (Path(bundle).parent if bundle else Path(archive).parent if archive else ROOT / "dist"))
    output = Path(output_dir).resolve()
    return PortableTarget(output, Path(bundle).resolve() if bundle else output / "BaguTrainer",
                          Path(archive).resolve() if archive else output / f"BaguTrainer-{__version__}-windows-x64.zip")


def require_new(*paths):
    for path in paths:
        if path.exists() or path.is_symlink():
            raise ValueError(f"Refusing to overwrite existing artifact/evidence: {path}; choose a new --output-dir/report")


def inventory(directory):
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & 0x400):
            raise ValueError("Artifact contains a link: " + str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    return result


def verify_archive(directory, archive, *, prefix="BaguTrainer"):
    checksum = Path(str(archive) + ".sha256")
    for path in (archive, checksum):
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing/linked attachment: " + str(path))
    digest = sha256(archive)
    if checksum.read_text(encoding="ascii").strip() != digest + "  " + archive.name:
        raise ValueError("SHA-256 attachment mismatch: " + str(checksum))
    expected = {prefix + "/" + name: item for name, item in inventory(directory).items()}
    with zipfile.ZipFile(archive) as zipped:
        names = [item.filename for item in zipped.infolist() if not item.is_dir()]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError("ZIP inventory differs from target directory: " + str(archive))
        for name, item in expected.items():
            with zipped.open(name) as stream:
                actual = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
                    size += len(chunk)
            if size != item["bytes"] or actual.hexdigest() != item["sha256"]:
                raise ValueError("ZIP payload differs from target directory: " + name)
    return {"path": str(archive), "bytes": archive.stat().st_size, "sha256": digest,
            "checksum_path": str(checksum), "checksum_sha256": sha256(checksum)}


def validate_target(target, *, expected_source=None, driver=None):
    """Read-only gate, run before any EXE or test child is started.

    Frozen drivers supply embedded provenance; source callers compare the existing
    complete source_provenance record (including uncommitted runtime/build files).
    """
    manifest_path = target.bundle / "BUILD_MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Missing BUILD_MANIFEST: " + str(manifest_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("BUILD_MANIFEST must be an object: " + str(manifest_path))
    if manifest.get("version") != __version__:
        raise ValueError(f"Target version {manifest.get('version')} != source {__version__}: {target.bundle}; select --output-dir/BAGU_PORTABLE_* or rebuild")
    from scripts.build_installer import validate_bundle
    from scripts.build_portable import source_provenance
    validate_bundle(target.bundle, current_source=False, check_exe=False)
    expected_source = expected_source if expected_source is not None else {"source_tree": source_provenance()}
    tree = manifest.get("source_tree")
    if not isinstance(tree, dict) or not tree.get("files") or tree != expected_source.get("source_tree"):
        raise ValueError("Source fingerprint mismatch; rebuild into a NEW --output-dir: " + str(target.bundle))
    if any(manifest.get(key) != expected_source[key] for key in ("source_commit", "source_dirty") if key in expected_source):
        raise ValueError("Production and acceptance provenance differ")
    result = target.describe()
    result.update(exe=str(target.bundle / "BaguTrainer.exe"), exe_sha256=sha256(target.bundle / "BaguTrainer.exe"),
                  source_commit=manifest.get("source_commit"), source_dirty=manifest.get("source_dirty"),
                  portable=verify_archive(target.bundle, target.archive))
    if driver is not None:
        driver = Path(driver).resolve()
        if driver.name != "BaguTrainerAcceptance.exe" or not driver.is_file():
            raise ValueError("Missing acceptance driver: " + str(driver))
        record = json.loads((driver.parent / "_internal" / "acceptance-provenance.json").read_text(encoding="utf-8"))
        if record.get("version") != __version__ or any(record.get(key) != manifest.get(key)
                for key in ("source_tree", "source_commit", "source_dirty")):
            raise ValueError("Acceptance driver version/source fingerprint differs from production")
        result["driver"] = {"path": str(driver), "sha256": sha256(driver),
                            "archive": verify_archive(driver.parent, driver.parent.parent / "BaguTrainerAcceptance-windows-x64.zip",
                                                      prefix="BaguTrainerAcceptance")}
    result["passed"] = True
    return result


def verify_installed_payload(executable, bundle):
    """Bind an installed EXE to an already validated clean production bundle.

    Installer metadata and unrelated user files are outside the production
    inventory. Read only original payload members; preserve every extra file.
    """
    from bagu_trainer.cleanup_manifest import safe_path as safe_file
    from scripts.build_installer import safe_path as safe_relative
    executable = safe_file(Path(executable).absolute())
    if executable.name != "BaguTrainer.exe":
        raise ValueError("Installed payload must name BaguTrainer.exe")
    manifest_path = Path(bundle) / "BUILD_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = [*manifest["files"], {"path": "BUILD_MANIFEST.json", "sha256": sha256(manifest_path)}]
    for item in expected:
        safe_relative(item["path"])
        path = safe_file(executable.parent / item["path"])
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise ValueError("Installed payload differs from verified production: " + item["path"])
    return {"exe": str(executable), "manifest_sha256": sha256(manifest_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--with-driver", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    require_new(args.report)
    target = resolve_target(args.output_dir, bundle=args.bundle, archive=args.archive)
    report = target.describe()
    try:
        report = validate_target(target, driver=target.driver if args.with_driver else None)
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        report.update(passed=False, error=str(error))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
