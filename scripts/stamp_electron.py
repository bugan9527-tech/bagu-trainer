"""Apply local Windows artwork/version metadata and report source provenance."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))


def main():
    from windows_assets import ICON, version_resource
    from PyInstaller.utils.win32.icon import CopyIcons
    from PyInstaller.compat import win32api
    import pefile
    from scripts.build_portable import source_provenance
    from PyInstaller.config import CONF
    executable = Path(sys.argv[1]).resolve(strict=True)
    executable.relative_to((ROOT / 'output' / 'electron').resolve())
    if executable.name != 'BaguTrainer.exe':
        raise ValueError('只允许修改本项目构建的桌面程序资源。')
    CONF['workpath'] = str(ROOT / 'build' / 'electron-resources')
    CopyIcons(str(executable), str(ICON))
    # Chromium ships an English version resource. Replace every language so
    # Explorer and the installer cannot pick the original Electron metadata.
    with pefile.PE(str(executable), fast_load=True) as pe:
        pe.parse_data_directories(directories=[2])
        versions = [(name.id, language.id)
                    for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries if entry.id == 16
                    for name in entry.directory.entries for language in name.directory.entries]
    handle = win32api.BeginUpdateResource(str(executable), False)
    try:
        for name, language in versions or [(1, 0)]:
            win32api.UpdateResource(handle, 16, name, version_resource().toRaw(), language)
    finally:
        win32api.EndUpdateResource(handle, False)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, encoding='utf-8').strip()
    print(json.dumps({'source_tree': source_provenance(), 'source_commit': git('rev-parse', 'HEAD'),
                      'source_dirty': bool(git('status', '--porcelain'))}))


if __name__ == '__main__':
    main()
