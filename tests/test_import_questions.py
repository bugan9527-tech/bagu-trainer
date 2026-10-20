# -*- coding: utf-8 -*-
"""导入脚本测试：tmp_path 全 fake，校验备份数量、excerpt 提取、missing 标记与报告统计。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402
from scripts import import_questions as imp  # noqa: E402


def _fake_repos(tmp_path):
    repos = tmp_path / "repos"
    (repos / "r1").mkdir(parents=True)
    (repos / "r1" / "a.md").write_text(
        "## 什么是读写分离？\n读写分离将读写分流到不同节点。\n\n"
        "## 分库分表\n分库分表是另一回事。\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("## 什么是读写分离？\n越界机密。\n",
                                          encoding="utf-8")
    return repos


def _export():
    return [
        {"question": "什么是读写分离？",
         "tags": ["interview-bank", "r1"],
         "source_paths": ["r1/a.md"]},
        {"question": "完全没出现在任何文件里的标题？",
         "tags": ["interview-bank", "r1"],
         "source_paths": ["r1/a.md"]},
        {"question": "文件都不存在的标题？",
         "tags": ["interview-bank", "r1"],
         "source_paths": ["r1/nothing.md"]},
        {"question": "越界路径的标题？",
         "tags": ["interview-bank", "r1"],
         "source_paths": ["../outside.md"]},
    ]


def test_build_bank_excerpt_and_statuses(tmp_path):
    repos = _fake_repos(tmp_path)
    bank, stat = imp.build_bank(_export(), repos, excerpt_limit=500)
    assert stat["ok"] == 1 and stat["missing"] == 3 and stat["none"] == 0
    by_q = {q["question"]: q for q in bank}
    q = by_q["什么是读写分离？"]
    assert q["source_status"] == "ok"
    assert q["excerpt_source"] == "r1/a.md"
    assert "读写" in q["source_excerpt"]
    assert len(q["source_excerpt"]) <= 500
    assert q["source_paths"] == ["r1/a.md"]
    # missing 不伪造：excerpt 为空
    for title in ("完全没出现在任何文件里的标题？", "文件都不存在的标题？",
                  "越界路径的标题？"):
        m = by_q[title]
        assert m["source_status"] == "missing" and m["source_excerpt"] == ""
    # 越界文件内容绝不会被读到
    joined = " ".join(q["source_excerpt"] for q in bank)
    assert "机密" not in joined


def test_build_bank_skips_blank_and_counts(tmp_path):
    repos = _fake_repos(tmp_path)
    export = _export() + [{"question": "   ", "tags": ["x"]}]
    bank, stat = imp.build_bank(export, repos, excerpt_limit=500)
    assert stat["skipped_blank"] == 1
    assert len(bank) == len(export) - 1


def test_build_bank_filters_title_noise_and_writes_clean_titles(tmp_path):
    repos = _fake_repos(tmp_path)
    export = _export() + [
        {"question": "@ghost2113 我觉得不对吧",
         "tags": ["interview-bank", "r1"], "source_paths": ["r1/a.md"]},
        {"question": "https://example.com/post/1",
         "tags": ["interview-bank", "r1"], "source_paths": ["r1/a.md"]},
        {"question": "怎么上手",
         "tags": ["interview-bank", "r1"], "source_paths": ["r1/a.md"]},
        {"question": "[什么是读写分离？](https://example.com/x)",
         "tags": ["interview-bank", "r1"], "source_paths": ["r1/a.md"]},
    ]
    bank, stat = imp.build_bank(export, repos, excerpt_limit=500)
    titles = [q["question"] for q in bank]
    assert stat["title_rejected"] == 3
    assert {
        "@ghost2113 我觉得不对吧",
        "https://example.com/post/1",
        "怎么上手",
    }.isdisjoint(titles)
    assert titles.count("什么是读写分离？") == 2
    reasons = {x["reason"] for x in stat["title_cleanup"]}
    assert reasons == {"github_comment", "url_only", "generic_title"}


def test_bank_stats_rates():
    bank = [
        {"id": "a", "options": {}, "answer": "", "analysis": "",
         "source_status": "ok"},
        {"id": "b", "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
         "answer": "A", "analysis": "解析文案", "source_status": "ok"},
    ]
    s = imp.bank_stats(bank)
    assert s["total"] == 2
    assert s["options_empty_rate"] == 50.0
    assert s["answer_empty_rate"] == 50.0
    assert s["source_missing"] == 0
    assert s["verified"] == 1


def test_main_backup_count_verified_and_reports(tmp_path, capsys):
    repos = _fake_repos(tmp_path)
    bank_path = tmp_path / "questions.json"
    backup_path = tmp_path / "questions_backup.json"
    src = tmp_path / "export.json"
    old = [{"id": "old-1", "question": "旧题", "options": {},
            "answer": "", "analysis": "来源: x/y.md"}]
    bank_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    src.write_text(json.dumps(_export(), ensure_ascii=False), encoding="utf-8")

    title_report = tmp_path / "title_cleanup.json"
    result = imp.main(src=src, repos=repos, bank_path=bank_path,
                      backup_path=backup_path, excerpt_limit=500,
                      title_report_path=title_report)
    # 备份数量与原库一致
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert len(backup) == len(old)
    # 写入数量 = export 有效条数
    written = json.loads(bank_path.read_text(encoding="utf-8"))
    assert len(written) == 4
    # 报告含前后数量/空值率/来源缺失率
    assert result["before"]["total"] == 1
    assert result["after"]["total"] == 4
    assert result["after"]["source_missing"] == 3
    out = capsys.readouterr().out
    assert "导入前" in out and "导入后" in out
    assert "来源缺失率" in out
    assert title_report.exists()
