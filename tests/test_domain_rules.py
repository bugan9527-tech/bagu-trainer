"""Public pure-rule scenarios with explicit data, time and policy."""
import json
from pathlib import Path

from bagu_trainer.domain.grading import Scorer
from bagu_trainer.domain.matching import Matcher
from bagu_trainer.domain.quizzes import QuizPolicy
from bagu_trainer.domain.quizzes import admission_failure
from bagu_trainer.domain.quizzes import generation_eligible
from bagu_trainer.domain.quizzes import is_expired
from bagu_trainer.domain.quizzes import parse_quiz
from bagu_trainer.domain.quizzes import quiz_deadline
from bagu_trainer.domain.quizzes import repractice_failure
from bagu_trainer.domain.review import ReviewPolicy
from bagu_trainer.domain.review import next_state
from bagu_trainer.domain.review import review_candidates


QUESTIONS = [
    {"id": "ttl", "knowledge_point": "Redis 缓存有效期", "question": "缓存 TTL 与主动失效如何配合避免读到陈旧数据？",
     "tags": ["redis"], "knowledge_tags": ["Redis", "缓存有效期"], "primary_knowledge_tag": "Redis"},
    {"id": "penetration", "knowledge_point": "缓存穿透", "question": "不存在的数据反复查询数据库，如何用空值缓存和布隆过滤器防止缓存穿透？",
     "tags": ["redis"], "knowledge_tags": ["Redis", "缓存穿透"], "primary_knowledge_tag": "缓存穿透"},
    {"id": "npe", "knowledge_point": "Java 空指针异常", "question": "Java 登录接口 NullPointerException 空指针异常如何定位？",
     "tags": ["java"], "knowledge_tags": ["Java", "异常"], "primary_knowledge_tag": "Java"},
    {"id": "index", "knowledge_point": "MySQL 索引", "question": "MySQL 联合索引的最左前缀原则与查询执行计划如何关联？",
     "tags": ["mysql"], "knowledge_tags": ["MySQL", "索引"], "primary_knowledge_tag": "索引"},
]
PROFILES = {"ttl": {"concept_terms": ["过期时间", "缓存失效", "陈旧状态"],
                    "concept_summary": "延长缓存有效期可能让陈旧数据可见更久"}}
QUERIES = ["Redis 缓存 TTL 从60秒改为600秒，没有主动失效，陈旧数据会怎样？",
           "查询不存在的 id，空值缓存和布隆过滤器如何防止缓存穿透？",
           "Java 登录接口抛出 NullPointerException 空指针异常",
           "MySQL 查询联合索引没有命中，如何看最左前缀与执行计划？",
           "更新 README 文档，调整 CSS 按钮间距"]


def test_matching_matches_frozen_pre_refactor_results_without_io(monkeypatch):
    expected = json.loads((Path(__file__).parent / "fixtures" / "matching_rule_baseline.json").read_text(encoding="utf-8"))
    from bagu_trainer.infrastructure import config
    from bagu_trainer.infrastructure import secrets
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure rules attempted infrastructure access")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(config, "load_config", forbidden)
    monkeypatch.setattr(secrets, "resolve_api_key", forbidden)
    matcher = Matcher(QUESTIONS, concept_profiles=PROFILES, raw_min=0.0)
    actual = [[{key: value for key, value in item.items() if key != "question"}
               for item in matcher.rank(query)] for query in QUERIES]
    assert actual == expected
    assert any(actual) and actual[-1] == []


def test_grading_and_review_use_explicit_time_and_policy():
    grade = Scorer().grade({"answer": "A", "kp_name": "缓存", "analysis": "解释"}, "选项A")
    assert grade["correct"] and grade["normalized_answer"] == "A"
    policy = ReviewPolicy(first_interval_days=2, min_interval_days=.5)
    state = next_state(grade["correct"], None, 100, policy=policy)
    assert state["next_review_at"] == 100 + 2 * 86400
    failed = next_state(False, state, 200, policy=policy)
    assert failed["next_review_at"] == 200 + .5 * 86400 and failed["attempts"] == 2
    due = [{"kp_id": "a", "next_review_at": 1}, {"kp_id": "b", "next_review_at": 2},
           {"kp_id": "c", "next_review_at": 3}]
    assert [item["kp_id"] for item in review_candidates(due, {"c"}, ["b"])] == ["b", "a"]


def test_explicit_retry_never_bypasses_reservation_pending_or_review_cooldown():
    assert generation_eligible(now=100, fresh=False, explicit_retry=True)
    for blocker in ({"reserved": True}, {"pending": True}, {"next_review_at": 101}):
        assert not generation_eligible(now=100, fresh=False, explicit_retry=True, **blocker)
    assert generation_eligible(now=100, next_review_at=100)
    assert quiz_deadline(100, 10) == 110
    assert is_expired(110, now=110, ttl_seconds=10)
    assert not is_expired(99, now=100, ttl_seconds=0)


def test_question_content_policy_is_separate_from_model_transport():
    document = {"question": "当前缓存应如何处理？", "options": dict(A="a", B="b", C="c", D="d"),
                "answer": "A", "analysis": "缓存解释"}
    content = json.dumps(document)
    assert parse_quiz(content) == document
    assert parse_quiz(content, policy=QuizPolicy(question_min_len=100)) is None
    snapshot = {**document, "generated_by": "llm"}
    assert admission_failure(snapshot) is None and repractice_failure(snapshot) is None
    assert admission_failure(snapshot, require_review=True)
    assert admission_failure({**snapshot, "generated_by": "offline"})
