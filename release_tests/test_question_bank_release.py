"""Source-only release checks; no user configuration, bank, network or credentials."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase


def load_dedupe():
    path = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_questions.py"
    spec = importlib.util.spec_from_file_location("release_dedupe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_bank_explains_local_build(tmp_path):
    with pytest.raises(FileNotFoundError, match="QUESTION_BANK.md"):
        KnowledgeBase(tmp_path / "questions.json")


def test_selected_sources_do_not_mix_other_checkouts_or_fetch_issues(tmp_path, monkeypatch):
    module = load_dedupe()
    repos = tmp_path / "repos"
    for name, title in [("JavaGuide", "什么是数据库索引？"),
                        ("Daily-Interview-Question", "什么是浏览器事件循环？")]:
        directory = repos / name
        directory.mkdir(parents=True)
        (directory / "README.md").write_text("# " + title + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPOS_DIR", repos)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "output")

    def no_network(*args, **kwargs):
        pytest.fail("Local extraction must not download issues implicitly")

    monkeypatch.setattr(module, "fetch_daily_issues", no_network)
    module.main(["--repos", "JavaGuide"])
    exported = json.loads((module.OUT_DIR / "questions_export.json").read_text(encoding="utf-8"))
    assert exported, "The selected local source must be extracted"
    assert all(q["source_paths"] == ["JavaGuide/README.md"] for q in exported)
    assert not (module.OUT_DIR / "by_repo" / "Daily-Interview-Question.md").exists()


def test_missing_selected_source_stops_before_writing(tmp_path, monkeypatch):
    module = load_dedupe()
    monkeypatch.setattr(module, "REPOS_DIR", tmp_path / "missing")
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "output")
    with pytest.raises(SystemExit) as error:
        module.main(["--repos", "JavaGuide"])
    assert error.value.code == 2
    assert not module.OUT_DIR.exists()


def test_first_build_without_existing_bank_profiles_or_fallbacks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for directory in ("bagu_trainer", "scripts"):
        shutil.copytree(root / directory, tmp_path / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tmp_path / "data").mkdir()
    source = tmp_path / "interview-banks" / "repos" / "JavaGuide" / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# 什么是数据库索引？\n\n"
        "数据库索引用额外的数据结构记录键值与数据位置的对应关系。"
        "查询可以利用索引缩小扫描范围。维护索引需要存储空间，"
        "插入和更新数据也可能增加索引维护成本，因此应结合查询条件选择索引。\n",
        encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", BAGU_USER_DATA_DIR=str(tmp_path))
    for arguments in (
        ["scripts/dedupe_questions.py", "--repos", "JavaGuide"],
        ["scripts/import_questions.py"],
        ["scripts/tag_question_knowledge.py"],
        ["scripts/build_concept_profiles.py"],
    ):
        result = subprocess.run([sys.executable, "-B", *arguments], cwd=tmp_path,
                                env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    data = tmp_path / "data"
    questions = json.loads((data / "questions.json").read_text(encoding="utf-8"))
    profiles = json.loads((data / "question_concepts.json").read_text(encoding="utf-8"))
    report = json.loads((data / "concept_profile_report.json").read_text(encoding="utf-8"))
    assert len(questions) == 1
    assert set(profiles["profiles"]) == {questions[0]["id"]}
    assert report["backup"] is None
    assert not (data / "fallback_quizzes.json").exists()
    assert not (tmp_path / "config.json").exists()
