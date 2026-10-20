# -*- coding: utf-8 -*-
"""生成失败链路集成测试：unavailable 明确暴露、绝不产出无选项/无答案题目。
依赖 conftest 的全 fake 环境（无网络/无真实 LLM/无真实 config.json）。
"""
import bagu_trainer.engine as engine


class UnavailableGenerator:
    """假出题器：所有生成均失败（模拟未配置 LLM 且无已验证选择题）。"""

    def __init__(self):
        self.calls = 0

    def generate(self, kp_question, context_text):
        self.calls += 1
        return {
            "kp_id": kp_question["id"],
            "kp_name": kp_question["knowledge_point"],
            "difficulty": kp_question["difficulty"],
            "generated_by": "unavailable",
            "available": False,
            "unavailable_reason": "未配置 LLM（config.json 需提供 llm.api_key）；且该知识点无已验证选择题可回退",
        }


def _set_diff(diff, text):
    diff.results = [{"source": "git", "files": ["x.py"], "diff_text": text}]


def test_report_returns_unavailable_not_broken_quiz(env, monkeypatch):
    gen = UnavailableGenerator()
    monkeypatch.setattr(engine, "generator", gen)
    _set_diff(env["diff"], "redis 缓存穿透 空值缓存")
    rep = engine.process_activity()
    assert rep["ok"] is True
    assert rep["quizzes"] == []            # 不允许出现空题
    assert rep["unavailable"][0]["knowledge_point"] == "测试知识点"
    assert "未配置 LLM" in rep["unavailable"][0]["reason"]
    assert rep["note"] and "生成失败" in rep["note"]
    # 不产生任何 pending / quizzes 记录
    assert engine._state["pending"] == []
    assert engine._state["quizzes"] == {}


def test_check_pending_reports_unavailable(env, monkeypatch):
    gen = UnavailableGenerator()
    monkeypatch.setattr(engine, "generator", gen)
    _set_diff(env["diff"], "redis 缓存穿透")
    r = engine.next_quiz()
    assert r["ok"] is True and r["has_pending"] is False
    assert "unavailable" in r and r["unavailable"]
    assert "生成失败" in r["note"]
    assert r.get("quiz") is None


def test_create_quiz_tool_returns_unavailable(env, monkeypatch):
    gen = UnavailableGenerator()
    monkeypatch.setattr(engine, "generator", gen)

    class FakeKB:
        def get(self, qid):
            return {"id": qid, "knowledge_point": "缓存穿透",
                    "difficulty": "medium", "question": "如何解决缓存穿透？",
                    "options": {}, "answer": "", "analysis": "来源: x.md"}

    monkeypatch.setattr(engine, "kb", FakeKB())
    r = engine.create_quiz("kp-x", context="")
    assert r["ok"] is False
    assert "未配置 LLM" in r["reason"]
    assert "quiz_id" not in r and "options" not in r


def test_same_context_does_not_retry_generation(env, monkeypatch):
    gen = UnavailableGenerator()
    monkeypatch.setattr(engine, "generator", gen)
    _set_diff(env["diff"], "redis 缓存穿透")
    engine.next_quiz()
    assert gen.calls == 1
    engine.next_quiz()
    assert gen.calls == 1  # 去重指纹生效，失败结果不被反复重试