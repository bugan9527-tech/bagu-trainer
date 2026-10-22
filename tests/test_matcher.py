# -*- coding: utf-8 -*-
"""Matcher 重构（BM25 相关性检索）的验收测试。

覆盖：评测集全量指标、用户指定场景（NPE 不再命中链表）、门控行为、
旧接口兼容性、确定性、分词噪声过滤、server 端 confidence/margin 字段。

不依赖网络 / Redis / 真实文件监听 / 用户会话；题库为项目自带 data/questions.json。
"""
import pytest

from bagu_trainer.knowledge_base import KnowledgeBase
from bagu_trainer.matcher import Matcher, tokenize
from eval_cases import NEGATIVE_CASES, POSITIVE_CASES


@pytest.fixture(scope="module")
def matcher(benchmark_root):
    """真实题库上的 Matcher（构建一次，全模块复用）"""
    import time
    t = time.time()
    m = Matcher(KnowledgeBase(benchmark_root / "data" / "questions.json"))
    print(f"[perf] matcher 索引构建 {time.time() - t:.2f}s")
    return m


# ---- 评测集结构校验 ----
def test_eval_set_sizes():
    assert len(POSITIVE_CASES) >= 40
    assert len(NEGATIVE_CASES) >= 20
    for c in POSITIVE_CASES:
        assert c.get("context") and c.get("accept")
    for c in NEGATIVE_CASES:
        assert c.get("no_match") is True and c.get("context")


# ---- 评测集指标（确定性断言，替换“声称更准”）----
def test_negatives_all_return_empty(matcher):
    triggered = []
    for c in NEGATIVE_CASES:
        out = matcher.match(c["context"], top_n=3)
        if out:
            triggered.append((c["label"], [q["id"] for q, _s, _h in out]))
    assert triggered == [], f"负向误触发: {triggered}"


def test_positives_top1_top3_full_hit(matcher):
    miss1, miss3 = [], []
    for c in POSITIVE_CASES:
        out = matcher.match(c["context"], top_n=3)
        ids = [q["id"] for q, _s, _h in out]
        if not ids or ids[0] not in c["accept"]:
            miss1.append((c["label"], ids[:3]))
        if not (set(ids) & c["accept"]):
            miss3.append((c["label"], ids))
    assert miss1 == [], f"top1 未命中: {miss1}"
    assert miss3 == [], f"top3 未命中: {miss3}"


# ---- 用户指定场景：Java NPE 不再第一顺位命中 C/C++ 指针题 ----
def test_nullpointer_case_not_surface_unrelated(matcher):
    ctx = "修复登录接口空指针异常，堆栈显示 NullPointerException"
    r = matcher.rank(ctx, top_n=3)
    assert r, "空指针场景不得返回空匹配"
    pointer_ids = {
        "bank-4391", "bank-4505", "bank-4542", "bank-4543", "bank-4601",
        "bank-4699",
    }
    assert r[0]["id"] in {"bank-0187", "bank-0200", "bank-0315",
                          "bank-1099", "bank-2355", "bank-2878"}
    assert r[0]["id"] not in pointer_ids
    assert not (pointer_ids & {x["id"] for x in r})


# ---- 旧接口兼容性 ----
def test_match_signature_and_ordering(matcher):
    out = matcher.match("给商品查询接口加 Redis 缓存防止缓存穿透", top_n=3)
    assert isinstance(out, list) and all(isinstance(x, tuple) and len(x) == 3 for x in out)
    scores = [s for _q, s, _h in out]
    assert scores == sorted(scores, reverse=True), "必须按分数降序"
    assert all(0.0 <= s <= 1.0 for s in scores)
    for q, _s, hits in out:
        assert q.get("id") and isinstance(hits, list)
        assert all(isinstance(h, str) for h in hits)


def test_rank_extra_fields(matcher):
    r = matcher.rank("两个并发事务循环等待行锁产生死锁", top_n=2)
    assert r
    for x in r:
        assert 0.0 <= x["confidence"] <= 1.0
        assert 0.0 <= x["margin"] <= 1.0
        assert "raw_score" in x and "id" in x


def test_min_score_semantics(matcher):
    # min_score >= 1（旧整数语义）等价于不设阈值，由门控决定
    a = matcher.match("用布隆过滤器挡掉不存在的 key", top_n=1, min_score=1)
    b = matcher.match("用布隆过滤器挡掉不存在的 key", top_n=1)
    assert [q["id"] for q, _s, _h in a] == [q["id"] for q, _s, _h in b]


def test_deterministic(matcher):
    ctx = "Kafka 消费者重复消费，业务侧做幂等去重"
    r1 = matcher.rank(ctx, top_n=3)
    r2 = matcher.rank(ctx, top_n=3)
    assert [x["id"] for x in r1] == [x["id"] for x in r2]
    assert [x["score"] for x in r1] == [x["score"] for x in r2]


def test_empty_and_garbage_input(matcher):
    assert matcher.match("", top_n=3) == []
    assert matcher.match("   ", top_n=3) == []
    # 全部词条都不在题库词表中的输入：无候选可匹配
    assert matcher.match("zzzqqq 维他命", top_n=3) == []
    assert matcher.rank(None, top_n=3) == []


# ---- 分词与噪声过滤 ----
def test_tokenize_filters_generic_grams():
    toks = tokenize("如何实现读写分离")
    assert "读写" in toks and "分离" in toks and "读写分" in toks
    for junk in ("如何", "实现", "何实", "如何实", "何实现"):
        assert junk not in toks


def test_tokenize_english_boundary_and_camel():
    toks = tokenize("NullPointerException rages on the heap")
    assert "null" in toks and "pointer" in toks and "exception" in toks
    assert "the" not in toks and "on" not in toks
    toks2 = tokenize("cache_penetration")
    assert "cache" in toks2 and "penetration" in toks2


def test_tokenize_junk_char_grams():
    toks = tokenize("调用了对方的接口")
    assert "对方的" not in toks and "调用了" not in toks
    assert "调用" in toks  # 正常词保留，仅函数字符粘连的组合 gram 被丢弃


# ---- server 端：resolve confidence/margin 增量字段 ----
def test_server_surfaces_confidence_and_margin(env):
    import bagu_trainer.engine as engine
    env["diff"].results = [{"source": "git", "files": ["x.py"],
                            "diff_text": "redis 缓存穿透"}]
    rep = engine.process_activity()
    assert rep["ok"] is True and len(rep["quizzes"]) == 1
    q = rep["quizzes"][0]
    assert "confidence" in q and "margin" in q
    m = rep["matched"][0]
    assert "confidence" in m and "margin" in m
    assert 0.0 <= m["confidence"] <= 1.0 and 0.0 <= m["margin"] <= 1.0
