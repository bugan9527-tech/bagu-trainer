"""Absolute-path entry point usable by pythonw.exe and Windows Startup."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagu_trainer.desktop import main

if __name__ == "__main__":
    # pythonw.exe has no console streams; libraries can still safely write logs.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    try:
        raise SystemExit(main())
    except Exception as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "桌面训练器未能启动（" + type(error).__name__ +
            "）。请在终端运行 start_desktop.py 查看错误。", "AI 八股训练器", 0x10)
        raise
