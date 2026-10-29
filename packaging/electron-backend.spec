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

# Electron 运行时只加载引擎、后端服务、题库构建和完整卸载工具。旧 Tk 桌面/
# 首次向导/项目面板等只用于 0.4.x 兼容入口，不进入 Electron 发行包。
# 卸载工具仍使用 Tk 确认窗口，因此 ui_theme/ui_layout/tk_runtime、Tcl/Tk、
# ttkbootstrap/Pillow 资源和图片必须保留，不能按“主界面不用”删除。
TK_UI_ONLY = {
    'bagu_trainer.desktop.bank_panel',
    'bagu_trainer.desktop.bank_setup',
    'bagu_trainer.desktop.tk_app',
    'bagu_trainer.desktop.history_page',
    'bagu_trainer.infrastructure.migration',
    'bagu_trainer.desktop.model_panel',
    'bagu_trainer.desktop.practice_controller',
    'bagu_trainer.desktop.practice_page',
    'bagu_trainer.desktop.practice_window',
    'bagu_trainer.desktop.projects_panel',
    'bagu_trainer.desktop.settings_page',
    'bagu_trainer.desktop.task_controller',
    'bagu_trainer.desktop.ui_copy',
    'bagu_trainer.desktop.ui_icons',
    'bagu_trainer.desktop.ui_shell',
    'bagu_trainer.desktop.ui_state',
    'bagu_trainer.desktop.ui_window',
}
hidden = [module for module in collect_submodules('bagu_trainer') if module not in TK_UI_ONLY]
hidden += ['keyring.backends.Windows', 'keyring.backends.null',
    'keyring.backends.chainer', 'keyring.backends.fail']
a = Analysis([str(root / 'scripts' / 'start_electron_backend.py')], pathex=[str(root)],
    binaries=[], datas=datas, hiddenimports=hidden,
    excludes=['pytest', 'unittest', 'tkinter.test', 'scripts', *sorted(TK_UI_ONLY)], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BaguBackend', console=True,
    debug=False, strip=False, upx=False, contents_directory='_internal',
    icon=str(root / 'packaging' / 'BaguTrainer.ico'))
cleanup = EXE(pyz, a.scripts, [], exclude_binaries=True, name='BaguCleanup', console=False,
    debug=False, strip=False, upx=False, contents_directory='_internal',
    icon=str(root / 'packaging' / 'BaguTrainer.ico'))
coll = COLLECT(exe, cleanup, a.binaries, a.datas, strip=False, upx=False, name='BaguBackend')
