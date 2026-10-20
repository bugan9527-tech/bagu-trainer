# A separate diagnostic artifact, never collected into the production ZIP.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata
root = Path(SPECPATH).parent
datas = copy_metadata("keyring", recursive=True) + [(str(root / "build" / "acceptance-provenance.json"), ".")]
datas += [(str(root / "LICENSE"), "."),
          (str(root / "build" / "portable-resources" / "licenses"), "licenses"),
          (str(root / "build" / "portable-resources" / "THIRD_PARTY_NOTICES.txt"), ".")]
hidden = collect_submodules("bagu_trainer") + ["keyring.backends.Windows", "keyring.backends.null",
                                              "keyring.backends.chainer", "keyring.backends.fail"]
a = Analysis([str(root / "scripts" / "acceptance" / "desktop_acceptance.py")], pathex=[str(root)],
             datas=datas, binaries=[], hiddenimports=hidden,
             excludes=["pytest", "unittest", "tkinter.test"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BaguTrainerAcceptance", console=False,
          debug=False, strip=False, upx=False, contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BaguTrainerAcceptance")
