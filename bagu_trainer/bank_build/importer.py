# -*- coding: utf-8 -*-
"""
把 interview-banks/deduped/questions_export.json（15 仓库扒取去重后的问答题）
导入训练器题库 data/questions.json：
  - 先备份现有题库并校验备份数量（data/questions_backup_pre_source_excerpt.json）
  - 保留每条 source_paths，并用 bagu_trainer.source_extractor 从
    interview-banks/repos 下的真实 Markdown 提取对应标题下的正文片段，
    保存为有长度上限的 source_excerpt（找不到标记 missing，不伪造内容）
  - 每条生成 keywords（英文词 + 中文 2/3-gram），保证 matcher 能命中
  - 导入前清洗标题并拒绝 GitHub 评论、纯链接、公众号元信息、泛化短语和代码残留；
    拒绝明细写入 data/title_cleanup_report.json，避免静默丢题
  - 导入结束输出「前后数量 / 空值率 / 来源缺失率 / 已验证题数」报告
用法: python scripts/import_questions.py
"""
import collections
import functools
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from bagu_trainer.config import USER_DATA_ROOT

PROJECT = USER_DATA_ROOT
SRC = PROJECT / "interview-banks" / "deduped" / "questions_export.json"
REPOS = PROJECT / "interview-banks" / "repos"
BANK = PROJECT / "data" / "questions.json"
BACKUP = PROJECT / "data" / "questions_backup_pre_source_excerpt.json"

from bagu_trainer.fallback_bank import fallback_stats  # noqa: E402
from bagu_trainer.source_extractor import (  # noqa: E402
    assess_excerpt,
    excerpt_for_detailed,
)
from bagu_trainer.title_quality import clean_title  # noqa: E402

STOP_EN = {
    "the", "and", "you", "your", "how", "what", "why", "for", "are", "its",
    "our", "that", "this", "have", "has", "will", "can", "not", "was",
    "with", "from", "use", "using", "does", "when", "where", "which",
    "who", "into", "about", "than", "then", "them", "they", "here",
    "there", "also", "any", "get", "let", "make", "most", "some", "more",
    "out", "all", "one", "two", "way", "new", "per", "did", "do", "is",
    "it", "of", "to", "in", "on", "a", "an", "or", "if", "as", "at",
    "by", "be", "no", "so", "up", "we", "he", "she", "i",
}
NUM_KEYWORDS_KEEP = 60


def make_keywords(text: str) -> list:
    tl = text.lower()
    kws = []
    for w in re.findall(r"[a-z0-9]+", tl):
        if len(w) >= 2 and w not in STOP_EN and not w.isdigit():
            kws.append(w)
    zh_runs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    for seg in zh_runs:
        for i in range(len(seg) - 1):
            kws.append(seg[i:i + 2])
        for i in range(len(seg) - 2):
            kws.append(seg[i:i + 3])
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:NUM_KEYWORDS_KEEP]


def _is_verified(q: dict) -> bool:
    """已验证选择题：四选项齐备互异、A-D 答案、非占位解析。"""
    opts = q.get("options") or {}
    if sorted(opts.keys()) != ["A", "B", "C", "D"]:
        return False
    vals = [str(v).strip() for v in opts.values()]
    if any(not v for v in vals) or len(set(vals)) != 4:
        return False
    if q.get("answer") not in ("A", "B", "C", "D"):
        return False
    analysis = (q.get("analysis") or "").strip()
    return bool(analysis) and not analysis.startswith("来源:")


def bank_stats(bank: list) -> dict:
    """题库健康度统计：命中、有效正文、低质量片段与已验证回退题。"""
    total = len(bank)
    fallback = fallback_stats()
    if not total:
        return {"total": 0, "options_empty": 0, "answer_empty": 0,
                "source_missing": 0, "verified": 0, "source_hit": 0,
                "source_hit_rate": 0.0, "usable_excerpts": 0,
                "usable_excerpt_rate": 0.0, "low_quality_excerpts": 0,
                "verified_fallback": fallback["valid"]}
    options_empty = sum(1 for q in bank if sorted(q.get("options") or {}) != ["A", "B", "C", "D"])
    answer_empty = sum(1 for q in bank if not q.get("answer"))
    source_missing = sum(1 for q in bank if q.get("source_status") != "ok")
    source_hit = total - source_missing
    usable = sum(1 for q in bank if _record_usable(q))
    low_quality = sum(
        1 for q in bank
        if q.get("source_status") == "ok" and not _record_usable(q)
    )
    verified = sum(1 for q in bank if _is_verified(q))
    return {
        "total": total,
        "options_empty": options_empty,
        "options_empty_rate": round(100.0 * options_empty / total, 2),
        "answer_empty": answer_empty,
        "answer_empty_rate": round(100.0 * answer_empty / total, 2),
        "source_missing": source_missing,
        "source_missing_rate": round(100.0 * source_missing / total, 2),
        "source_hit": source_hit,
        "source_hit_rate": round(100.0 * source_hit / total, 2),
        "usable_excerpts": usable,
        "usable_excerpt_rate": round(100.0 * usable / total, 2),
        "low_quality_excerpts": low_quality,
        "verified": verified,
        "verified_fallback": fallback["valid"],
    }


def _record_usable(q: dict) -> bool:
    """兼容旧题库：没有 usable_excerpt 字段时按正文质量现场判定。"""
    if "usable_excerpt" in q:
        return bool(q.get("usable_excerpt"))
    if q.get("excerpt_quality") in ("weak", "empty"):
        return False
    return assess_excerpt(q.get("source_excerpt") or "")["usable"]


def build_bank(export: list, repos_root, excerpt_limit: int, *,
               checkpoint=lambda: None, stable_ids=False) -> tuple:
    """从 export 构建题库；返回 (bank, 逐条提取统计)。"""
    bank = []
    stat = {"ok": 0, "missing": 0, "none": 0, "skipped_blank": 0,
            "usable": 0, "weak": 0, "exact": 0, "parent": 0,
            "title_rejected": 0, "title_cleanup": []}
    excerpt_for_cached = functools.lru_cache(maxsize=None)(
        lambda paths, title: excerpt_for_detailed(
            str(repos_root), paths, title, excerpt_limit))
    for i, e in enumerate(export, 1):
        checkpoint()
        raw_text = (e.get("question") or "").strip()
        if not raw_text:
            stat["skipped_blank"] += 1
            continue
        decision = clean_title(raw_text)
        if decision["rejected"]:
            stat["title_rejected"] += 1
            stat["title_cleanup"].append({
                "id": "bank-%04d" % i,
                "original": decision["original"][:500],
                "cleaned": decision["cleaned"][:500],
                "reason": decision["reason"],
                "source_paths": list(e.get("source_paths") or [])[:20],
            })
            continue
        text = decision["cleaned"]
        repo = (e.get("tags") or ["unknown"])[-1]
        source_paths = list(e.get("source_paths") or [])
        detail = excerpt_for_cached(tuple(source_paths), text)
        excerpt, status, used = (
            detail["excerpt"], detail["status"], detail["used"])
        stat[status] += 1
        if status == "ok":
            stat["usable" if detail["usable"] else "weak"] += 1
            if detail["match"] in ("exact", "body_parent", "parent"):
                stat["exact" if detail["match"] == "exact" else "parent"] += 1
        identity = "\n".join([repo, *sorted(source_paths), text])
        question_id = ("source-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                       if stable_ids else "bank-%04d" % i)
        bank.append({
            "id": question_id,
            "knowledge_point": text.rstrip("?？。."),
            "tags": ["八股", repo],
            "keywords": make_keywords(text),
            "difficulty": "medium",
            "question": text,
            "options": {},
            "answer": "",
            "analysis": "来源: %s" % ", ".join(source_paths or [repo]),
            "source_paths": source_paths,
            "source_excerpt": excerpt,
            "source_status": status,
            "source_match": detail["match"],
            "excerpt_quality": detail["quality"],
            "usable_excerpt": bool(detail["usable"]),
            "excerpt_quality_reason": detail["reason"],
            "excerpt_source": used or "",
        })
    return bank, stat


def format_report(label: str, s: dict) -> str:
    return ("[%s] 题数=%d 来源命中率=%.2f%% 有效正文率=%.2f%% "
            "低质量片段=%d 来源缺失率=%.2f%% 已验证回退题=%d" % (
                label, s["total"], s["source_hit_rate"],
                s["usable_excerpt_rate"], s["low_quality_excerpts"],
                s["source_missing_rate"], s["verified_fallback"]))


def _json_payload(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _backup_bank(old: list, backup_path: Path) -> Path:
    """备份旧题库并校验数量/内容哈希；不覆盖已有备份。"""
    target = Path(backup_path)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = target.with_name(f"{target.stem}_{stamp}{target.suffix}")
        n = 1
        while target.exists():
            target = target.with_name(
                f"{Path(backup_path).stem}_{stamp}_{n}{Path(backup_path).suffix}")
            n += 1
    payload = _json_payload(old)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    restored = json.loads(target.read_text(encoding="utf-8"))
    assert len(restored) == len(old), "备份数量校验失败"
    assert hashlib.sha256(_json_payload(restored).encode("utf-8")).hexdigest() == \
        hashlib.sha256(payload.encode("utf-8")).hexdigest(), "备份内容校验失败"
    return target


def _write_bank_atomic(path: Path, bank: list) -> None:
    target = Path(path)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(_json_payload(bank), encoding="utf-8")
    os.replace(temp, target)


def main(src=SRC, repos=REPOS, bank_path=BANK, backup_path=BACKUP,
         excerpt_limit: int = 1500, title_report_path=None) -> dict:
    export = json.loads(Path(src).read_text(encoding="utf-8"))
    print("[src] questions_export.json entries = %d" % len(export))

    before = None
    bank_path = Path(bank_path)
    backup_used = None
    if bank_path.exists():
        old = json.loads(bank_path.read_text(encoding="utf-8"))
        backup_used = _backup_bank(old, Path(backup_path))
        before = bank_stats(old)
        print("[backup] %d 旧题 -> %s（数量与内容哈希已校验一致）"
              % (len(old), backup_used.name))
    else:
        print("[backup] 目标题库不存在，跳过备份（首次导入）")

    bank, stat = build_bank(export, repos, excerpt_limit)
    expected = len(export) - stat["skipped_blank"] - stat["title_rejected"]
    assert len(bank) == expected, "导入数量校验失败"
    if before is not None and len(bank) < int(before["total"] * 0.95):
        raise AssertionError("导入后题库数量减少超过 5%，已中止写入")
    _write_bank_atomic(bank_path, bank)
    written = len(json.loads(bank_path.read_text(encoding="utf-8")))
    assert written == len(bank), "写入数量校验失败"

    after = bank_stats(bank)
    n_kw = sum(len(q["keywords"]) for q in bank)
    report_path = Path(title_report_path) if title_report_path else (
        bank_path.parent / "title_cleanup_report.json")
    report = {
        "source_total": len(export),
        "kept_total": len(bank),
        "skipped_blank": stat["skipped_blank"],
        "title_rejected": stat["title_rejected"],
        "title_rejected_reasons": dict(sorted(collections.Counter(
            x["reason"] for x in stat["title_cleanup"]).items())),
        "removed": stat["title_cleanup"],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_json_payload(report), encoding="utf-8")
    print("[extract] 正文命中 ok=%d missing=%d none=%d 空题跳过=%d "
          "标题拒绝=%d 有效正文=%d 低质量=%d 精确/父章节=%d/%d"
          % (stat["ok"], stat["missing"], stat["none"], stat["skipped_blank"],
             stat["title_rejected"], stat["usable"], stat["weak"],
             stat["exact"], stat["parent"]))
    if before:
        print(format_report("导入前", before))
    print(format_report("导入后", after))
    print("[fallback] 独立人工验证题=%d（文件 data/fallback_quizzes.json）"
          % after["verified_fallback"])
    print("[write] data/questions.json = %d 题, 平均 keywords %.1f 个"
          % (len(bank), n_kw / max(1, len(bank))))
    print("[title] 标题清洗拒绝 %d 条，明细 -> %s"
          % (stat["title_rejected"], report_path))
    print("[done] 重启桌面训练器后生效")
    return {"before": before, "after": after, "extract_stat": stat,
            "backup": str(backup_used) if backup_used else None,
            "title_report": str(report_path)}


if __name__ == "__main__":
    main()
