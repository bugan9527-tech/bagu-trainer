# -*- coding: utf-8 -*-
"""直接执行 MCP 入口时的启动回归。"""
import subprocess
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
SERVER = PROJECT / "bagu_trainer" / "server.py"


def test_direct_server_script_imports_without_shadowing_stdlib_secrets():
    result = subprocess.run(
        [sys.executable, str(SERVER), "--check-imports"],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "server imports ok" in result.stdout
    assert "cannot import name 'token_hex'" not in result.stderr
