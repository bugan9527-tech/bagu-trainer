# -*- coding: utf-8 -*-
"""概念档案（concept profile）的加载与校验。

data/question_concepts.json 由 scripts/build_concept_profiles.py 离线确定性生成，
以题目 ID 为键，值为：
  concept_terms      3-10 个关键概念词（来自标签与同现句，供检索索引）
  concept_summary    <=300 字符的场景无关概念摘要（不落完整答案）
  concept_source     title/tags/answer/analysis/source_excerpt 的组合
  concept_confidence high / medium / low

该模块只做加载与校验，不含任何 LLM 调用；文件缺失或损坏时降级为空档案，
检索自动退化为「题干 + 标签」模式（Matcher 内标签本就参与索引）。
"""
import json
from functools import lru_cache
from pathlib import Path

MIN_TERMS = 3
MAX_TERMS = 10
MAX_SUMMARY_CHARS = 300


def load_concept_profiles(path=None) -> dict:
    """按题目 ID 加载概念档案；任何失败都返回空 dict（调用方无需 try）。

    进程内缓存：同一个进程里 Matcher 多次初始化 / 多个 Matcher 实例
    共享一次 JSON 解析（5443 份档案约数 MB，重复解析会拖慢构建）。
    """
    from bagu_trainer.infrastructure.bank_storage import active_bank_dir
    path = Path(path) if path is not None else active_bank_dir() / "question_concepts.json"
    return _load_concept_profiles(str(path))


@lru_cache(maxsize=4)
def _load_concept_profiles(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = data.get("profiles") or {}
    return {str(qid): p for qid, p in profiles.items() if isinstance(p, dict)}


load_concept_profiles.cache_clear = _load_concept_profiles.cache_clear
load_concept_profiles.cache_info = _load_concept_profiles.cache_info


def validate_profiles(question_ids, profiles) -> tuple:
    """校验概念档案与题库的对应关系与字段形状。

    返回 (stats, errors)：stats 含覆盖率与 confidence 分布；
    errors 为字段不合法的题目 ID 说明（不抛异常，供脚本写报告）。
    """
    ids = [str(i) for i in question_ids]
    stats = {
        "total_questions": len(ids),
        "covered": 0,
        "missing_ids_count": 0,
        "confidence": {"high": 0, "medium": 0, "low": 0,
                       "invalid": 0},
        "term_count_below_min": 0,
        "term_count_above_max": 0,
        "summary_over_limit": 0,
        "extra_ids_count": 0,
        "invalid_terms_count": 0,
    }
    errors = []
    id_set = set(ids)
    for qid in ids:
        prof = profiles.get(qid)
        if not prof:
            stats["missing_ids_count"] += 1
            errors.append(f"{qid}: 缺失概念档案")
            continue
        stats["covered"] += 1
        conf = prof.get("concept_confidence")
        if conf in ("high", "medium", "low"):
            stats["confidence"][conf] += 1
        else:
            stats["confidence"]["invalid"] += 1
            errors.append(f"{qid}: confidence 非法 {conf!r}")
        terms = prof.get("concept_terms") or []
        if not isinstance(terms, list):
            stats["invalid_terms_count"] += 1
            errors.append(f"{qid}: concept_terms 不是列表")
            terms = []
        elif any(not isinstance(t, str) or not t.strip() for t in terms):
            stats["invalid_terms_count"] += 1
            errors.append(f"{qid}: concept_terms 含空值/非字符串")
        if len(terms) < MIN_TERMS:
            stats["term_count_below_min"] += 1
            errors.append(
                f"{qid}: concept_terms={len(terms)}，少于 {MIN_TERMS}"
            )
        if len(terms) > MAX_TERMS:
            stats["term_count_above_max"] += 1
            errors.append(
                f"{qid}: concept_terms={len(terms)}，多于 {MAX_TERMS}"
            )
        summary = prof.get("concept_summary") or ""
        if not isinstance(summary, str):
            errors.append(f"{qid}: concept_summary 不是字符串")
            summary = ""
        if len(summary) > MAX_SUMMARY_CHARS:
            stats["summary_over_limit"] += 1
            errors.append(
                f"{qid}: concept_summary={len(summary)}，超过 "
                f"{MAX_SUMMARY_CHARS}"
            )
    extra_ids = sorted(set(str(i) for i in profiles) - id_set)
    stats["extra_ids_count"] = len(extra_ids)
    if extra_ids:
        errors.append(f"存在多余档案 ID: {extra_ids[:10]}")
    return stats, errors
