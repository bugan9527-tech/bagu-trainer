"""One real engine/GUI/HTTP/watcher/SQLite loop across two fresh processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk acceptance")
def test_first_use_to_scoring_and_restart_with_local_mock(tmp_path):
    root = Path(__file__).resolve().parents[1]
    work = tmp_path / "完整 验收"
    result = subprocess.run([sys.executable, "-B", str(root / "scripts" / "acceptance" / "desktop_acceptance.py"),
                             "--work", str(work), "--memory-credentials"],
                            cwd=root, env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
                            capture_output=True, text=True, encoding="utf-8", timeout=110)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((work / "ACCEPTANCE.json").read_text(encoding="utf-8"))
    assert report["passed"] and report["path_empty_in_app"]
    assert report["requests"] == {"downloads": 4, "probes": 1, "quizzes": 1, "rejected": 1}
    assert len(report["first"]["milestones"]) == 8
    assert report["restart"]["milestones"] == ["restart_direct_main_and_history_restore"]
