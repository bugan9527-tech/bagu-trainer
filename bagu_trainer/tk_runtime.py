"""Locate the Tcl/Tk scripts bundled with the selected Python installation."""
import os
import sys
from pathlib import Path


def prepare_tk():
    if getattr(sys, "frozen", False):
        # PyInstaller's _tkinter hook collects these directories and DLLs.
        root = Path(sys._MEIPASS)
        for name, directory in (("TCL_LIBRARY", "_tcl_data"), ("TK_LIBRARY", "_tk_data")):
            path = root / directory
            if not path.is_dir():
                raise RuntimeError("打包的 Tcl/Tk 资源不完整")
            os.environ[name] = str(path)
        os.chdir(root)
        return
    # This helper is used only by dedicated GUI processes before their workers
    # start. Project/config/database paths are absolute. A stable interpreter cwd
    # avoids Tcl's path-normalization failures with redirected profile folders.
    os.chdir(sys.base_prefix)
    # Set these before importing _tkinter: Tcl snapshots its environment when its
    # DLL is first loaded. Supported Python installs bundle matching Tcl/Tk dirs.
    library = Path(sys.base_prefix) / "tcl"
    for name, prefix, script in (("TCL_LIBRARY", "tcl", "init.tcl"),
                                 ("TK_LIBRARY", "tk", "tk.tcl")):
        candidates = sorted(library.glob(prefix + "[89].*"), reverse=True)
        for location in candidates:
            if (location / script).is_file():
                if name not in os.environ:
                    os.environ[name] = os.path.relpath(location, Path.cwd()).replace("\\", "/")
                break
