# -*- coding: utf-8 -*-
"""学习闭环集成测试：固定时间注入 + 临时 SQLite（conftest 注入 db_path）。
全 fake：无网络、无 LLM、无真实文件监听、无真实 .trae-cn、无用户隐私数据。
"""
import json

import bagu_trainer.server as server
from bagu_trainer.scheduler import (FIRST_INTERVAL_DAYS, MIN_INTERVAL_DAYS,
                                    next_state)

T0 = 1_800_000_000.0
HOUR = 3600.0
DAY = 86400.0


def _set_diff(diff, text):
    diff.results = [{"source": "git", "files": ["x.py"], "diff_text": text}]


def _go(monkeypatch, t):
    monkeypatch.setattr(server, "_now", lambda: t)


# ============ 调度器纯函数：确定性公式 ============
def test_scheduler_correct_grows_interval():
    s1 = next_state(True, None, T0)
    assert s1["interval_days"] == FIRST_INTERVAL_DAYS
    assert s1["next_review_at"] == T0 + DAY
    s2 = next_state(True, s1, T0 + DAY)
    # 连续答对：间隔 = 上次间隔 * 新 ease（2.5+0.15 后再 +0.15 = 2.8）
    assert s2["interval_days"] == round(FIRST_INTERVAL_DAYS * 2.8, 4)
    assert s2["next_review_at"] > s1["next_review_at"] + DAY  # 间隔递增
    assert s2["mastery"] == 1.0


def test_scheduler_wrong_short_due_then_reset():
    s1 = next_state(True, None, T0)
    s2 = next_state(False, s1, T0 + DAY)
    assert s2["interval_days"] == MIN_INTERVAL_DAYS
    # 答错后到期时刻（6 小时）远早于答对后的 1 天
    assert s2["next_review_at"] == T0 + DAY + MIN_INTERVAL_DAYS * DAY
    assert s2["mastery"] == 0.5
    s3 = next_state(True, s2, s2["next_review_at"])
    assert s3["interval_days"] == FIRST_INTERVAL_DAYS  # 错后重对：重新从 1 天起步
    assert abs(s3["ease"] - (2.5 + 0.15 - 0.2 + 0.15)) < 1e-9
    assert s3["mastery"] == round(2 / 3, 4)


# ============ 判分闭环：错题更早到期 -> 到期自动重练 ============
def test_wrong_answer_due_sooner_then_review_delivered(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    r = server.check_pending_quiz()
    assert r["has_pending"] is True and r["quiz"].get("review") is not True
    qid = r["quiz"]["quiz_id"]
    wrong = server.submit_answer(qid, "B")  # FakeGenerator 正确答案是 A
    assert wrong["ok"] is True and wrong["correct"] is False
    assert wrong["schedule"]["next_review_at"] == T0 + MIN_INTERVAL_DAYS * DAY
    st = server._store.concept_state("test-kp-1")
    assert st["next_review_at"] == T0 + MIN_INTERVAL_DAYS * DAY
    # 6 小时到期后：自动交付同知识点复习题（确定性快照重建，不调 LLM）
    _go(monkeypatch, T0 + MIN_INTERVAL_DAYS * DAY + 1)
    r2 = server.check_pending_quiz()
    assert r2["has_pending"] is True
    q2 = r2["quiz"]
    assert q2["review"] is True and q2["kp_id"] == "test-kp-1"
    assert env["gen"].calls == 1  # 复习不重复生成/billing
    right = server.submit_answer(q2["quiz_id"], "A")
    assert right["ok"] is True and right["correct"] is True
    assert right["schedule"]["interval_days"] == FIRST_INTERVAL_DAYS


def test_repeat_correct_interval_grows(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    q1 = server.check_pending_quiz()["quiz"]
    s1 = server.submit_answer(q1["quiz_id"], "A")
    assert s1["schedule"]["interval_days"] == FIRST_INTERVAL_DAYS
    review_time = s1["schedule"]["next_review_at"] + 1
    _go(monkeypatch, review_time)
    q2 = server.check_pending_quiz()["quiz"]
    assert q2["review"] is True
    s2 = server.submit_answer(q2["quiz_id"], "A")
    assert s2["correct"] is True
    assert s2["schedule"]["interval_days"] == round(FIRST_INTERVAL_DAYS * 2.8, 4)
    assert s2["schedule"]["next_review_at"] == review_time + 2.8 * DAY


# ============ 幂等：重复提交明确拒绝、不重复计数 ============
def test_duplicate_submit_rejected_counted_once(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    qid = server.check_pending_quiz()["quiz"]["quiz_id"]
    assert server.submit_answer(qid, "A")["ok"] is True
    r2 = server.submit_answer(qid, "B")
    assert r2["ok"] is False and "已作答" in r2["note"]
    assert server._store.concept_state("test-kp-1")["attempts"] == 1
    assert server._store.attempt_count(qid) == 1


# ============ 重启一致性：未完成题水合、历史保留 ============
def test_restart_unfinished_quiz_still_submittable(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    qid = server.check_pending_quiz()["quiz"]["quiz_id"]  # 未作答
    # 模拟重启：关连接、清内存态（store 路径不变，指向同一临时库）
    server._store.close()
    server._store = None
    server._state["quizzes"].clear()
    server._state["pending"].clear()
    _go(monkeypatch, T0 + 600)  # 未过期
    ans = server.submit_answer(qid, "A")
    assert ans["ok"] is True and ans["correct"] is True
    assert server._store.attempt_count(qid) == 1
    # 再次重启：作答历史与统计完整保留
    server._store.close()
    server._store = None
    server._state["quizzes"].clear()
    server._state["pending"].clear()
    stats = server.get_learning_stats()
    assert stats["total_attempts"] == 1
    assert stats["accuracy"] == 1.0
    assert stats["answered_quizzes"] == 1


# ============ 选题顺序：到期复习优先 + 同 kp 冷却 ============
def test_due_review_beats_new_context_generation(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    qid = server.check_pending_quiz()["quiz"]["quiz_id"]
    server.submit_answer(qid, "A")  # 到期时间 T0+1 天
    # 到期前出现新上下文：同 kp 未到复习期，不重复出题
    _go(monkeypatch, T0 + 60)
    _set_diff(env["diff"], "redis 分布式锁新话题")
    r = server.check_pending_quiz()
    assert r["has_pending"] is False
    assert env["gen"].calls == 1
    # 到期后（且上下文同 kp 相关）：优先交付到期复习题
    _go(monkeypatch, T0 + DAY + 1)
    r2 = server.check_pending_quiz()
    assert r2["has_pending"] is True
    assert r2["quiz"]["review"] is True
    assert env["gen"].calls == 1  # 复习仍是快照重建，不重新生成


# ============ 统计工具：聚合数字、无会话/代码原文 ============
def test_learning_stats_aggregates_only(env, monkeypatch):
    _go(monkeypatch, T0)
    _set_diff(env["diff"], "redis 缓存穿透")
    qid = server.check_pending_quiz()["quiz"]["quiz_id"]
    server.submit_answer(qid, "B")  # 答错 -> mastery 0（薄弱）
    # 第二个薄弱知识点走 store 直插（固定时间，保证确定性）
    s = server._store
    s.create_quiz("q2", "test-kp-2", {
        "kp_id": "test-kp-2", "kp_name": "分布式锁", "difficulty": "medium",
        "question": "Q2?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
        "answer": "A", "analysis": "p", "generated_by": "fake"},
        "answered", ".", "手动 | kp=test-kp-2", T0, T0 + 3600,
        delivered_at=T0, answered_at=T0)
    s.record_attempt("q2", "test-kp-2", "A", "A", False, T0,
                     next_state(False, None, T0))
    _go(monkeypatch, T0 + MIN_INTERVAL_DAYS * DAY + 1)  # 两个错题都到期
    stats = server.get_learning_stats()
    assert stats["ok"] is True
    assert stats["total_attempts"] == 2
    assert stats["correct_count"] == 0
    assert stats["accuracy"] == 0.0
    assert stats["answered_quizzes"] == 2
    assert stats["due_reviews"] == 2
    weak = {w["kp_id"]: w for w in stats["weak_knowledge_points"]}
    assert set(weak) == {"test-kp-1", "test-kp-2"}
    assert weak["test-kp-1"]["mastery"] == 0.0
    # 隐私：返回不含任何上下文原文/代码片段
    dumped = json.dumps(stats, ensure_ascii=False)
    assert "缓存穿透" not in dumped
    assert "question" not in dumped