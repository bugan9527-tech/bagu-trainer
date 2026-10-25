"""Build a separate frozen acceptance driver, then optionally exercise the EXEs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_portable import prepare_notices, source_provenance
from scripts.portable_artifacts import require_new, resolve_target, validate_target
from bagu_trainer.version import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--memory-credentials", action="store_true")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "frozen-acceptance")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--production-exe", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    options = parser.parse_args()
    target = resolve_target(options.output_dir, bundle=options.production_exe.parent if options.production_exe else None)
    output = target.output_dir
    if options.production_exe and options.production_exe.name != "BaguTrainer.exe":
        raise ValueError("--production-exe must name BaguTrainer.exe")
    require_new(output / "BaguTrainerAcceptance", output / "BaguTrainerAcceptance-windows-x64.zip",
                output / "BaguTrainerAcceptance-windows-x64.zip.sha256")
    if options.verify:
        require_new(options.work.resolve())
        validate_target(target)
    if options.require_clean and subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip():
        raise ValueError("Acceptance build requires a clean source checkout")
    (ROOT / "build").mkdir(exist_ok=True)
    build_work = Path(tempfile.mkdtemp(prefix="acceptance-", dir=ROOT / "build"))
    resources = build_work / "portable-resources"
    prepare_notices(resources)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True).strip())
    provenance = build_work / "acceptance-provenance.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps({"version": __version__, "source_commit": commit, "source_dirty": dirty,
                                      "source_tree": source_provenance()}), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output), "--workpath", str(build_work / "pyinstaller"),
                    str(ROOT / "packaging" / "acceptance.spec")], cwd=ROOT, check=True,
                   env=dict(os.environ, BAGU_BUILD_RESOURCES=str(resources), BAGU_ACCEPTANCE_PROVENANCE=str(provenance)))
    directory = output / "BaguTrainerAcceptance"
    shutil.copyfile(ROOT / "CLEAN_WINDOWS_ACCEPTANCE.md", directory / "验收说明.md")
    archive = shutil.make_archive(str(output / "BaguTrainerAcceptance-windows-x64"), "zip", directory.parent, directory.name)
    Path(archive + ".sha256").write_text(hashlib.sha256(Path(archive).read_bytes()).hexdigest() + "  " + Path(archive).name + "\n", encoding="ascii")
    print(archive)
    if options.verify:
        work = options.work.resolve()
        if work.exists():
            raise ValueError("Acceptance work directory already exists; choose a new directory")
        args = [str(directory / "BaguTrainerAcceptance.exe"), "--work", str(work),
                "--production-exe", str(target.bundle / "BaguTrainer.exe"), "--output-dir", str(output),
                "--archive", str(target.archive)]
        if options.memory_credentials:
            args.append("--memory-credentials")
        result = subprocess.run(args, timeout=240)
        if result.returncode:
            error = work / "ERROR.json"
            raise RuntimeError(error.read_text(encoding="utf-8") if error.exists() else "Frozen acceptance failed")
        report = json.loads((work / "ACCEPTANCE.json").read_text(encoding="utf-8"))
        if not report["passed"] or not report["frozen"]:
            raise RuntimeError("Acceptance did not run the frozen driver successfully")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
