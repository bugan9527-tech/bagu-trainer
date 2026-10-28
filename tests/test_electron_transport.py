"""Run the real pipe transport with isolated, synthetic persisted quizzes."""
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.mark.parametrize("explicit_shutdown", [False, True])
def test_pipe_drains_accepted_answers_on_eof_or_shutdown(tmp_path, explicit_shutdown):
    root = Path(__file__).resolve().parents[1]
    profile = tmp_path / "profile"
    subprocess.run([sys.executable, "-B", str(root / "scripts/electron_fixture.py"), str(profile)],
                   cwd=root, check=True, capture_output=True, timeout=15)
    environment = {**os.environ, "BAGU_USER_DATA_DIR": str(profile),
        "BAGU_TEST_INSTANCE": uuid.uuid4().hex, "BAGU_LLM_API_KEY": "synthetic-transport-key",
        "PYTHONIOENCODING": "utf-8"}
    commands = [
        {"id": 1, "method": "snapshot"},
        {"id": 2, "method": "practice.enter", "params": {"quiz_id": "electron-fixture-1"}},
        {"id": 3, "method": "practice.submit", "params": {"quiz_id": "electron-fixture-1", "answer": "A"}},
        {"id": 4, "method": "execute", "params": {"code": "private-payload"}},
    ]
    if explicit_shutdown:
        commands.append({"id": 5, "method": "shutdown"})
    result = subprocess.run([sys.executable, "-u", "-B", str(root / "scripts/start_electron_backend.py")],
        input="".join(json.dumps(command) + "\n" for command in commands), cwd=root,
        env=environment, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    replies = {message["id"]: message for message in messages if isinstance(message.get("id"), int)}
    assert len(replies) == len(commands)
    assert replies[1]["result"]["pending"][0]["status"] == "ready"
    assert "answer" not in replies[2]["result"]["quiz"]
    assert replies[3]["result"]["correct"]
    assert "error" in replies[4]
    assert "synthetic-transport-key" not in result.stdout
    assert "private-payload" not in result.stdout
    with sqlite3.connect(profile / "data/trainer.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
