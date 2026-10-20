# PyInstaller onedir: resource allowlist only; no project-local user data.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
resources = root / "build" / "portable-resources"
datas = [(str(root / name), str(Path(name).parent)) for name in (
    "LICENSE", "FIRST_USE.md", "QUESTION_BANK.md", "BANK_BUILD.md", "PORTABLE_README.md",
    "WINDOWS_PORTABLE.md", "DESKTOP_MODE.md", "KEY_MIGRATION.md", "STARTUP.md",
    "VALIDATION.md", "CLEAN_WINDOWS_ACCEPTANCE.md",
    "interview-banks/sources.json", "interview-banks/README.md")]
datas += [(str(resources / "licenses"), "licenses"),
          (str(resources / "THIRD_PARTY_NOTICES.txt"), ".")]
datas += copy_metadata("keyring", recursive=True)
hidden = collect_submodules("bagu_trainer") + ["keyring.backends.Windows", "keyring.backends.null",
                                              "keyring.backends.chainer", "keyring.backends.fail"]
a = Analysis([str(root / "scripts" / "start_desktop.py")], pathex=[str(root)],
             binaries=[], datas=datas, hiddenimports=hidden,
             excludes=["pytest", "unittest", "tkinter.test", "scripts"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BaguTrainer", console=False,
          debug=False, strip=False, upx=False, contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BaguTrainer")
