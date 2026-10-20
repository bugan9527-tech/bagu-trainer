"""Build/audit a minimal Windows x64 onedir ZIP from an isolated build venv."""
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__


def prepare_notices():
    resources = ROOT / "build" / "portable-resources"
    licenses = resources / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    notices = ["BaguTrainer runtime components and bundled licenses", "",
               "Python's license also contains notices for its bundled OpenSSL, Tcl/Tk,",
               "SQLite and Microsoft runtime components. See Python-LICENSE.txt.", ""]
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    tk_license = next((Path(sys.base_prefix) / "tcl").glob("tk*/license.terms"))
    shutil.copyfile(tk_license, licenses / "Tk-license.terms")
    # This is a dedicated venv with runtime + build tools only. Include licenses
    # for the bootloader/build tools too, rather than guessing what metadata is needed.
    versions = {}
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
    (resources / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")
    return versions


def audit(directory):
    forbidden = {"config.json", "questions.json", "question_concepts.json", "fallback_quizzes.json",
                 "active-bank.json", "onboarding.json", "bridge-credentials.json"}
    entries = []
    for file in sorted(directory.rglob("*")):
        relative = file.relative_to(directory)
        if file.is_file():
            if (file.name in forbidden or file.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log"}
                    or set(relative.parts) & {"repos", "deduped", "tests", "release_tests", ".git", "scripts"}):
                raise RuntimeError("发布包包含禁止收录的文件：" + str(relative))
            entries.append({"path": relative.as_posix(), "bytes": file.stat().st_size,
                            "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    return entries


def main():
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Windows x64 Python is required")
    versions = prepare_notices()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"],
                                        cwd=ROOT, text=True).strip())
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
                    str(ROOT / "packaging" / "windows.spec")], check=True, cwd=ROOT)
    bundle = ROOT / "dist" / "BaguTrainer"
    shutil.copyfile(ROOT / "PORTABLE_README.md", bundle / "使用说明.md")
    contents = audit(bundle)
    manifest = {"application": "BaguTrainer", "version": __version__, "architecture": "windows-x64",
                "layout": "PyInstaller onedir", "python": sys.version, "dependencies": versions, "files": contents,
                "source_commit": commit, "source_dirty": dirty}
    (bundle / "BUILD_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    name = f"BaguTrainer-{__version__}-windows-x64"
    archive = shutil.make_archive(str(ROOT / "dist" / name), "zip", bundle.parent, bundle.name)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n", encoding="ascii")
    print(archive)
    return archive


if __name__ == "__main__":
    main()
