"""Local grounding checks and strict parsing of the separate model review."""
import hashlib
import json
import re
from dataclasses import dataclass


class QuizValidationError(ValueError):
    """Messages are fixed diagnostics, never model text or project snippets."""

    def __init__(self, message, *, failure_kind="quality"):
        super().__init__(message)
        self.failure_kind = "quality" if failure_kind == "quality" else "service"


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


def admission_failure(quiz, *, require_review=False):
    """AI-only admission for fresh questions and persisted review snapshots."""
    if quiz.get("available") is False:
        return quiz.get("unavailable_reason") or "题目生成失败"
    if quiz.get("generated_by") != "llm":
        return "仅允许展示 AI 生成的题目；离线选择题及来源不明的旧题不再展示。"
    if require_review and not has_passed_review(quiz):
        return "新题缺少通过的上下文关联性审核记录，请重试生成。"
    return None


def quiz_deadline(now, ttl_seconds):
    """Use a finite persisted deadline when expiration is disabled."""
    return now + ttl_seconds if ttl_seconds > 0 else 253402300799.0


def is_expired(deadline, *, now, ttl_seconds):
    return ttl_seconds > 0 and deadline <= now


def generation_eligible(*, now, next_review_at=None, pending=False,
                        reserved=False, fresh=True, explicit_retry=False):
    """Explicit retries bypass a seen fingerprint, never a reservation or cooldown."""
    return not (reserved or (not fresh and not explicit_retry) or pending
                or (next_review_at or 0) > now)


QUESTION_MIN_LEN = 5
QUESTION_MAX_LEN = 500


@dataclass(frozen=True)
class QuizPolicy:
    question_min_len: int = QUESTION_MIN_LEN
    question_max_len: int = QUESTION_MAX_LEN


DEFAULT_QUIZ_POLICY = QuizPolicy()


def parse_quiz(content, *, policy=DEFAULT_QUIZ_POLICY):
    """从 LLM 输出中提取 JSON 并严格校验，任一不合法返回 None。

    校验项：question/options/answer/analysis 字段齐全；选项恰为 A/B/C/D
    四键且全部非空、互不相同；答案必须是 A-D；解析非空；题目长度合理。
    """
    if not isinstance(content, str):
        return None
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    try:
        d = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(d, dict):
        return None
    if not all(k in d for k in ("question", "options", "answer", "analysis")):
        return None
    opts = d["options"]
    if not isinstance(opts, dict) or sorted(opts.keys()) != ["A", "B", "C", "D"]:
        return None
    vals = []
    for k in ("A", "B", "C", "D"):
        v = opts.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        vals.append(v.strip())
    if len(set(vals)) != 4:
        return None
    if not isinstance(d["answer"], str) or d["answer"].strip().upper() not in ("A", "B", "C", "D"):
        return None
    analysis = d["analysis"]
    if not isinstance(analysis, str) or not analysis.strip():
        return None
    question = d["question"]
    if not isinstance(question, str):
        return None
    question = question.strip()
    if not (policy.question_min_len <= len(question) <= policy.question_max_len):
        return None
    return {
        "question": question,
        "options": dict(zip(("A", "B", "C", "D"), vals)),
        "answer": d["answer"].strip().upper(),
        "analysis": analysis.strip(),
    }


def repractice_failure(snapshot, *, policy=DEFAULT_QUIZ_POLICY):
    failure = admission_failure(snapshot)
    if failure:
        return failure
    if not isinstance(snapshot.get("question"), str) or not snapshot["question"].strip():
        return "旧快照缺少完整题干，无法重练。"
    options = snapshot.get("options")
    if not isinstance(options, dict) or any(not isinstance(options.get(k), str) or not options[k].strip() for k in "ABCD"):
        return "旧快照缺少完整的四个选项，无法重练。"
    if snapshot.get("answer") not in tuple("ABCD"):
        return "旧快照缺少有效的正确答案，无法重练。"
    if not isinstance(snapshot.get("analysis"), str) or not snapshot["analysis"].strip():
        return "旧快照缺少解析，无法重练。"
    if parse_quiz(json.dumps(snapshot, ensure_ascii=False), policy=policy) is None:
        return "旧快照未通过现有题目校验（题干长度、四个不同选项或答案格式），无法重练。"
    return None
