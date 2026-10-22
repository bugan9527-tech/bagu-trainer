"""Bounded startup recovery never hides truly missing runtime resources."""
import os
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk runtime")


@pytest.mark.parametrize("failures", [1, 2])
def test_existing_script_initialization_failure_recovers_only_once(monkeypatch, failures):
    from bagu_trainer.tk_runtime import create_root, prepare_tk
    prepare_tk()
    import tkinter as tk
    script = Path(os.environ["TK_LIBRARY"]) / "tk.tcl"
    assert script.is_file()
    actual, calls = tk.Tk, []
    def factory():
        calls.append(1)
        if len(calls) <= failures:
            raise tk.TclError(f'couldn\'t read file "{script.as_posix()}": no such file or directory')
        return actual()
    monkeypatch.setattr(tk, "Tk", factory)
    if failures == 2:
        with pytest.raises(tk.TclError):
            create_root()
    else:
        root = create_root()
        try:
            root.update_idletasks()
            assert root.winfo_exists() and root._bagu_tk_recovered
        finally:
            root.destroy()
    assert len(calls) == 2


def test_missing_script_is_not_retried_or_hidden(tmp_path, monkeypatch):
    from bagu_trainer.tk_runtime import create_root
    import tkinter as tk
    library = tmp_path / "missing-tk"
    monkeypatch.setenv("TK_LIBRARY", str(library))
    calls = []
    def factory():
        calls.append(1)
        raise tk.TclError(f'couldn\'t read file "{(library / "tk.tcl").as_posix()}": no such file or directory')
    monkeypatch.setattr(tk, "Tk", factory)
    with pytest.raises(tk.TclError):
        create_root()
    assert len(calls) == 1
