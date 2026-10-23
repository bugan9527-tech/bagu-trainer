# PyInstaller onedir: resource allowlist only; no project-local user data.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata
from PyInstaller.utils.hooks.tcl_tk import tcltk_info

if not tcltk_info.available:
    raise RuntimeError("Tcl/Tk 探测失败，拒绝生成缺失 GUI 资源的生产包；请检查 Python 安装或构建进程权限")

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "packaging"))
from windows_assets import ICON, version_resource
resources = root / "build" / "portable-resources"
datas = [(str(root / name), str(Path(name).parent)) for name in (
    "LICENSE", "FIRST_USE.md", "QUESTION_BANK.md", "BANK_BUILD.md", "PORTABLE_README.md",
    "WINDOWS_PORTABLE.md", "WINDOWS_INSTALLER.md", "DESKTOP_MODE.md", "KEY_MIGRATION.md", "STARTUP.md",
    "VALIDATION.md", "CLEAN_WINDOWS_ACCEPTANCE.md", "UI_DESIGN.md", "UI_LAYOUT.md", "UI_STATES.md", "UI_DISPLAY.md", "THIRD_PARTY_NOTICES.md",
    "interview-banks/sources.json", "interview-banks/README.md")]
datas += [(str(resources / "licenses"), "licenses"),
          (str(resources / "THIRD_PARTY_NOTICES.txt"), ".")]
datas += copy_metadata("keyring", recursive=True)
datas += collect_data_files("ttkbootstrap") + copy_metadata("ttkbootstrap", recursive=True)
hidden = collect_submodules("bagu_trainer") + ["keyring.backends.Windows", "keyring.backends.null",
                                              "keyring.backends.chainer", "keyring.backends.fail"]
a = Analysis([str(root / "scripts" / "start_desktop.py")], pathex=[str(root)],
             binaries=[], datas=datas, hiddenimports=hidden,
             excludes=["pytest", "unittest", "tkinter.test", "scripts"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BaguTrainer", console=False,
          debug=False, strip=False, upx=False, contents_directory="_internal",
          icon=str(ICON), version=version_resource())
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BaguTrainer")
