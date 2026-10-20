# -*- coding: utf-8 -*-
"""复习调度模块：SM-2 变体（确定性、可解释，不引入任何随机值）。

文档化公式（SM-2 核心思路的最小化变体）：
  答对（correct=True）：
      ease = min(MAX_EASE, ease + EASE_STEP)      # 难度系数上升（题目对用户"变简单"）
      streak += 1                                 # 连续答对次数
      interval = FIRST_INTERVAL_DAYS if streak <= 1 else interval * ease
          # 首次答对固定 1 天；连续答对时上一次间隔按 ease 倍数递增
  答错（correct=False）：
      ease = max(MIN_EASE, ease - EASE_PENALTY)   # 难度系数下降（题目对用户"变难"）
      streak = 0                                   # 连续答对中断
      interval = MIN_INTERVAL_DAYS                 # 短间隔（默认 6 小时）重新出现

  掌握度：mastery = correct_count / attempts       # 正确率，取值 [0,1]，从未作答按 0
  到期时刻：next_review_at = last_reviewed_at + interval * 86400 秒

所有时间戳为 epoch 秒（由调用方注入 now），本模块不读墙钟，
保证可用固定时间做确定性测试。
"""

MIN_INTERVAL_DAYS = 0.25      # 答错后 6 小时重新出现
FIRST_INTERVAL_DAYS = 1.0     # 首次答对后 1 天复习
EASE_DEFAULT = 2.5
MIN_EASE = 1.3
MAX_EASE = 2.9
EASE_STEP = 0.15
EASE_PENALTY = 0.2
SECONDS_PER_DAY = 86400.0

# 无历史时的初始 concept_state（与 store.concept_state 的列一一对应）
EMPTY_STATE = {
    "attempts": 0,
    "correct_count": 0,
    "mastery": 0.0,
    "ease": EASE_DEFAULT,
    "streak": 0,
    "interval_days": 0.0,
    "last_reviewed_at": None,
    "next_review_at": None,
}


def next_state(correct: bool, prev: dict, now: float) -> dict:
    """根据本次作答结果与历史状态，计算下一次复习的全部状态字段。

    prev 为上一次 concept_state（可为 None/{}，视为从未学过）；
    返回 dict 可直接 upsert 进 concept_state 表。
    """
    prev = prev or {}
    attempts = int(prev.get("attempts") or 0) + 1
    correct_count = int(prev.get("correct_count") or 0) + (1 if correct else 0)
    ease = float(prev.get("ease") or EASE_DEFAULT)
    streak = int(prev.get("streak") or 0)
    interval = float(prev.get("interval_days") or 0.0)
    if correct:
        ease = min(MAX_EASE, ease + EASE_STEP)
        streak += 1
        interval = FIRST_INTERVAL_DAYS if streak <= 1 else interval * ease
    else:
        ease = max(MIN_EASE, ease - EASE_PENALTY)
        streak = 0
        interval = MIN_INTERVAL_DAYS
    return {
        "attempts": attempts,
        "correct_count": correct_count,
        "mastery": round(correct_count / attempts, 4),
        "ease": round(ease, 4),
        "streak": streak,
        "interval_days": round(interval, 4),
        "last_reviewed_at": now,
        "next_review_at": round(now + interval * SECONDS_PER_DAY, 4),
    }