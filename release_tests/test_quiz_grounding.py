"""Grounding/review release gates with fixed model responses and no external API."""
import copy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from bagu_trainer import quiz_generator as qg


CONTEXT = "订单状态查询将缓存 TTL 从 60 秒改为 600 秒，没有增加主动失效机制。"
SOURCE = (
    "缓存有效期决定缓存条目在多久后失效。数据源发生更新时，如果没有主动失效或"
    "同步更新缓存的机制，读取方可能继续得到旧数据。较长的有效期通常减少缓存"
    "失效后的数据库访问次数，但也可能延长陈旧数据被读取的时间。选择有效期时"
    "需要结合业务允许的数据更新延迟和数据库访问成本，不能只考虑缓存命中率。"
)
CANDIDATE = {
    "question": "未增加主动失效机制时，延长订单状态缓存 TTL 后，应重点关注什么？",
    "options": {"A": "旧订单状态可能被读取更久", "B": "所有查询都立即读取新状态",
                "C": "数据库不再需要保存订单", "D": "缓存自动获得强一致性"},
    "answer": "A", "analysis": "较长的有效期可能延长陈旧数据被读取的时间。",
    "grounding": {"context_quote": "缓存 TTL 从 60 秒改为 600 秒",
                  "source_quote": "但也可能延长陈旧数据被读取的时间",
                  "rationale": "订单状态查询延长有效期又没有主动失效，应考查陈旧状态可见时间的变化。"},
}
REVIEW = {"context_relevant": True, "source_supported": True, "answer_unique": True,
          "no_answer_leak": True, "correct_answer": "A",
          "reason": "题目针对订单状态缓存变更考查陈旧性取舍，来源支持唯一答案。"}


@pytest.fixture
def model(monkeypatch):
    llm = {"base_url": "https://example.invalid/v1", "api_key": "local-fixture-key",
           "model": "fixture", "timeout_sec": 5}
    monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: llm))
    return llm


def run(candidate=None, review=None, *, context=CONTEXT, source=SOURCE, hook=None):
    calls = []
    candidate = copy.deepcopy(CANDIDATE) if candidate is None else candidate
    review = copy.deepcopy(REVIEW) if review is None else review

    def opener(request, timeout=None):
        payload = json.loads(request.data)
        calls.append({"payload": payload, "timeout": timeout})
        if hook:
            hook(calls)
        reply = candidate if len(calls) == 1 else review
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode())

    question = {"id": "fixture", "knowledge_point": "缓存有效期", "question": "如何选择缓存有效期？",
                "difficulty": "easy", "source_excerpt": source}
    result = qg.QuizGenerator(urlopen=opener).generate(question, context)
    return result, calls


def unavailable(result):
    assert result["available"] is False
    assert not {"question", "options", "answer", "analysis", "grounding"} & result.keys()


def test_success_has_verified_quotes_separate_review_and_receipt(model):
    result, calls = run()
    assert result["generated_by"] == "llm" and len(calls) == 2
    audit = json.loads(calls[1]["payload"]["messages"][-1]["content"])
    assert audit["task"] == "review_context_grounding"
    assert audit["context"] == CONTEXT and audit["source"] == SOURCE
    assert audit["candidate"]["question"] == CANDIDATE["question"]
    assert calls[1]["payload"]["temperature"] == 0
    assert result["grounding"]["context_sha256"] == hashlib.sha256(CONTEXT.encode()).hexdigest()
    assert result["grounding"]["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()


@pytest.mark.parametrize("kind", ["手动描述", "项目会话"])
def test_manual_and_session_context_do_not_require_a_file_diff(model, kind):
    result, calls = run(context=kind + "：" + CONTEXT)
    assert result["generated_by"] == "llm" and len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("context_quote", "不存在的 SQL 文件改动"),
    ("source_quote", "来源里不存在的更新保证与数据一致性结论"),
    ("rationale", "相关"),
])
def test_invalid_evidence_stops_before_review(model, field, value):
    candidate = copy.deepcopy(CANDIDATE)
    candidate["grounding"][field] = value
    result, calls = run(candidate)
    unavailable(result)
    assert len(calls) == 1


def test_missing_evidence_cannot_bypass_review(model):
    candidate = copy.deepcopy(CANDIDATE)
    candidate.pop("grounding")
    result, calls = run(candidate)
    unavailable(result)
    assert len(calls) == 1


@pytest.mark.parametrize("question", ["Redis 默认端口是什么？", "在 orders.py 中，Redis 默认端口是什么？"])
def test_generic_or_filename_only_question_is_rejected_by_review(model, question):
    candidate = dict(CANDIDATE, question=question)
    result, calls = run(candidate, dict(REVIEW, context_relevant=False))
    unavailable(result)
    assert "题目与上下文" in result["unavailable_reason"] and len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("source_supported", False), ("answer_unique", False), ("no_answer_leak", False),
    ("correct_answer", "B"), ("context_relevant", "true"), ("source_supported", 1),
    ("reason", ""),
])
def test_negative_incomplete_or_inconsistent_review_is_not_served(model, field, value):
    result, calls = run(review=dict(REVIEW, **{field: value}))
    unavailable(result)
    assert len(calls) == 2


def test_review_outage_does_not_leak_backend_error_or_serve_candidate(model):
    result, calls = run(review=RuntimeError("sensitive-provider-response"))
    unavailable(result)
    assert "审核未完成" in result["unavailable_reason"]
    assert "sensitive-provider-response" not in json.dumps(result)
    assert len(calls) == 2


def test_generation_and_review_share_one_timeout_budget(model, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(qg, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    result, calls = run(hook=lambda calls: clock.__setitem__(0, 3.0 if len(calls) == 1 else 4.0))
    assert result["generated_by"] == "llm"
    assert [call["timeout"] for call in calls] == [5.0, 2.0]


def test_spent_generation_budget_does_not_start_review(model, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(qg, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    result, calls = run(hook=lambda calls: clock.__setitem__(0, 6.0))
    unavailable(result)
    assert len(calls) == 1


def test_quote_outside_sent_truncated_context_is_rejected(model):
    context = "无关文本" * 600 + CONTEXT
    result, calls = run(context=context)
    unavailable(result)
    assert len(calls) == 1


def test_duplicate_json_fields_are_rejected(model):
    duplicate = json.dumps(CANDIDATE, ensure_ascii=False)[:-1] + ', "answer": "A"}'
    result, calls = run(candidate=duplicate)
    unavailable(result)
    assert len(calls) == 1


def test_empty_context_is_an_actionable_failure(model):
    result, calls = run(context=" ")
    unavailable(result)
    assert "缺少工作上下文" in result["unavailable_reason"] and calls == []


def test_both_model_requests_redact_source_and_context_secrets(model):
    extra = '\napi_key="local-fixture-key"\npassword=not-a-real-password\n'
    result, calls = run(context=CONTEXT + extra, source=SOURCE + extra)
    assert result["generated_by"] == "llm" and len(calls) == 2
    for call in calls:
        body = json.dumps(call["payload"], ensure_ascii=False)
        assert "local-fixture-key" not in body and "not-a-real-password" not in body
        assert "[REDACTED]" in body
