# -*- coding: utf-8 -*-
"""判分模块：校验用户答案，返回对错与解析"""


def _normalize(answer) -> str:
    """把 "A" / "a" / "A." / "选项A" / "选A" / 完整选项文本 归一为字母"""
    if answer is None:
        return ""
    a = str(answer).strip().upper()
    if a in "ABCD":  # 可能直接传了选项文本，取首字母兜底
        return a
    a = a.replace("选项", "").replace("选", "").strip(" .。")
    return a[0] if a and a[0] in "ABCD" else ""


class Scorer:
    def grade(self, quiz: dict, user_answer: str) -> dict:
        norm = _normalize(user_answer)
        return {
            "correct": bool(norm) and norm == quiz.get("answer"),
            "your_answer": user_answer,
            "normalized_answer": norm or "(无法识别)",
            "correct_answer": quiz.get("answer"),
            "knowledge_point": quiz.get("kp_name"),
            "analysis": quiz.get("analysis", ""),
        }