"""Background child processes must not allocate a flashing Windows console."""
import os
import subprocess
import sys

import pytest

from bagu_trainer.diff_reader import DiffReader
from bagu_trainer import server


@pytest.mark.skipif(os.name != "nt", reason="Windows console allocation")
def test_git_launcher_child_has_no_console(tmp_path, monkeypatch):
    actual_run = subprocess.run

    def inspect_console(command, **options):
        assert command[0] == "git"
        # Keep all production launch options; substitute a console executable
        # that can report whether Windows attached/allocated a console.
        return actual_run([
            sys.executable, "-B", "-c",
            "import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow()))",
        ], **options)

    monkeypatch.setattr(subprocess, "run", inspect_console)
    result = DiffReader(str(tmp_path))._run_git(["--version"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_real_git_still_returns_output_without_console(tmp_path):
    try:
        result = DiffReader(str(tmp_path))._run_git(["--version"])
    except FileNotFoundError:
        pytest.skip("Git is not installed")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("git version ")


def test_config_gui_launch_does_not_create_extra_console(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert server._open_config_window()
    _, options = calls[0]
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["stdin"] == subprocess.DEVNULL
