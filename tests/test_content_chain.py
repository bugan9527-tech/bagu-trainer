# -*- coding: utf-8 -*-
"""内容链回归：来源格式、片段质量、LLM 闸门与离线人工验证题。"""
import json

import pytest

import bagu_trainer.quiz_generator as qg
import bagu_trainer.engine as engine
from bagu_trainer.fallback_bank import (
    find_fallback,
    load_fallback_quizzes,
    validate_fallback_quiz,
)
from bagu_trainer.source_extractor import (
    assess_excerpt,
    excerpt_for_detailed,
    extract_excerpt,
)


LONG_EXCERPT = (
    "读写分离会把读请求和写请求路由到不同节点：主库负责写操作并维护复制"
    "关系，只读副本承担查询流量；实现时需要配置主从复制、处理复制延迟，"
    "并让应用根据一致性要求选择读主库还是读从库。"
)


def test_pure_link_is_not_usable():
    result = assess_excerpt("[JWT 基础概念详解](./jwt-intro.md)")
    assert result["usable"] is False
    assert result["quality"] == "weak"
    assert result["reason"] == "link_only"
    html_link = assess_excerpt(
        '<a href="https://example.com/jwt">JWT 基础概念详解</a>')
    assert html_link["usable"] is False
    assert html_link["reason"] == "link_only"


def test_code_fragment_and_promo_are_not_usable():
    code = assess_excerpt("```python\nresult = retrieve_rag(goal, context)\n```")
    assert code["usable"] is False and code["reason"] == "code_only"
    tilde_code = assess_excerpt("~~~\nretrieve_rag(goal, context)\n~~~")
    assert tilde_code["usable"] is False and tilde_code["reason"] == "code_only"
    call_code = assess_excerpt("retrieve_rag(goal, business_context)")
    assert call_code["usable"] is False and call_code["reason"] == "code_only"
    promo = assess_excerpt("必须支持！推荐 RSS 订阅本网站获取最新更新。")
    assert promo["usable"] is False and promo["reason"] == "promo_only"


def test_question_list_without_explanation_is_not_usable():
    text = (
        "- Redis 常用数据结构分别适合哪些业务场景？\n"
        "- 缓存穿透、击穿、雪崩和缓存一致性问题应该如何设计方案？\n"
        "- RDB、AOF、AOF 重写和混合持久化有什么区别？\n"
        "- 主从复制、哨兵和集群分别适合什么规模？\n"
    )
    result = assess_excerpt(text)
    assert result["usable"] is False
    assert result["reason"] == "question_list"


def test_short_excerpt_does_not_enter_llm(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    monkeypatch.setattr(qg, "load_config", lambda: {"llm": llm})
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        raise AssertionError("短片段不应进入 LLM")

    kp = {
        "id": "short-1", "knowledge_point": "不可回退的短片段",
        "difficulty": "easy", "question": "短片段问题？",
        "source_excerpt": "缓存穿透是查询不存在的数据",
        "excerpt_source": "r/a.md",
    }
    quiz = qg.QuizGenerator(urlopen=urlopen).generate(kp, "上下文")
    assert calls["n"] == 0
    assert quiz["available"] is False
    assert "来源片段不可用" in quiz["unavailable_reason"]
    assert "options" not in quiz and "answer" not in quiz


def test_numbered_inline_code_anchor_and_bold_headings():
    md = """<a name="rw"></a>
### **`1、什么是读写分离？`**

读写分离将读请求和写请求分散到不同节点，写请求进入主库，读请求由只读副本承担，
从而降低单库压力。

## （二）如何实现读写分离？

先部署多个数据库节点并建立主从复制，再把写请求交给主库、读请求交给从库。
"""
    ex = extract_excerpt(md, "1. 什么是读写分离?")
    assert "降低单库压力" in ex
    assert "部署多个数据库节点" not in ex
    assert "如何实现" not in ex


def test_numbered_parent_heading_is_used_only_within_same_file():
    md = """# 数据库

## 读写分离

读写分离的核心是把写请求交给主库、读请求交给只读副本；主从复制负责同步数据，
应用需要处理复制延迟和一致性取舍。

## 分库分表

分库分表解决的是单库容量和单表数据量问题。
"""
    ex = extract_excerpt(md, "读写分离的实现方式")
    assert "主从复制" in ex and "单库压力" not in ex
    assert "分库分表" not in ex


def test_parent_fallback_never_combines_different_files(tmp_path):
    repos = tmp_path / "repos"
    (repos / "a").mkdir(parents=True)
    (repos / "b").mkdir(parents=True)
    (repos / "a" / "one.md").write_text(
        "## 缓存\n\n这里只讨论缓存容量。\n", encoding="utf-8")
    (repos / "b" / "two.md").write_text(
        "## 数据库\n\n这里只讨论数据库索引。\n", encoding="utf-8")
    detail = excerpt_for_detailed(
        repos, ["a/one.md", "b/two.md"], "缓存穿透的治理方式")
    assert detail["status"] == "missing"
    assert detail["excerpt"] == ""


def test_ok_status_can_still_be_unusable(tmp_path):
    repos = tmp_path / "repos"
    (repos / "r").mkdir(parents=True)
    (repos / "r" / "a.md").write_text(
        "## 什么是 JWT？\n\n[JWT 基础概念详解](./jwt-intro.md)\n",
        encoding="utf-8")
    detail = excerpt_for_detailed(repos, ["r/a.md"], "什么是 JWT？")
    assert detail["status"] == "ok"
    assert detail["usable"] is False
    assert detail["quality"] == "weak"


def test_fallback_file_has_at_least_twenty_complete_quizzes():
    items = load_fallback_quizzes()
    assert len(items) >= 20
    ids = set()
    for q in items:
        assert validate_fallback_quiz(q) == []
        assert q["id"] not in ids
        ids.add(q["id"])
        assert sorted(q["options"]) == ["A", "B", "C", "D"]
        assert len(set(q["options"].values())) == 4
        assert q["answer"] in ("A", "B", "C", "D")
        assert q["analysis"].strip()
        assert q.get("manual_verified") is True
        assert q.get("verification_note")


def test_offline_generator_serves_verified_fallback(monkeypatch):
    monkeypatch.setattr(qg, "load_config", lambda: {})
    item = load_fallback_quizzes()[0]
    kp = {
        "id": "bank-http-idempotent",
        "knowledge_point": item["knowledge_point"],
        "difficulty": "easy",
        "question": "HTTP 幂等方法有哪些？",
        "keywords": item["keywords"],
    }
    quiz = qg.QuizGenerator().generate(kp, "")
    assert quiz.get("available") is not False
    assert quiz["generated_by"] == "fallback"
    assert quiz["fallback_quiz_id"] == item["id"]
    assert sorted(quiz["options"]) == ["A", "B", "C", "D"]
    assert quiz["answer"] in ("A", "B", "C", "D")
    assert quiz["analysis"].strip()


def test_all_fallback_quizzes_are_answerable(monkeypatch):
    monkeypatch.setattr(qg, "load_config", lambda: {})
    gen = qg.QuizGenerator()
    for item in load_fallback_quizzes():
        quiz = gen.generate(item, "")
        assert quiz.get("available") is not False
        assert sorted(quiz["options"]) == ["A", "B", "C", "D"]
        assert len(set(quiz["options"].values())) == 4
        assert quiz["answer"] in ("A", "B", "C", "D")
        assert str(quiz["analysis"]).strip()


def test_fallback_selection_is_relevant():
    kp = {
        "id": "bank-cache-penetration",
        "knowledge_point": "缓存穿透是什么？",
        "question": "查询不存在的数据绕过缓存怎么办？",
        "keywords": ["缓存穿透", "空值缓存", "布隆过滤器"],
    }
    assert find_fallback(kp)["id"] == "fallback-005"


def test_fallback_selection_rejects_generic_question_words():
    for kp in (
        {"id": "a", "knowledge_point": "Agent Loop 的完整流程是什么？",
         "question": "Agent Loop 的完整流程是什么？", "keywords": []},
        {"id": "b", "knowledge_point": "接口和抽象类的区别",
         "question": "接口和抽象类的区别是什么？", "keywords": []},
        {"id": "c", "knowledge_point": "Agent 退款流程中的幂等设计",
         "question": "Agent 发起退款时如何保证幂等和审计？", "keywords": []},
    ):
        assert find_fallback(kp) is None


def test_create_quiz_tool_serves_fallback_id(env, monkeypatch):
    class EmptyKB:
        def get(self, qid):
            return None

    monkeypatch.setattr(engine, "kb", EmptyKB())
    monkeypatch.setattr(engine, "generator", qg.QuizGenerator())
    monkeypatch.setattr(qg, "load_config", lambda: {})
    result = engine.create_quiz("fallback-001", context="HTTP 并发场景")
    assert result["ok"] is True
    assert result["generated_by"] == "fallback"
    assert sorted(result["options"]) == ["A", "B", "C", "D"]
    internal = engine._state["quizzes"][result["quiz_id"]]
    assert internal["answer"] in ("A", "B", "C", "D")
    assert internal["analysis"].strip()


def test_startup_log_reports_offline_count(env, capsys):
    engine._startup_log()
    err = capsys.readouterr().err
    assert "已验证回退题:" in err
    assert f"当前离线可出 {len(load_fallback_quizzes())} 道" in err
