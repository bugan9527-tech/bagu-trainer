"""Build a separate frozen acceptance driver, then optionally exercise the EXEs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
from build_portable import prepare_notices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--memory-credentials", action="store_true")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "frozen-acceptance")
    options = parser.parse_args()
    prepare_notices()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip())
    provenance = ROOT / "build" / "acceptance-provenance.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps({"source_commit": commit, "source_dirty": dirty}), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "acceptance-pyinstaller"),
                    str(ROOT / "packaging" / "acceptance.spec")], cwd=ROOT, check=True)
    directory = ROOT / "dist" / "BaguTrainerAcceptance"
    shutil.copyfile(ROOT / "CLEAN_WINDOWS_ACCEPTANCE.md", directory / "验收说明.md")
    archive = shutil.make_archive(str(ROOT / "dist" / "BaguTrainerAcceptance-windows-x64"), "zip", directory.parent, directory.name)
    Path(archive + ".sha256").write_text(hashlib.sha256(Path(archive).read_bytes()).hexdigest() + "  " + Path(archive).name + "\n", encoding="ascii")
    print(archive)
    if options.verify:
        work = options.work.resolve()
        if work.exists():
            raise ValueError("Acceptance work directory already exists; choose a new directory")
        args = [str(directory / "BaguTrainerAcceptance.exe"), "--work", str(work),
                "--production-exe", str(ROOT / "dist" / "BaguTrainer" / "BaguTrainer.exe")]
        if options.memory_credentials:
            args.append("--memory-credentials")
        result = subprocess.run(args, timeout=160)
        if result.returncode:
            error = work / "ERROR.json"
            raise RuntimeError(error.read_text(encoding="utf-8") if error.exists() else "Frozen acceptance failed")
        report = json.loads((work / "ACCEPTANCE.json").read_text(encoding="utf-8"))
        if not report["passed"] or not report["frozen"]:
            raise RuntimeError("Acceptance did not run the frozen driver successfully")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
