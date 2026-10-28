from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata
from PyInstaller.utils.hooks.tcl_tk import tcltk_info

if not tcltk_info.available:
    raise RuntimeError('完整卸载工具需要 Tcl/Tk；拒绝构建缺少运行资源的程序包。')

root = Path(SPECPATH).parent
resources = root / 'build' / 'electron-resources'
datas = [(str(root / 'interview-banks' / 'sources.json'), 'interview-banks'),
         (str(root / 'interview-banks' / 'README.md'), 'interview-banks'),
         (str(root / 'LICENSE'), '.'),
         (str(resources / 'licenses'), 'licenses'),
         (str(resources / 'THIRD_PARTY_NOTICES.txt'), '.')]
datas += copy_metadata('keyring', recursive=True)
datas += collect_data_files('ttkbootstrap') + copy_metadata('ttkbootstrap', recursive=True)
hidden = collect_submodules('bagu_trainer') + ['keyring.backends.Windows', 'keyring.backends.null',
    'keyring.backends.chainer', 'keyring.backends.fail']
a = Analysis([str(root / 'scripts' / 'start_electron_backend.py')], pathex=[str(root)],
    binaries=[], datas=datas, hiddenimports=hidden,
    excludes=['pytest', 'unittest', 'tkinter.test', 'scripts'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BaguBackend', console=True,
    debug=False, strip=False, upx=False, contents_directory='_internal',
    icon=str(root / 'packaging' / 'BaguTrainer.ico'))
cleanup = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BaguCleanup', console=False,
    debug=False, strip=False, upx=False, contents_directory='_internal',
    icon=str(root / 'packaging' / 'BaguTrainer.ico'))
coll = COLLECT(exe, cleanup, a.binaries, a.datas, strip=False, upx=False, name='BaguBackend')
