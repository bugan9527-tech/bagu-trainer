# -*- coding: utf-8 -*-
"""独立的人工验证回退题库。

数据文件为 data/fallback_quizzes.json，与主题库解耦；只接受完整四选项、
合法答案、非空解析，并且必须带来源路径或明确的人工验证标记。运行时不调用
LLM，也不把 LLM 生成的题目自动标记成人工验证。
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT
from .source_extractor import normalize_title

FALLBACK_PATH = PROJECT_ROOT / "data" / "fallback_quizzes.json"
MIN_FALLBACK_QUIZZES = 20

_GENERIC_TERMS = {
    "什么", "是什么", "是什", "如何", "实现", "怎么", "为什么", "哪些",
    "是否", "可以", "使用", "作用", "区别", "的区别", "的区", "原理",
    "问题", "方案", "处理", "解决", "情况", "什么情况", "么情况",
    "通过", "分别", "流程", "特点", "机制", "技术", "方式", "方法",
}
_WEAK_TERMS = {
    "java", "mysql", "redis", "http", "https", "where", "order", "by",
    "select", "join", "limit", "group", "having", "orderby", "groupby",
    "状态码", "索引",
    "缓存", "数据库", "线程", "分布式",
}
_SPECIFIC_EN_ACRONYMS = {
    "rdb", "aof", "mvcc", "tcp", "udp", "kafka", "cap", "rest",
    "jvm", "gc", "404", "ip", "dns",
}


def validate_fallback_quiz(q) -> list:
    """返回单个回退题的校验错误列表；空列表表示通过。"""
    errors = []
    if not isinstance(q, dict):
        return ["题目必须是 JSON 对象"]
    for key in ("id", "knowledge_point", "question", "analysis"):
        if not isinstance(q.get(key), str) or not q[key].strip():
            errors.append(f"{key} 不能为空")
    opts = q.get("options")
    if not isinstance(opts, dict) or sorted(opts.keys()) != ["A", "B", "C", "D"]:
        errors.append("options 必须恰为 A/B/C/D 四个键")
    else:
        values = [str(opts[k]).strip() for k in "ABCD"]
        if any(not v for v in values):
            errors.append("选项不能为空")
        if len(set(values)) != 4:
            errors.append("四个选项必须互不相同")
    if q.get("answer") not in ("A", "B", "C", "D"):
        errors.append("answer 必须是 A-D")
    paths = q.get("source_paths")
    if paths is not None and not isinstance(paths, list):
        errors.append("source_paths 必须是列表")
    manual = q.get("manual_verified") is True
    if not manual and not paths:
        errors.append("必须提供 source_paths 或 manual_verified=true")
    if manual and not str(q.get("verification_note") or "").strip():
        errors.append("manual_verified=true 时必须提供 verification_note")
    provenance = str(q.get("generated_by") or "").lower()
    if "llm" in provenance and not manual:
        errors.append("LLM 生成题不得作为人工验证回退题")
    if len(str(q.get("question") or "").strip()) < 5:
        errors.append("题干过短")
    return errors


def _load_json(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("quizzes")
    if not isinstance(data, list):
        raise ValueError("fallback_quizzes.json 必须是题目数组或包含 quizzes 数组")
    return data


@lru_cache(maxsize=8)
def _load_fallback_cached(path_str: str) -> tuple:
    data = _load_json(Path(path_str))
    errors, out, seen = [], [], set()
    for i, raw in enumerate(data, 1):
        item_errors = validate_fallback_quiz(raw)
        if item_errors:
            errors.append(f"第 {i} 题: " + "；".join(item_errors))
            continue
        qid = raw["id"].strip()
        if qid in seen:
            errors.append(f"第 {i} 题: id {qid} 重复")
            continue
        seen.add(qid)
        q = dict(raw)
        q["id"] = qid
        q["knowledge_point"] = q["knowledge_point"].strip()
        q["question"] = q["question"].strip()
        q["analysis"] = q["analysis"].strip()
        q["options"] = {k: str(raw["options"][k]).strip() for k in "ABCD"}
        q["answer"] = raw["answer"]
        q["source_paths"] = [str(p) for p in (raw.get("source_paths") or [])]
        q["verified_fallback"] = True
        q["generated_by"] = "manual"
        out.append(q)
    if errors:
        raise ValueError("回退题库校验失败: " + " | ".join(errors[:5]))
    return tuple(out)


def load_fallback_quizzes(path=None) -> list:
    """加载并严格校验回退题库；校验失败抛 ValueError。"""
    target = Path(path or FALLBACK_PATH)
    if not target.is_file():
        return []
    return [dict(q) for q in _load_fallback_cached(str(target.resolve()))]


def offline_fallback_count(path=None) -> int:
    """返回当前可用（通过严格校验）的离线回退题数量。"""
    try:
        return len(load_fallback_quizzes(path))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def get_fallback_quiz(qid: str, path=None):
    """按稳定 id 取一道已校验回退题。"""
    for q in load_fallback_quizzes(path):
        if q["id"] == qid:
            return dict(q)
    return None


def _fallback_score(kp: dict, candidate: dict) -> float:
    kp_id = str(kp.get("id") or "")
    if kp_id and kp_id == candidate.get("id"):
        return 1000.0
    if kp_id and kp_id in (candidate.get("fallback_for") or []):
        return 900.0
    kp_name = normalize_title(kp.get("knowledge_point") or "")
    cand_name = normalize_title(candidate.get("knowledge_point") or "")
    kp_question = normalize_title(kp.get("question") or "")
    cand_question = normalize_title(candidate.get("question") or "")
    if kp_name and kp_name == cand_name:
        return 500.0
    if cand_name and (cand_name in kp_name or cand_name in kp_question):
        return 300.0 + min(50, len(cand_name))
    if kp_name and cand_name and kp_name in cand_name:
        return 220.0
    if cand_question and cand_question == kp_question:
        return 250.0
    # 只使用人工写明的主题词做匹配，避免「是什么」「区别」这类
    # 通用 n-gram 把不相关知识点误配到回退题。
    kp_text = " ".join([
        str(kp.get("knowledge_point") or ""), str(kp.get("question") or ""),
    ]).casefold()
    kp_norm = normalize_title(kp_text)
    strong_terms = {
        normalize_title(x) for x in (candidate.get("strong_match_terms") or [])
        if normalize_title(x)
    }
    matched = set()
    strong_matched = False
    for raw in (candidate.get("keywords") or []):
        term = str(raw).strip().casefold()
        if not term or term in _GENERIC_TERMS:
            continue
        norm = normalize_title(term)
        if len(norm) < 2:
            continue
        if re.fullmatch(r"[a-z0-9+#.]+", term):
            hit = bool(re.search(r"(?<![a-z0-9])" + re.escape(term)
                                 + r"(?![a-z0-9])", kp_text))
        else:
            hit = norm in kp_norm
        if hit:
            matched.add(norm)
            if norm in strong_terms:
                strong_matched = True
    topic_specific = any(
        (
            re.search(r"[\u4e00-\u9fff]", t)
            and len(t) >= 3 and t not in _WEAK_TERMS
        ) or (
            not re.search(r"[\u4e00-\u9fff]", t)
            and (len(t) >= 4 or t in _SPECIFIC_EN_ACRONYMS)
            and t not in _WEAK_TERMS
        )
        for t in matched
    )
    if len(matched) >= 2 and topic_specific:
        return 100.0 + 10.0 * min(20, len(matched))
    if strong_matched:
        return 80.0
    return 0.0


def find_fallback(kp_question: dict, path=None):
    """为命中的知识点选择最相关的人工验证题；无可靠相关题返回 None。"""
    try:
        candidates = load_fallback_quizzes(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    best, best_score = None, 0.0
    for candidate in candidates:
        score = _fallback_score(kp_question, candidate)
        if score > best_score:
            best, best_score = candidate, score
    return dict(best) if best is not None and best_score >= 80.0 else None


def fallback_stats(path=None) -> dict:
    """供导入报告/启动日志使用的回退题库统计。"""
    try:
        items = load_fallback_quizzes(path)
        return {"total": len(items), "valid": len(items), "errors": []}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"total": 0, "valid": 0, "errors": [str(exc)]}
