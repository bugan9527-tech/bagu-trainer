"""The standalone clean-Windows mock must support both generation and review."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import pytest

from bagu_trainer.application.learning import generator as qg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell acceptance mock")
def test_builtin_powershell_mock_supports_generation_and_review(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/acceptance/mock-model.ps1"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with (tmp_path / "mock.log").open("wb") as log:
        process = subprocess.Popen([str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                    "-OutputDirectory", str(tmp_path)], stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            ready_file = tmp_path / "mock.json"
            deadline = time.monotonic() + 20
            while not ready_file.exists():
                assert process.poll() is None, (tmp_path / "mock.log").read_text(encoding="utf-8", errors="replace")
                assert time.monotonic() < deadline, "PowerShell fixture did not become ready"
                time.sleep(0.05)
            ready = json.loads(ready_file.read_text(encoding="utf-8-sig"))
            monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: {
                "base_url": ready["base_url"], "api_key": ready["fixture_key"], "model": ready["model"], "timeout_sec": 10,
            }))
            source = ("数据库索引通过额外的数据结构记录键值与行位置的对应关系，查询可以利用索引缩小扫描范围。"
                      "建立索引需要额外存储空间，插入和更新时也可能增加维护索引的成本，因此需要结合实际查询条件选择合适索引。")
            result = qg.QuizGenerator().generate({"id": "fixture", "knowledge_point": "索引", "difficulty": "easy",
                "question": "如何利用索引优化查询？", "source_excerpt": source},
                'query = "SELECT id FROM orders WHERE customer_id = 100"\n# 为订单查询添加合适索引')
            assert result.get("generated_by") == "llm", result.get("unavailable_reason")
            with urllib.request.urlopen(ready["base_url"].removesuffix("/v1") + "/status", timeout=3) as response:
                counts = json.load(response)
            assert counts["quizzes"] == counts["reviews"] == 1
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
