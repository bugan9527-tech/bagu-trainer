"""Build the Python engine with an explicit source/resource allowlist."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    build_python = Path(os.environ.get('BAGU_BUILD_PYTHON', ROOT / '.venv-build' / 'Scripts' / 'python.exe'))
    if build_python.resolve() != Path(sys.executable).resolve() and build_python.is_file():
        return subprocess.call([str(build_python), '-B', str(Path(__file__).resolve())], cwd=ROOT)
    sys.path.insert(0, str(ROOT))
    from scripts.build_portable import prepare_notices
    prepare_notices(ROOT / 'build' / 'electron-resources')
    return subprocess.call([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', str(ROOT / 'build' / 'electron-backend'),
        '--workpath', str(ROOT / 'build' / 'electron-pyinstaller'),
        str(ROOT / 'packaging' / 'electron-backend.spec')], cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
