"""Absolute-path entry point usable by pythonw.exe and Windows Startup."""
import sys
import os
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    from bagu_trainer.runtime import run
    raise SystemExit(run())
