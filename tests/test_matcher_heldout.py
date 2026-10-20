# -*- coding: utf-8 -*-
"""Held-out and domain-gating acceptance tests for Matcher.

The cases live in ``eval_heldout.py`` and were authored before the matcher
change.  These tests assert safety behavior and deterministic domain
separation; they do not rewrite accept labels to make a result pass.
"""
import pytest

from bagu_trainer.knowledge_base import KnowledgeBase
from bagu_trainer.matcher import Matcher
from eval_heldout import (
    DOMAIN_SAFETY_CASES,
    NEGATIVE_CASES_HELDOUT,
    POSITIVE_CASES_HELDOUT,
)


@pytest.fixture(scope="module")
def matcher():
    return Matcher(KnowledgeBase())


def test_heldout_set_is_independent_and_large_enough():
    assert len(POSITIVE_CASES_HELDOUT) >= 15
    assert len(NEGATIVE_CASES_HELDOUT) >= 30
    categories = {c["category"] for c in NEGATIVE_CASES_HELDOUT}
    assert {"docs", "formatting", "style", "generic", "word_sense"} <= categories
    for case in POSITIVE_CASES_HELDOUT:
        assert case["context"] and case["accept"]
        assert case["domain"]
    for case in NEGATIVE_CASES_HELDOUT:
        assert case["context"] and case["no_match"] is True


def test_heldout_negatives_return_empty(matcher):
    triggered = []
    for case in NEGATIVE_CASES_HELDOUT:
        out = matcher.rank(case["context"], top_n=3)
        if out:
            triggered.append((case["label"], [x["id"] for x in out]))
    assert triggered == [], f"held-out 负向误触发: {triggered}"


def test_readme_and_css_known_repros_return_empty(matcher):
    for context in (
        "更新 README 文档，补充安装步骤和项目说明",
        "修改 React 组件按钮样式，调整 CSS 布局",
    ):
        assert matcher.match(context, top_n=3) == []
        assert matcher.rank(context, top_n=3) == []


def test_java_npe_only_matches_java_exception_or_empty(matcher):
    java_exception_ids = {
        "bank-0187", "bank-0200", "bank-0315", "bank-1099", "bank-2355",
        "bank-2878",
    }
    pointer_ids = {
        "bank-4391", "bank-4505", "bank-4542", "bank-4543", "bank-4601",
        "bank-4699",
    }
    for context in (
        "修复登录接口空指针异常",
        "修复登录接口空指针异常，堆栈显示 NullPointerException",
        "Java 登录接口抛出 NullPointerException，堆栈指向 user.getRole()",
    ):
        ranked = matcher.rank(context, top_n=3)
        if ranked:
            assert ranked[0]["id"] in java_exception_ids
            assert ranked[0]["id"] not in pointer_ids


def test_cpp_null_pointer_keeps_cpp_domain(matcher):
    context = "C++ 里空指针解引用和对象释放后的悬空指针有什么区别"
    ranked = matcher.rank(context, top_n=3)
    assert ranked
    assert ranked[0]["id"] in {
        "bank-4391", "bank-4505", "bank-4542", "bank-4543",
        "bank-4601", "bank-4699",
    }


def test_domain_safety_cases_do_not_top_forbidden_domain(matcher):
    id_to_index = {q["id"]: i for i, q in enumerate(matcher.questions)}
    failures = []
    for case in DOMAIN_SAFETY_CASES:
        ranked = matcher.rank(case["context"], top_n=3)
        if not ranked:
            assert case["allow_empty"], f"{case['label']} 不应为空"
            continue
        top = ranked[0]
        strong = matcher._domain_profiles[id_to_index[top["id"]]][0]
        forbidden = strong & case["forbidden_domains"]
        if forbidden:
            failures.append((case["label"], top["id"], sorted(forbidden)))
    assert failures == [], f"跨领域 top1: {failures}"


def test_representative_cross_domain_queries_rank_expected_domain(matcher):
    expected = {
        "Java synchronized 的偏向锁、轻量级锁和重量级锁是怎样升级的": "bank-1052",
        "MySQL 可重复读隔离级别下，共享锁和排他锁如何阻塞彼此": "bank-1749",
        "解释 BFC 的布局规则，以及为什么它会影响外边距折叠": "bank-5092",
    }
    for context, top_id in expected.items():
        ranked = matcher.rank(context, top_n=3)
        assert ranked and ranked[0]["id"] == top_id, (context, ranked[:3])
