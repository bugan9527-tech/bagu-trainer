# -*- coding: utf-8 -*-
"""出题模块测试：无 LLM / 超时 / 非法 JSON / 重复选项 / 空选项 / 空答案 /
无来源片段拒合成。全部 fake，不触碰真实 LLM 与真实 config.json。
"""
import json
import urllib.error

import pytest

import bagu_trainer.application.learning.generator as qg
from bagu_trainer.domain.quizzes import parse_quiz

UNVERIFIED = {
    "id": "u1", "knowledge_point": "读写分离", "difficulty": "medium",
    "question": "如何实现读写分离？",
    "options": {}, "answer": "",
    "analysis": "来源: r/a.md",
    "source_paths": ["r/a.md"],
    "source_excerpt": (
        "读写分离通常会把读请求与写请求路由到不同节点：主库负责写操作并维护"
        "复制关系，只读副本承担查询流量；实现时需要配置主从复制、处理复制"
        "延迟，并让应用根据一致性要求选择读主库还是读从库。"
    ),
    "excerpt_source": "r/a.md",
}

VERIFIED = {
    "id": "v1", "knowledge_point": "缓存穿透", "difficulty": "easy",
    "question": "以下关于缓存穿透的说法正确的是？",
    "options": {"A": "空值缓存", "B": "限流", "C": "熔断", "D": "降级"},
    "answer": "A",
    "analysis": "缓存穿透指查询不存在的数据，可用空值缓存短 TTL 兜底。",
    "source_paths": ["r/cache.md"],
    "source_excerpt": (
        "缓存穿透是查询不存在的数据，缓存中没有对应内容，请求会继续访问数据库。"
        "如果相同的无效查询不断出现，可以缓存空结果并设置较短的过期时间，减少"
        "数据库重复查询。也可以使用布隆过滤器提前过滤明显不存在的键，但仍需"
        "处理过滤器的误判和数据更新，不能把过滤器判断当作数据存在的确凿证明。"
    ),
    "excerpt_source": "r/cache.md",
}

NO_EXCERPT = dict(UNVERIFIED, source_excerpt="", excerpt_source="")

VALID_LLM_CONTENT = json.dumps({
    "question": "结合场景，实现读写分离的正确步骤是？",
    "options": {"A": "部署主从并建立复制", "B": "只扩容主库", "C": "关闭复制", "D": "删除从库"},
    "answer": "A",
    "analysis": "读写分离需要主从复制支撑，易错点是直接扩容主库。",
    "grounding": {
        "context_quote": "订单查询增加读写分离",
        "source_quote": "主库负责写操作并维护复制关系",
        "rationale": "订单查询要将读写请求分流，需要通过主从复制支持只读节点查询。",
    },
}, ensure_ascii=False)

VALID_REVIEW = {"context_relevant": True, "source_supported": True,
                "answer_unique": True, "no_answer_leak": True, "correct_answer": "A",
                "reason": "当前订单查询场景与主从复制分流的依据相符，答案唯一。"}


class FakeResp:
    def __init__(self, text):
        self._t = text

    def read(self, size=-1):
        body = self._t.encode("utf-8")
        return body if size < 0 else body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_urlopen(respond=None, error=None, capture=None):
    def urlopen(req, timeout=None):
        payload = json.loads(req.data.decode("utf-8"))
        is_review = "review_context_grounding" in payload["messages"][-1]["content"]
        if capture is not None:
            capture["review_payload" if is_review else "payload"] = payload
        if error is not None:
            raise error
        if respond is None:
            raise AssertionError("不应发起的 LLM 调用")
        return FakeResp(json.dumps(
            {"choices": [{"message": {"content": json.dumps(VALID_REVIEW) if is_review else respond}}]},
            ensure_ascii=False))

    return urlopen


def no_llm(monkeypatch):
    monkeypatch.setattr(qg, "load_config", lambda: {})


def with_llm(monkeypatch, llm):
    monkeypatch.setattr(qg, "load_config", lambda: {"llm": llm})


# ---- _parse_quiz 严格校验 ----
def test_parse_quiz_accepts_valid():
    d = parse_quiz("```json\n" + VALID_LLM_CONTENT + "\n```")
    assert d and d["answer"] == "A" and len(d["options"]) == 4


def test_parse_quiz_rejects_duplicate_options():
    content = json.dumps({
        "question": "q12345", "answer": "A", "analysis": "x",
        "options": {"A": "同", "B": "同", "C": "三", "D": "四"}}, ensure_ascii=False)
    assert parse_quiz(content) is None


def test_parse_quiz_rejects_empty_option():
    content = json.dumps({
        "question": "q12345", "answer": "A", "analysis": "x",
        "options": {"A": "", "B": "二", "C": "三", "D": "四"}}, ensure_ascii=False)
    assert parse_quiz(content) is None


def test_parse_quiz_rejects_empty_answer():
    content = json.dumps({
        "question": "q12345", "answer": "", "analysis": "x",
        "options": {"A": "一", "B": "二", "C": "三", "D": "四"}}, ensure_ascii=False)
    assert parse_quiz(content) is None


def test_parse_quiz_rejects_missing_analysis():
    content = json.dumps({
        "question": "q12345", "answer": "A", "analysis": "  ",
        "options": {"A": "一", "B": "二", "C": "三", "D": "四"}}, ensure_ascii=False)
    assert parse_quiz(content) is None


def test_parse_quiz_rejects_absurd_question_length():
    content = json.dumps({
        "question": "短", "answer": "A", "analysis": "x",
        "options": {"A": "一", "B": "二", "C": "三", "D": "四"}}, ensure_ascii=False)
    assert parse_quiz(content) is None


def test_parse_quiz_rejects_invalid_json():
    assert parse_quiz("这不是 JSON") is None


# ---- 无 LLM ----
def test_no_llm_unverified_returns_unavailable(monkeypatch):
    no_llm(monkeypatch)
    gen = qg.QuizGenerator(urlopen=make_urlopen())
    quiz = gen.generate(UNVERIFIED, "上下文")
    assert quiz.get("available") is False
    assert "未配置 LLM" in quiz["unavailable_reason"]
    assert quiz.get("generated_by") == "unavailable"
    assert "options" not in quiz and "answer" not in quiz  # 绝不返回空题


def test_no_llm_verified_quiz_is_not_served(monkeypatch):
    no_llm(monkeypatch)
    gen = qg.QuizGenerator(urlopen=make_urlopen())
    quiz = gen.generate(VERIFIED, "上下文")
    assert quiz["available"] is False
    assert quiz["generated_by"] == "unavailable"
    assert "未配置 LLM" in quiz["unavailable_reason"]
    assert not {"question", "options", "answer", "analysis"} & quiz.keys()


# ---- LLM 成功 + 提示词接地 ----
def test_llm_success_grounded_on_excerpt(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    capture = {}
    gen = qg.QuizGenerator(urlopen=make_urlopen(respond=VALID_LLM_CONTENT,
                                                capture=capture))
    quiz = gen.generate(UNVERIFIED, "为订单查询增加读写分离与主从复制")
    assert quiz.get("available") is not False
    assert quiz["generated_by"] == "llm"
    assert sorted(quiz["options"].keys()) == ["A", "B", "C", "D"]
    assert quiz["excerpt_source"] == "r/a.md"
    sys_msg = capture["payload"]["messages"][0]["content"]
    user_msg = capture["payload"]["messages"][1]["content"]
    assert UNVERIFIED["source_excerpt"] in user_msg  # 来源片段作为事实依据
    assert "订单查询增加读写分离" in user_msg
    assert quiz["grounding"]["review"]["context_relevant"] is True
    assert capture["review_payload"]["temperature"] == 0
    assert "不得暗示或暴露正确答案" in sys_msg
    assert quiz["answer"] in ("A", "B", "C", "D")
    # 题干不得以「答案是X」等形式泄露答案字母
    assert quiz["answer"] not in quiz["question"]
    assert "答案是" not in quiz["question"]


# ---- LLM 超时 ----
def test_llm_timeout_unavailable(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    gen = qg.QuizGenerator(urlopen=make_urlopen(error=TimeoutError("boom")))
    quiz = gen.generate(UNVERIFIED, "上下文")
    assert quiz.get("available") is False
    assert "LLM 调用失败" in quiz["unavailable_reason"]
    assert "options" not in quiz


def test_http_failure_reports_status_without_server_error_text(monkeypatch):
    import urllib.error
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    error = urllib.error.HTTPError("https://fake.local/private", 429,
                                   "sk-private-never-log-this", {}, None)
    gen = qg.QuizGenerator(urlopen=make_urlopen(error=error))
    quiz = gen.generate(UNVERIFIED, "缓存代码")
    assert quiz.get("available") is False
    assert "HTTP 429" in quiz["unavailable_reason"]
    assert "sk-private" not in str(quiz)


def test_llm_timeout_never_serves_verified_quiz(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    capture = {}
    gen = qg.QuizGenerator(urlopen=make_urlopen(error=TimeoutError("boom"), capture=capture))
    quiz = gen.generate(VERIFIED, "上下文")
    assert capture  # Exercise a real generator request, not the excerpt gate.
    assert quiz["available"] is False and "TimeoutError" in quiz["unavailable_reason"]
    assert "options" not in quiz and "answer" not in quiz


# ---- LLM 非法输出 ----
def test_llm_invalid_json_unavailable(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    gen = qg.QuizGenerator(urlopen=make_urlopen(respond="这不是 JSON"))
    quiz = gen.generate(UNVERIFIED, "上下文")
    assert quiz.get("available") is False
    assert "校验失败" in quiz["unavailable_reason"]
    assert "options" not in quiz


def test_llm_invalid_json_never_serves_verified_quiz(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    gen = qg.QuizGenerator(urlopen=make_urlopen(respond="{broken"))
    quiz = gen.generate(VERIFIED, "上下文")
    assert quiz["available"] is False and "校验失败" in quiz["unavailable_reason"]
    assert "options" not in quiz and "answer" not in quiz


# ---- 来源缺失：拒绝无依据合成 ----
def test_llm_refused_when_no_excerpt(monkeypatch):
    llm = {"base_url": "https://fake.local/v1", "api_key": "sk-test",
           "model": "m", "timeout_sec": 5}
    with_llm(monkeypatch, llm)
    capture = {}
    gen = qg.QuizGenerator(urlopen=make_urlopen(capture=capture))
    quiz = gen.generate(NO_EXCERPT, "上下文")
    assert quiz.get("available") is False
    assert "来源正文缺失" in quiz["unavailable_reason"]
    assert capture == {}  # 未发起任何 LLM 调用


# ---- 越界 source_path：提取拒绝（经 extractor 覆盖，这里验证生成侧兜底）----
def test_generator_handles_missing_excerpt_status(monkeypatch):
    no_llm(monkeypatch)
    gen = qg.QuizGenerator(urlopen=make_urlopen())
    broken = dict(UNVERIFIED, source_excerpt="", source_status="missing")
    quiz = gen.generate(broken, "上下文")
    assert quiz.get("available") is False
    assert "未配置 LLM" in quiz["unavailable_reason"]


@pytest.mark.parametrize("case", ["missing_source", "short_source", "invalid_quiz", "forged_quote",
                                  "source_supported", "context_relevant", "answer_unique",
                                  "no_answer_leak", "answer_conflict", "invalid_review"])
def test_quality_rejections_are_classified_without_relaxing_validation(monkeypatch, case):
    with_llm(monkeypatch, {"base_url": "https://fake.local/v1", "api_key": "sk-test", "model": "m"})
    question = dict(UNVERIFIED)
    content = json.loads(VALID_LLM_CONTENT)
    review = dict(VALID_REVIEW)
    if case == "missing_source":
        question["source_excerpt"] = ""
    elif case == "short_source":
        question["source_excerpt"] = "正文太短"
    elif case == "invalid_quiz":
        content["options"]["B"] = content["options"]["A"]
    elif case == "forged_quote":
        content["grounding"]["source_quote"] = "这是实际来源中不存在的正文引用片段"
    elif case == "answer_conflict":
        review["correct_answer"] = "B"
    elif case == "invalid_review":
        review = {"source_supported": "true"}
    else:
        review[case] = False
    monkeypatch.setitem(globals(), "VALID_REVIEW", review)
    capture = {}
    gen = qg.QuizGenerator(urlopen=make_urlopen(respond=json.dumps(content, ensure_ascii=False), capture=capture))
    quiz = gen.generate(question, "为订单查询增加读写分离与主从复制")
    assert quiz["available"] is False and quiz["failure_kind"] == "quality"
    assert quiz["unavailable_reason"]
    assert not {"question", "options", "answer", "analysis", "grounding"} & quiz.keys()
    if case in ("missing_source", "short_source"):
        assert not capture


@pytest.mark.parametrize("stage", ["generation", "review"])
@pytest.mark.parametrize("error_type", [TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError])
def test_service_errors_at_both_stages_remain_visible(monkeypatch, stage, error_type):
    with_llm(monkeypatch, {"base_url": "https://fake.local/v1", "api_key": "sk-test", "model": "m"})
    calls = []
    normal = make_urlopen(respond=VALID_LLM_CONTENT)

    def request(req, timeout=None):
        calls.append(1)
        if len(calls) == (1 if stage == "generation" else 2):
            if error_type is urllib.error.HTTPError:
                raise error_type("https://fake.local", 429, "private-response", {}, None)
            raise error_type("private-response")
        return normal(req, timeout=timeout)

    quiz = qg.QuizGenerator(urlopen=request).generate(UNVERIFIED, "为订单查询增加读写分离与主从复制")
    assert quiz["available"] is False and quiz["failure_kind"] == "service"
    assert "private-response" not in str(quiz)
    assert len(calls) == (1 if stage == "generation" else 2)
