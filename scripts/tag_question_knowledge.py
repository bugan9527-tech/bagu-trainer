# -*- coding: utf-8 -*-
"""Tag every main-bank question with deterministic knowledge points."""
import collections
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from bagu_trainer.knowledge_taxonomy import classify_question  # noqa: E402


BANK = PROJECT / "data" / "questions.json"
REPORT = PROJECT / "data" / "knowledge_tag_report.json"
CSV_REPORT = PROJECT / "data" / "knowledge_tag_report.csv"


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(
        f"questions_backup_pre_knowledge_tags_{stamp}{path.suffix}")
    payload = path.read_bytes()
    target.write_bytes(payload)
    assert hashlib.sha256(target.read_bytes()).digest() == \
        hashlib.sha256(payload).digest()
    return target


def _atomic_json(path: Path, value) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(temp, path)


def main():
    questions = json.loads(BANK.read_text(encoding="utf-8"))
    backup = _backup(BANK)
    domain_counts = collections.Counter()
    tag_counts = collections.Counter()
    source_counts = collections.Counter()

    for q in questions:
        labels = classify_question(q)
        q.update(labels)
        domain_counts[labels["knowledge_domain"]] += 1
        source_counts[labels["knowledge_tag_source"]] += 1
        tag_counts.update(labels["knowledge_tags"])

    unlabeled = [q["id"] for q in questions if not q.get("knowledge_tags")]
    assert not unlabeled, f"仍有未标记题目: {unlabeled[:10]}"

    _atomic_json(BANK, questions)
    report = {
        "total": len(questions),
        "labeled": len(questions) - len(unlabeled),
        "unlabeled": len(unlabeled),
        "domains": dict(domain_counts.most_common()),
        "tags": dict(tag_counts.most_common()),
        "tag_sources": dict(source_counts.most_common()),
        "backup": str(backup),
    }
    _atomic_json(REPORT, report)

    with CSV_REPORT.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "id", "question", "repository", "knowledge_domain",
            "primary_knowledge_tag", "knowledge_tags", "tag_source",
        ])
        for q in questions:
            tags = q.get("tags") or []
            writer.writerow([
                q["id"], q["question"], tags[-1] if len(tags) > 1 else "",
                q.get("knowledge_domain", ""),
                q.get("primary_knowledge_tag", ""),
                " | ".join(q.get("knowledge_tags") or []),
                q.get("knowledge_tag_source", ""),
            ])

    print("[backup]", backup)
    print("[tagged]", len(questions), "unlabeled=", len(unlabeled))
    print("[domains]")
    for name, count in domain_counts.most_common():
        print(f"  {name:12} {count:4}")
    print("[source]", dict(source_counts))
    print("[report]", REPORT)
    print("[csv]", CSV_REPORT)
    return report


if __name__ == "__main__":
    main()
