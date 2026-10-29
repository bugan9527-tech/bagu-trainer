"""Background child processes must not allocate a flashing Windows console."""
import os
import subprocess
import sys

import pytest

from bagu_trainer.infrastructure.diff_reader import DiffReader
from bagu_trainer.application.learning import engine


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


def test_git_output_decodes_utf8_and_replaces_legacy_bytes(tmp_path, monkeypatch):
    actual_run = subprocess.run

    def emit_bytes(command, **options):
        return actual_run([
            sys.executable, "-B", "-c",
            "import sys; sys.stdout.buffer.write(bytes.fromhex('e4b8ade69687202d20ff')); "
            "sys.stderr.buffer.write(bytes.fromhex('e99499e8afaf20ff'))",
        ], **options)

    monkeypatch.setattr(subprocess, "run", emit_bytes)
    result = DiffReader(str(tmp_path))._run_git(["diff"])
    assert result.returncode == 0
    assert result.stdout == "中文 - \ufffd"
    assert result.stderr == "错误 \ufffd"


def test_real_git_changes_keep_chinese_paths_and_contents(tmp_path):
    reader = DiffReader(str(tmp_path))
    try:
        result = reader._run_git(["init"])
    except FileNotFoundError:
        pytest.skip("Git is not installed")
    assert result.returncode == 0, result.stderr
    source = tmp_path / "缓存.py"
    source.write_text("# 原有缓存逻辑\nvalue = 1\n", encoding="utf-8")
    assert reader._run_git(["add", "--", source.name]).returncode == 0
    source.write_text("# 中文缓存逻辑已更新\nvalue = 2\n", encoding="utf-8")
    result = reader.changes()
    assert result["source"] == "git"
    assert result["files"] == [source.name]
    assert "+# 中文缓存逻辑已更新" in result["diff_text"]
    assert "-# 原有缓存逻辑" in result["diff_text"]


def test_config_gui_launch_does_not_create_extra_console(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert engine._open_config_window()
    _, options = calls[0]
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["stdin"] == subprocess.DEVNULL
