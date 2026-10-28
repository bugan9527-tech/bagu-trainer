"""Build/audit a minimal Windows x64 onedir ZIP from an isolated build venv."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__
from scripts.portable_artifacts import require_new, resolve_target


def prepare_notices(resources=None):
    resources = resources or ROOT / "build" / "portable-resources"
    licenses = resources / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    notices = ["BaguTrainer runtime components and bundled licenses", "",
               "Desktop theme: ttkbootstrap; theme imaging: Pillow.",
               "Python's license also contains notices for its bundled OpenSSL, Tcl/Tk,",
               "SQLite and Microsoft runtime components. See Python-LICENSE.txt.", ""]
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    tk_license = next((Path(sys.base_prefix) / "tcl").glob("tk*/license.terms"))
    shutil.copyfile(tk_license, licenses / "Tk-license.terms")
    # This is a dedicated venv with runtime + build tools only. Include licenses
    # for the bootloader/build tools too, rather than guessing what metadata is needed.
    versions = {}
    ui_licenses = set()
    for distribution in sorted(metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        if name.lower() in {"pip", "setuptools"}:
            continue
        versions[name] = distribution.version
        notices.append(f"{name} {distribution.version}: {distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'see bundled license'}")
        for item in distribution.files or []:
            if any(token in item.name.lower() for token in ("license", "copying", "notice")):
                original = Path(distribution.locate_file(item))
                if original.is_file():
                    destination = licenses / name / item.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, destination)
                    ui_licenses.add(name.lower())
    if not {"ttkbootstrap", "pillow"} <= ui_licenses:
        raise RuntimeError("Desktop theme dependencies or their licenses are missing; install requirements-build.txt")
    (resources / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")
    return versions


def audit(directory):
    forbidden = {"config.json", "questions.json", "question_concepts.json", "fallback_quizzes.json",
                 "active-bank.json", "onboarding.json", "bridge-credentials.json", "window.json"}
    entries = []
    for file in sorted(directory.rglob("*")):
        relative = file.relative_to(directory)
        if file.is_symlink() or (file.lstat().st_file_attributes & 0x400 if os.name == "nt" else False):
            raise RuntimeError("发布包包含禁止收录的链接：" + str(relative))
        if file.is_file():
            low = file.name.lower()
            if (low in forbidden or low.startswith((".env", "config.", "acceptance", "bagutraineracceptance"))
                    or low.startswith(("key.", "api-key", "api_key", "secret.", "secrets.", "credentials.", "token."))
                    or low in {"installer_acceptance.json", "installer_lifecycle.json", "installed_acceptance.json",
                               "installer_manifest.json", "release_validation.json", "release-tests.xml", "owner.lock"}
                    or any(low.endswith(ext) or (ext + "-") in low or (ext + ".") in low
                           for ext in (".db", ".sqlite", ".sqlite3", ".log"))
                    or file.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".keystore", ".jks"}
                    or low.startswith(("id_rsa", "id_ed25519"))
                    or {part.lower() for part in relative.parts} & {"repos", "deduped", "tests", "release_tests", ".git", "scripts",
                                              "runtime", "logs", "profiles", "user-data", ".desktop-runtime", "validation", "acceptance"}):
                raise RuntimeError("发布包包含禁止收录的文件：" + str(relative))
            entries.append({"path": relative.as_posix(), "bytes": file.stat().st_size,
                            "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    return entries


def source_provenance():
    """Fingerprint runtime/build code, including current uncommitted modules."""
    paths = [path for folder in ("bagu_trainer", "scripts", "packaging", "electron", "frontend")
             for path in (ROOT / folder).rglob("*") if path.suffix in {
                 ".py", ".spec", ".iss", ".isl", ".ico", ".json", ".cjs", ".ts", ".tsx", ".css", ".html"}]
    paths.extend(ROOT.glob("requirements*.txt"))
    paths.extend(ROOT / name for name in ("package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")
                 if (ROOT / name).is_file())
    files = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(paths)}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    return {"sha256": digest, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require-clean", action="store_true", help="Fail before building if tracked or untracked source changed")
    options = parser.parse_args()
    target = resolve_target(options.output_dir)
    output = target.output_dir
    # PyInstaller --noconfirm may remove an existing onedir (possibly running).
    archive_path = output / f"BaguTrainer-{__version__}-windows-x64.zip"
    require_new(output / "BaguTrainer", archive_path, Path(str(archive_path) + ".sha256"))
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Windows x64 Python is required")
    if options.require_clean and subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip():
        raise ValueError("Release build requires a clean source checkout")
    (ROOT / "build").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="portable-", dir=ROOT / "build"))
    resources = work / "portable-resources"
    versions = prepare_notices(resources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"],
                                        cwd=ROOT, text=True).strip())
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
                    str(ROOT / "packaging" / "windows.spec")], check=True, cwd=ROOT,
                   env=dict(os.environ, BAGU_BUILD_RESOURCES=str(resources)))
    bundle = output / "BaguTrainer"
    shutil.copyfile(ROOT / "PORTABLE_README.md", bundle / "使用说明.md")
    contents = audit(bundle)
    manifest = {"application": "BaguTrainer", "version": __version__, "architecture": "windows-x64",
                "layout": "PyInstaller onedir", "python": sys.version, "dependencies": versions, "files": contents,
                "source_commit": commit, "source_dirty": dirty, "source_tree": source_provenance()}
    (bundle / "BUILD_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    name = f"BaguTrainer-{__version__}-windows-x64"
    archive = shutil.make_archive(str(output / name), "zip", bundle.parent, bundle.name)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n", encoding="ascii")
    print(archive)
    return archive


if __name__ == "__main__":
    main()
