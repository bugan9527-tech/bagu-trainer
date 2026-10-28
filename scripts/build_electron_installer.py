"""Use the existing audited Inno lifecycle for the Electron payload."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    build_python = Path(os.environ.get('BAGU_BUILD_PYTHON', ROOT / '.venv-build' / 'Scripts' / 'python.exe'))
    python = str(build_python) if build_python.is_file() else sys.executable
    command = [python, '-B', str(ROOT / 'scripts' / 'build_installer.py'),
        '--bundle', str(ROOT / 'output' / 'electron' / 'win-unpacked'),
        '--output-dir', str(ROOT / 'output' / 'electron' / 'installer')]
    unpacker = ROOT / 'build' / 'installer-tools' / 'unpacker' / 'innounp.exe'
    compiler = ROOT / 'build' / 'installer-tools' / 'inno' / '{app}' / 'ISCC.exe'
    if compiler.is_file():
        command.extend(['--iscc', str(compiler)])
    if unpacker.is_file():
        command.extend(['--innounp', str(unpacker)])
    return subprocess.call([*command, *sys.argv[1:]], cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
