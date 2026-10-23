"""Local grounding checks and strict parsing of the separate model review."""
import hashlib
import json
import re


class QuizValidationError(ValueError):
    """Messages are fixed diagnostics, never model text or project snippets."""


def parse_object(content):
    if not isinstance(content, str):
        raise QuizValidationError("AI 返回内容不是有效 JSON 对象。")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("non-finite JSON constant")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, TypeError):
        raise QuizValidationError("AI 返回内容不是有效 JSON 对象或存在重复字段。") from None
    if not isinstance(value, dict):
        raise QuizValidationError("AI 返回内容不是有效 JSON 对象。")
    return value


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def validate_grounding(value, context, source):
    if not isinstance(value, dict):
        raise QuizValidationError("缺少上下文关联证据，请重试生成。")
    result = {}
    for field, material, minimum, maximum, label in (
            ("context_quote", context, 2, 256, "上下文"),
            ("source_quote", source, 12, 500, "来源正文")):
        quote = value.get(field)
        if not isinstance(quote, str) or not minimum <= len(quote.strip()) <= maximum:
            raise QuizValidationError(f"{label}引用缺失或长度不合法，请重试生成。")
        quote = _normalize(quote)
        if not minimum <= len(quote) <= maximum:
            raise QuizValidationError(f"{label}引用的有效长度不合法，请重试生成。")
        if not any(c.isalnum() for c in quote) or quote not in _normalize(material):
            raise QuizValidationError(f"{label}引用与实际发送的材料不符，请重试生成。")
        if "[REDACTED]" in quote:
            raise QuizValidationError(f"{label}引用包含脱敏占位内容，请改选有效片段。")
        result[field] = quote
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not 12 <= len(rationale.strip()) <= 600:
        raise QuizValidationError("缺少具体的场景与考点关联说明，请重试生成。")
    result["rationale"] = rationale.strip()
    return result


def validate_review(value, answer):
    if not isinstance(value, dict):
        raise QuizValidationError("关联性审核未返回有效结论，请重试。")
    for field, label in (
            ("context_relevant", "题目与上下文的关联"),
            ("source_supported", "来源正文对结论的支持"),
            ("answer_unique", "正确答案的唯一性"),
            ("no_answer_leak", "题干与选项中的答案泄露")):
        if type(value.get(field)) is not bool:
            raise QuizValidationError("关联性审核字段不完整或类型不合法，请重试。")
        if not value[field]:
            raise QuizValidationError(f"关联性审核未通过：{label}，请重试生成。")
    correct = value.get("correct_answer")
    if correct not in ("A", "B", "C", "D") or correct != answer:
        raise QuizValidationError("关联性审核未通过：审核答案与生成答案不一致。")
    reason = value.get("reason")
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 600:
        raise QuizValidationError("关联性审核缺少有效说明，请重试。")
    return {field: value[field] for field in
            ("context_relevant", "source_supported", "answer_unique", "no_answer_leak", "correct_answer", "reason")}


def grounding_receipt(evidence, review, context, source, model):
    return {
        "version": 1, **evidence, "review": review, "review_model": model,
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def has_passed_review(quiz):
    receipt = quiz.get("grounding")
    if (not isinstance(receipt, dict) or type(receipt.get("version")) is not int
            or receipt["version"] != 1):
        return False
    if any(not isinstance(receipt.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[field])
           for field in ("context_sha256", "source_sha256")):
        return False
    try:
        validate_review(receipt.get("review"), quiz.get("answer"))
    except QuizValidationError:
        return False
    return True
