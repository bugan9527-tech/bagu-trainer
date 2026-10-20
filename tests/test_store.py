# -*- coding: utf-8 -*-
"""Store 层测试：临时库 + 固定时间戳，无网络、无真实用户数据。"""
from bagu_trainer.scheduler import next_state
from bagu_trainer.store import Store

T0 = 1_800_000_000.0
HOUR = 3600.0
DAY = 86400.0

SNAP = {
    "kp_id": "kp-1", "kp_name": "缓存穿透", "difficulty": "medium",
    "question": "Q?", "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
    "answer": "A", "analysis": "解析", "generated_by": "fake",
}


def _put_state(s, quiz_id, kp_id, correct, answered_at):
    """造一条已作答记录：quizzes + attempts + concept_state 一套落库"""
    s.create_quiz(quiz_id, kp_id, dict(SNAP, kp_id=kp_id), "answered", "/p",
                  f"手动 | kp={kp_id}", answered_at - 10, answered_at - 10 + HOUR,
                  delivered_at=answered_at - 10, answered_at=answered_at)
    state = next_state(correct, None, answered_at)
    s.record_attempt(quiz_id, kp_id, "A" if correct else "B",
                     "A" if correct else "B", correct, answered_at, state)


# ---- 重启一致性：关库重开，历史数据完整保留 ----
def test_reopen_keeps_history(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    _put_state(s, "q1", "kp-1", False, T0)
    assert s.get_quiz("q1")["status"] == "answered"
    assert s.concept_state("kp-1")["attempts"] == 1
    expected_next = T0 + 0.25 * DAY  # 答错 -> 6 小时到期
    assert s.concept_state("kp-1")["next_review_at"] == expected_next
    s.close()
    # 模拟重启：同路径重新打开
    s2 = Store(str(tmp_path / "t.db"))
    try:
        assert s2.get_quiz("q1")["snapshot"]["question"] == "Q?"
        assert s2.get_quiz("q1")["source_summary"] == "手动 | kp=kp-1"
        assert s2.concept_state("kp-1")["next_review_at"] == expected_next
        assert s2.concept_state("kp-1")["mastery"] == 0.0
        assert s2.attempt_count("q1") == 1
    finally:
        s2.close()


# ---- 水合：未完成题复活，过期题写通 expired ----
def test_load_pending_resurrects_and_expires(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.create_quiz("p1", "kp-1", SNAP, "ready", "/p", "", T0, T0 + 3600)
    s.create_quiz("p2", "kp-2", SNAP, "delivered", "/p", "", T0, T0 + 7200,
                  delivered_at=T0)
    s.create_quiz("old", "kp-1", SNAP, "ready", "/p", "", T0 - 7200, T0 - 1)
    # T0+1800：old 已过期被标记；p1/p2 未过期可水合
    rows = s.load_pending(T0 + 1800)
    assert {r["quiz_id"] for r in rows} == {"p1", "p2"}
    assert s.get_quiz("old")["status"] == "expired"
    # T0+7300：p1/p2 也过期 -> 全部写通 expired，不再水合
    rows = s.load_pending(T0 + 7300)
    assert rows == []
    assert s.get_quiz("p1")["status"] == "expired"
    assert s.get_quiz("p2")["status"] == "expired"
    s.close()


# ---- due 排序：最早到期在前 ----
def test_due_kps_ordered_by_next_review(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    _put_state(s, "qb", "kp-b", False, T0)          # next = T0 + 6h
    _put_state(s, "qa", "kp-a", False, T0 - 6 * HOUR)  # next = T0
    _put_state(s, "qc", "kp-c", False, T0 + 1)      # next = T0 + 1 + 6h（未到期）
    due = s.due_kps(T0 + 6 * HOUR + 0.5)
    assert [d["kp_id"] for d in due] == ["kp-a", "kp-b"]
    s.close()


# ---- 统计与薄弱知识点 ----
def test_stats_and_weak_kps(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    _put_state(s, "q1", "kp-weak", False, T0)   # mastery 0（薄弱）
    _put_state(s, "q2", "kp-strong", True, T0)  # mastery 1（不薄弱）
    # 手动把 kp-strong 的下次复习调到未来
    st = s.concept_state("kp-strong")
    s.record_attempt("q2", "kp-strong", "A", "A", True, T0 + DAY,
                     next_state(True, st, T0 + DAY))
    stats = s.stats(T0 + 6 * HOUR + 1)
    assert stats["total_attempts"] == 3
    assert stats["correct_count"] == 2
    assert stats["answered_quizzes"] == 2
    # 此刻 kp-weak 已到期（答错 6 小时后），kp-strong 未到期 -> due=1
    assert stats["due_reviews"] == 1
    weak = s.weak_kps(10, 0.6)
    assert [w["kp_id"] for w in weak] == ["kp-weak"]
    assert weak[0]["mastery"] == 0.0 and weak[0]["attempts"] == 1
    s.close()


# ---- 题目标快照不含内部字段 ----
def test_snapshot_strips_internal_fields(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    quiz = dict(SNAP, **{"_created_at": T0, "_expires_at": T0 + 1})
    s.create_quiz("q1", "kp-1", {k: v for k, v in quiz.items()
                                 if not str(k).startswith("_")},
                  "ready", "/p", "", T0, T0 + 1)
    row = s.get_quiz("q1")
    assert "_created_at" not in row["snapshot"]
    assert row["snapshot"]["answer"] == "A"  # 判分所需字段保留
    s.close()