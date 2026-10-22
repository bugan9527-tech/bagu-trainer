"""Locate the Tcl/Tk scripts bundled with the selected Python installation."""
import os
import sys
from pathlib import Path


def create_root():
    """Create Tk, recovering once when an existing script is reported missing.

    Only retry a missing-file error for a script that actually exists inside
    the selected Tk library. Real missing resources and other errors propagate.
    """
    prepare_tk()
    import tkinter as tk
    try:
        root = tk.Tk()
        _load_focus_commands(root)
        return root
    except tk.TclError as error:
        import re
        message = str(error)
        library = Path(os.environ.get("TK_LIBRARY", "")).resolve()
        missing = re.findall(r'couldn\'t read file "([^"\n]+)": no such file or directory', message)
        recoverable = False
        for name in missing:
            path = Path(name).resolve()
            if path.is_relative_to(library) and path.is_file():
                recoverable = True
        if not recoverable:
            raise
        # Tcl caches filesystem normalization independently of Python chdir.
        # A Tcl-only interpreter can refresh it before loading Tk scripts.
        import _tkinter
        bridge = _tkinter.create(None, "bagu-path-reset", "Tk", False, True, False, False, None)
        bridge.call("cd", str(Path.cwd()))
        del bridge  # no GUI resources; release on the calling UI thread
        root = tk.Tk()  # exactly one retry; a second failure is not suppressed
        root._bagu_tk_recovered = True
        _load_focus_commands(root)
        return root


def _load_focus_commands(root):
    # Load keyboard traversal before workers/hosts can change the process cwd.
    # Tcl's lazy autoload path cache can otherwise fail after multiple roots.
    if not root.tk.call("info", "commands", "tk_focusNext"):
        root.tk.call("source", str(Path(os.environ["TK_LIBRARY"]) / "focus.tcl"))


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
                previous_relative = os.path.relpath(location, Path(sys.base_prefix)).replace("\\", "/")
                if name not in os.environ or os.environ[name].replace("\\", "/") == previous_relative:
                    # A Tcl interpreter may retain its own cwd cache across
                    # Python chdir calls. Never resolve our bundled scripts
                    # through that cache when creating another root window.
                    os.environ[name] = location.resolve().as_posix()
                break
