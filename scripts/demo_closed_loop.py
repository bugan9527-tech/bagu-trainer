# -*- coding: utf-8 -*-
"""演示最小闭环：AI 活动 -> 匹配命中知识点 -> 生成情景题 -> 作答 -> 判分解析。

用法:
  python scripts/demo_closed_loop.py          # 默认用 data/mock 数据（可离线跑通）
  python scripts/demo_closed_loop.py --real   # 用本机真实会话记录 + 本目录文件变动
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bagu_trainer.config import PROJECT_ROOT as ROOT, load_config  # noqa: E402
from bagu_trainer.diff_reader import DiffReader                          # noqa: E402
from bagu_trainer.knowledge_base import KnowledgeBase                    # noqa: E402
from bagu_trainer.matcher import Matcher                                 # noqa: E402
from bagu_trainer.quiz_generator import QuizGenerator                    # noqa: E402
from bagu_trainer.scorer import Scorer                                   # noqa: E402
from bagu_trainer.session_reader import (                                # noqa: E402
    entries_to_text, find_trae_home, read_recent_entries)


def collect_mock():
    mock_dir = ROOT / "data" / "mock"
    entries = []
    for line in (mock_dir / "mock_session.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        entries.append({"intent": d["intent"], "actions": d["actions"],
                        "outcome": d["outcome"], "learned": d["learned"]})
    diff_text = (mock_dir / "mock_diff.txt").read_text(encoding="utf-8")
    return entries_to_text(entries), diff_text, "mock 会话记录 + mock diff"


def collect_real(cfg):
    trae_home = find_trae_home(cfg.get("trae_home"))
    entries = read_recent_entries(trae_home, since_minutes=cfg["session_recent_minutes"]) if trae_home else []
    changes = DiffReader(str(ROOT)).changes()
    return entries_to_text(entries), changes["diff_text"], \
        f"真实会话记录({len(entries)} 条) + 本目录文件变动[{changes['source']}]"


def run(context, diff_text, source_note):
    print("=" * 60)
    print(f"[1] 信号源: {source_note}")
    print(f"    上下文长度: {len(context) + len(diff_text)} 字符")

    print("=" * 60)
    print("[2] 匹配知识点")
    cfg = load_config()
    matches = Matcher(KnowledgeBase()).match(context + "\n" + diff_text,
                                             top_n=cfg["max_quizzes_per_hit"])
    if not matches:
        print("    未命中任何知识点（--real 模式下属正常：近期活动未覆盖题库范围）")
        if source_note.startswith("mock"):
            sys.exit(1)
        return
    for q, score, hits in matches:
        print(f"    - {q['knowledge_point']}  (score={score}, 命中: {hits})")

    print("=" * 60)
    print("[3] 生成情景题")
    quiz = QuizGenerator(cfg).generate(matches[0][0], context + "\n" + diff_text)
    print(f"    出题方式: {quiz['generated_by']}"
          + (f"  |  回退原因: {quiz['fallback_reason']}" if quiz.get("fallback_reason") else ""))
    print(f"    {quiz['question']}")
    for k, v in quiz["options"].items():
        print(f"      {k}. {v}")

    print("=" * 60)
    print("[4] 判分解析")
    scorer = Scorer()
    for answer in ("A", quiz["answer"]):
        r = scorer.grade(quiz, answer)
        mark = "正确" if r["correct"] else "错误"
        extra = f" | 解析: {r['analysis']}" if not r["correct"] else ""
        print(f"    用户答 {answer} -> {mark} (正确答案 {r['correct_answer']}){extra}")
    print("=" * 60)
    print("闭环 OK: 活动报告 -> 命中 -> 出题 -> 判分解析 全部跑通")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实会话记录与本地文件变动")
    args = ap.parse_args()
    if args.real:
        ctx, diff, note = collect_real(load_config())
    else:
        ctx, diff, note = collect_mock()
    if not ctx.strip() and not diff.strip():
        print("无可用信号（--real 模式下本机暂无近期活动记录/文件变动），退出")
        sys.exit(1)
    run(ctx, diff, note)