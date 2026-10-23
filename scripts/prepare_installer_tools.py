"""Download hash-pinned installer tools and unpack them without installing."""
import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_installer import TOOLCHAIN, check_compiler, check_unpacker, sha256


def download(record, destination):
    if not record["url"].startswith("https://") or "/latest" in record["url"]:
        raise ValueError("Tool downloads require a pinned HTTPS source")
    if not destination.exists() or sha256(destination) != record["sha256"]:
        request = urllib.request.Request(record["url"], headers={"User-Agent": "BaguTrainer-build"})
        with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    if sha256(destination) != record["sha256"]:
        raise ValueError("Tool download SHA-256 mismatch: " + destination.name)
    return destination


def prepare(work):
    work = work.resolve()
    if (ROOT / "build").resolve() not in work.parents:
        raise ValueError("Installer tools must be staged below this project's build/")
    work.mkdir(parents=True, exist_ok=True)
    pinned = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
    archive = download(pinned["inspection"], work / "innounp.zip")
    unpacker = work / "unpacker" / "innounp.exe"
    unpacker.parent.mkdir(exist_ok=True)
    # Extract exactly the executable; archive paths are never used as destinations.
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as source:
        candidates = [name for name in source.namelist() if name.replace("\\", "/").split("/")[-1].lower() == "innounp.exe"]
        if len(candidates) != 1:
            raise ValueError("Pinned innounp archive has no unique executable")
        unpacker.write_bytes(source.read(candidates[0]))
    check_unpacker(unpacker)
    setup = download(pinned["inno_setup"], work / ("innosetup-" + pinned["inno_setup"]["version"] + ".exe"))
    compiler = work / "inno" / "{app}" / "ISCC.exe"
    subprocess.run([str(unpacker), "-x", "-m", "-y", "-b", "-d" + str(work / "inno"), str(setup)],
                   check=True, stdout=subprocess.DEVNULL)
    check_compiler(compiler)
    print("Pinned Inno Setup " + pinned["inno_setup"]["version"] + " and innounp verified; no tool installed.")
    return compiler, unpacker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "installer-tools")
    args = parser.parse_args()
    try:
        prepare(args.work)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, "Installer tool preparation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
