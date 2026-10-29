# -*- coding: utf-8 -*-
"""matcher 评测脚本：分别输出 development 与 held-out 的基线/当前对比。

用法:
  python scripts/eval_matcher.py              # 默认阈值跑对比
  python scripts/eval_matcher.py --conf 0.2 --margin 0.06   # 调阈值看新 matcher

指标:
  top1 / top3:   正向案例 top1 或 top3 命中 accept 集合的比例
  负向误触发率:  负向案例返回非空匹配的比例（期望 0）

注意：阈值调参只应看 development。held-out 是固定验收集，不参与参数选择。
"""
import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase  # noqa: E402
from bagu_trainer.infrastructure.matcher import Matcher
from bagu_trainer.infrastructure.matcher import _domain_profile  # noqa: E402
from eval_cases import POSITIVE_CASES, NEGATIVE_CASES  # noqa: E402
from eval_heldout import (  # noqa: E402
    DOMAIN_SAFETY_CASES,
    NEGATIVE_CASES_HELDOUT,
    POSITIVE_CASES_HELDOUT,
)

KB = KnowledgeBase()


# ---- 旧算法参考实现（子串计数基线，与重构前 matcher.py 逐行等价）----
class BaselineMatcher:
    """旧 matcher：关键词出现次数 + 标签出现 +2，min_score=1"""

    def __init__(self, knowledge_base):
        self.kb = knowledge_base

    def match(self, text, top_n=3, min_score=1):
        text = (text or "").lower()
        scored = []
        for q in self.kb.all():
            score, hits = 0, []
            for kw in q.get("keywords", []):
                n = text.count(kw.lower())
                if n:
                    score += n
                    hits.append(kw)
            for tag in q.get("tags", []):
                if tag.lower() in text:
                    score += 2
                    hits.append(f"tag:{tag}")
            if score >= min_score:
                scored.append((q, score, hits))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]


def evaluate(matcher, positives, negatives, top_n=3):
    """返回分组指标、正向失败和负向误触发明细。"""
    pos_rows, neg_rows = [], []
    for case in positives:
        out = matcher.match(case["context"], top_n=top_n)
        ids = [q["id"] for q, _s, _h in out]
        top1 = bool(ids) and ids[0] in case["accept"]
        top3 = bool(set(ids) & case["accept"])
        pos_rows.append((case["label"], ids[:3], top1, top3,
                         case["context"][:42]))
    for case in negatives:
        out = matcher.match(case["context"], top_n=top_n)
        ids = [q["id"] for q, _s, _h in out]
        neg_rows.append((case["label"], ids[:3], len(out) > 0,
                         case.get("category", "")))
    top1 = sum(r[2] for r in pos_rows) / max(1, len(pos_rows))
    top3 = sum(r[3] for r in pos_rows) / max(1, len(pos_rows))
    ftr = sum(r[2] for r in neg_rows) / max(1, len(neg_rows))
    return {
        "top1": top1,
        "top3": top3,
        "ftr": ftr,
        "positive_rows": pos_rows,
        "negative_rows": neg_rows,
    }


def show_row(name, ids):
    return ", ".join(ids) if ids else "(空)"


def domain_errors(matcher, cases, top_n=3):
    """找出带 forbidden_domains 元数据、但 top1 命中异域强信号的样本。"""
    rows = []
    for case in cases:
        out = matcher.rank(case["context"], top_n=top_n)
        if not out:
            continue
        top = out[0]
        q = top["question"]
        strong, _all_domains = _domain_profile(
            (q.get("knowledge_point") or "") + " " + (q.get("question") or "")
        )
        wrong = sorted(strong & set(case.get("forbidden_domains", ())))
        if wrong:
            rows.append((case["label"], top["id"],
                         q.get("knowledge_point", ""), wrong))
    return rows


def print_group(label, baseline, current, positives, negatives):
    print("=" * 88)
    print(f"{label}: 正向 {len(positives)} 例 | 负向 {len(negatives)} 例")
    print("-" * 88)
    print(f"{'指标':<16}{'旧子串计数基线':<16}{'当前 Matcher':<16}")
    print(f"{'top1 命中率':<16}{baseline['top1']:<16.3f}{current['top1']:<16.3f}")
    print(f"{'top3 命中率':<16}{baseline['top3']:<16.3f}{current['top3']:<16.3f}")
    print(f"{'负向误触发率':<16}{baseline['ftr']:<16.3f}{current['ftr']:<16.3f}")
    print("-" * 88)

    positives_missed = [r for r in current["positive_rows"] if not r[2]]
    print("当前 top1 未命中正向案例:")
    if not positives_missed:
        print("  (无)")
    for item in positives_missed:
        case_label, ids, _top1, _top3, context = item
        print(f"  x {case_label}: 输出 {show_row(case_label, ids)} | 上下文: {context}...")

    triggered = [r for r in current["negative_rows"] if r[2]]
    print("当前负向误触发案例:")
    if not triggered:
        print("  (无)")
    for case_label, ids, _triggered, category in triggered:
        print(f"  x [{category}] {case_label}: 输出 {show_row(case_label, ids)}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", type=float, default=None,
                    help="覆盖新 matcher 的 conf_min（默认用内置值）")
    ap.add_argument("--margin", type=float, default=None,
                    help="覆盖新 matcher 的 margin_min（默认用内置值）")
    args = ap.parse_args()

    base = BaselineMatcher(KB)
    dev_base = evaluate(base, POSITIVE_CASES, NEGATIVE_CASES)
    heldout_base = evaluate(
        base, POSITIVE_CASES_HELDOUT, NEGATIVE_CASES_HELDOUT,
    )

    mkw = {}
    if args.conf is not None:
        mkw["conf_min"] = args.conf
    if args.margin is not None:
        mkw["margin_min"] = args.margin
    new = Matcher(KB, **mkw)
    dev_current = evaluate(new, POSITIVE_CASES, NEGATIVE_CASES)
    heldout_current = evaluate(
        new, POSITIVE_CASES_HELDOUT, NEGATIVE_CASES_HELDOUT,
    )

    print(f"题库: {len(KB.all())} 题")
    print_group("DEVELOPMENT", dev_base, dev_current,
                POSITIVE_CASES, NEGATIVE_CASES)
    print_group("HELD-OUT", heldout_base, heldout_current,
                POSITIVE_CASES_HELDOUT, NEGATIVE_CASES_HELDOUT)

    error_rows = domain_errors(
        new, POSITIVE_CASES_HELDOUT + DOMAIN_SAFETY_CASES,
    )
    print("=" * 88)
    print(f"HELD-OUT 跨领域 top1 错误: {len(error_rows)} 例")
    if not error_rows:
        print("  (无)")
    for case_label, qid, knowledge_point, wrong in error_rows:
        print(f"  x {case_label}: {qid} [{','.join(wrong)}] {knowledge_point}")

    print("-" * 88)
    print("领域安全案例结果（允许空匹配的案例按约定计为安全）:")
    id_to_domain = {}
    for case in DOMAIN_SAFETY_CASES:
        out = new.rank(case["context"], top_n=3)
        ids = [x["id"] for x in out]
        top_id = ids[0] if ids else None
        if top_id:
            q = out[0]["question"]
            id_to_domain[top_id] = _domain_profile(
                (q.get("knowledge_point") or "") + " " + (q.get("question") or "")
            )[0]
            wrong = sorted(id_to_domain[top_id] & case["forbidden_domains"])
        else:
            wrong = []
        accepted = bool(top_id and top_id in case["accept"])
        safe = (not wrong) and (accepted or (not top_id and case["allow_empty"]))
        narrow_miss = bool(top_id and not accepted and not wrong)
        status = "OK" if safe else ("DOMAIN-SAFE" if narrow_miss else "FAIL")
        print(f"  {status} {case['label']}: "
              f"{show_row(case['label'], ids)}")
    print("=" * 88)


if __name__ == "__main__":
    main()
