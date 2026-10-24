"""The identical lifecycle scenario also runs in the frozen acceptance driver."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk clipboard lifecycle")
def test_continuous_lifecycle_across_process_restart(tmp_path):
    root = Path(__file__).resolve().parents[1]
    work = tmp_path / "lifecycle"
    env = dict(os.environ, BAGU_USER_DATA_DIR=str(work / "user"), PYTHONUTF8="1",
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    results = []
    for phase in ("prepare", "resume"):
        child = subprocess.run([sys.executable, "-B", str(root / "scripts/acceptance/desktop_acceptance.py"),
                                "--work", str(work), "--phase", "bank-" + phase],
                               env=env, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert child.returncode == 0, child.stdout + child.stderr
        results.append(json.loads((work / (phase + "-result.json")).read_text(encoding="utf-8")))
    assert all(result["passed"] for result in results)
    assert results[0]["pid"] != results[1]["pid"]
    assert len(results[0]["milestones"]) == 5
    assert len(results[1]["milestones"]) == 5
