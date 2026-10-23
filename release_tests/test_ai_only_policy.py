"""Release gate: a complete offline question must never bypass model generation."""
import io
import json

import pytest

from bagu_trainer import quiz_generator as qg


@pytest.mark.parametrize("failure", ["no_model", "timeout", "invalid_output", "missing_excerpt"])
def test_offline_question_cannot_replace_failed_ai(monkeypatch, failure):
    question = {
        "id": "fixture", "knowledge_point": "缓存", "difficulty": "easy",
        "question": "如何减少不存在数据的重复查询？",
        "options": {"A": "缓存空结果", "B": "忽略所有请求", "C": "删除数据库", "D": "禁用缓存"},
        "answer": "A", "analysis": "缓存空结果可以减少不存在数据的重复查询。",
        "manual_verified": True,
        "source_excerpt": (
            "缓存穿透是查询不存在的数据，缓存中没有对应内容，请求会继续访问数据库。"
            "如果相同的无效查询不断出现，可以缓存空结果并设置较短的过期时间，减少"
            "数据库重复查询。也可以使用布隆过滤器提前过滤明显不存在的键，但仍需"
            "处理过滤器的误判和数据更新，不能把过滤器判断当作数据存在的确凿证明。"
        ),
    }
    llm = {"base_url": "https://example.invalid/v1", "api_key": "local-fixture-key", "model": "fixture"}
    monkeypatch.setattr(qg.QuizGenerator, "_current_llm", staticmethod(lambda: None if failure == "no_model" else llm))
    monkeypatch.setattr(qg.fallback_bank, "find_fallback", lambda *_: pytest.fail("Offline fallback is forbidden"))
    requests = []

    def opener(request, timeout=None):
        requests.append(request)
        if failure == "timeout":
            raise TimeoutError("fixture")
        assert failure == "invalid_output"
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "invalid JSON"}}]}).encode())

    if failure == "missing_excerpt":
        question["source_excerpt"] = ""
    quiz = qg.QuizGenerator(urlopen=opener).generate(question, "手动描述缓存空结果的过期策略")
    assert quiz["available"] is False and quiz["generated_by"] == "unavailable"
    assert not {"question", "options", "answer", "analysis"} & quiz.keys()
    assert bool(requests) is (failure in {"timeout", "invalid_output"})
