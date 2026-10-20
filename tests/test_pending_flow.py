# -*- coding: utf-8 -*-
"""pending 状态机与 check_pending_quiz 时序竞态修复的集成测试。

依赖 conftest 的全 fake 环境：无网络、无真实文件监听、无真实 .trae-cn、
无真实 LLM、无真实 config.json。
"""
import threading
import time

import bagu_trainer.server as server


def _set_diff(diff, text):
    diff.results = [{"source": "git", "files": ["x.py"], "diff_text": text}]


# ---- 验收：改完文件立刻 check 能拿到新题（不等 30 秒轮询）----
def test_check_generates_immediately(env):
    _set_diff(env["diff"], "redis 缓存穿透 缓存击穿")
    r1 = server.check_pending_quiz()
    assert r1["ok"] is True and r1["has_pending"] is True
    quiz = r1["quiz"]
    assert quiz.get("quiz_id")
    assert sorted(quiz["options"].keys()) == ["A", "B", "C", "D"]
    assert env["gen"].calls == 1
    # pending 记录包含要求的元数据字段
    p = server._state["pending"][0]
    for field in ("created_at", "delivered_at", "expires_at", "project_root", "kp_id"):
        assert field in p, field
    assert p["status"] == "delivered" and p["delivered_at"] is not None


# ---- 验收：同一题只推送一次，未作答仍可 submit，不重复生成 ----
def test_delivered_not_repushed_and_still_submittable(env):
    _set_diff(env["diff"], "redis 缓存穿透")
    r1 = server.check_pending_quiz()
    assert r1["has_pending"] is True
    quiz = r1["quiz"]
    # 再次 check：不重复推送、不重复生成
    r2 = server.check_pending_quiz()
    assert r2["ok"] is True and r2["has_pending"] is False
    assert env["gen"].calls == 1
    # 已交付未作答仍可正常作答
    ans = server.submit_answer(quiz["quiz_id"], "A")
    assert ans["ok"] is True and ans["correct"] is True
    assert ans["analysis"] == "解析"
    # 作答后不再推送
    r3 = server.check_pending_quiz()
    assert r3["has_pending"] is False
    assert env["gen"].calls == 1


# ---- 验收：两个线程同时 check 只生成一组题 ----
def test_concurrent_check_generates_only_once(env):
    _set_diff(env["diff"], "redis 并发")
    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        barrier.wait()
        results[name] = server.check_pending_quiz()

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert env["gen"].calls == 1
    delivered = [v for v in results.values() if v.get("has_pending")]
    assert len(delivered) == 1
    # 由拿到 False 的另一线程视角，该题依然可作答
    assert server.submit_answer(delivered[0]["quiz"]["quiz_id"], "A")["correct"] is True


# ---- 验收：同一上下文（仅空白差异）不重复生成 ----
def test_same_context_no_regeneration(env):
    _set_diff(env["diff"], "redis 缓存穿透")
    r1 = server.check_pending_quiz()
    assert r1["has_pending"] is True and env["gen"].calls == 1
    # 上下文仅空白差异：归一化后同指纹，不得再生成
    _set_diff(env["diff"], "  redis\t\n缓存穿透   ")
    server._poll_once()
    assert env["gen"].calls == 1
    assert len(server._state["pending"]) == 1
    r2 = server.check_pending_quiz()
    assert r2["has_pending"] is False and env["gen"].calls == 1


# ---- 验收：后台先出题，check 直接复用 ready 题不重复生成 ----
def test_monitor_then_check_reuses_ready(env):
    _set_diff(env["diff"], "redis 缓存预热")
    server._poll_once()
    assert env["gen"].calls == 1
    assert len(server._state["pending"]) == 1
    assert server._state["pending"][0]["status"] == "ready"
    r = server.check_pending_quiz()
    assert r["has_pending"] is True
    assert env["gen"].calls == 1
    r2 = server.check_pending_quiz()
    assert r2["has_pending"] is False and env["gen"].calls == 1


# ---- 验收：report 与 check 共用去重，同一活动不重复计费 ----
def test_report_then_check_no_double_generation(env):
    _set_diff(env["diff"], "redis 缓存穿透")
    rep = server.report_ai_activity()
    assert rep["ok"] is True and len(rep["quizzes"]) == 1
    assert env["gen"].calls == 1
    qid = rep["quizzes"][0]["quiz_id"]
    # check 同一上下文：去重，不再生成、不再推送（report 已直接交付）
    r = server.check_pending_quiz()
    assert r["has_pending"] is False and env["gen"].calls == 1
    # report 交付的题可直接作答
    assert server.submit_answer(qid, "A")["correct"] is True
    # 重复 report 同上下文：去重跳过，不重复生成
    rep2 = server.report_ai_activity()
    assert rep2["ok"] is True and rep2["quizzes"] == []
    assert env["gen"].calls == 1


# ---- 验收：过期题确定性清理，且不可再作答 ----
def test_expired_quiz_deterministically_cleaned(env):
    now = time.time()
    qid = "expired-1"
    server._state["quizzes"][qid] = {
        "kp_id": "test-kp-1", "kp_name": "测试知识点", "difficulty": "easy",
        "question": "Q", "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
        "answer": "A", "analysis": "p", "generated_by": "fake",
        "_created_at": now - 7200, "_expires_at": now - 1, "_status": "delivered",
    }
    server._state["pending"].append({
        "quiz_id": qid, "kp_id": "test-kp-1", "project_root": ".",
        "status": "delivered", "created_at": now - 7200, "delivered_at": now - 7100,
        "answered_at": None, "expires_at": now - 1,
    })
    r = server.check_pending_quiz()
    assert r["ok"] is True and r["has_pending"] is False
    assert qid not in server._state["quizzes"]
    assert all(p["quiz_id"] != qid for p in server._state["pending"])
    assert server.submit_answer(qid, "A")["ok"] is False
    assert env["gen"].calls == 0


# ---- 验收：超量题确定性淘汰（最旧优先），pending 不悬挂 ----
def test_over_capacity_deterministic_eviction(env, monkeypatch):
    small = {"quiz_ttl_seconds": 3600, "max_pending_quizzes": 3,
             "max_quizzes": 5, "seen_hash_capacity": 10}
    monkeypatch.setattr(server, "load_config",
                        lambda: {**env["cfg"], **env["caps"], **small})
    now = time.time()
    for i in range(7):
        qid = "q%d" % i
        server._state["quizzes"][qid] = {
            "kp_id": "test-kp-1", "kp_name": "测试知识点", "difficulty": "easy",
            "question": "Q%d" % i, "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "answer": "A", "analysis": "p", "generated_by": "fake",
            "_created_at": now - 100 + i, "_expires_at": now + 3600, "_status": "ready",
        }
        server._state["pending"].append({
            "quiz_id": qid, "kp_id": "test-kp-1", "project_root": ".",
            "status": "ready", "created_at": now - 100 + i, "delivered_at": None,
            "answered_at": None, "expires_at": now + 3600,
        })
    server.check_pending_quiz()
    assert len(server._state["quizzes"]) <= 5
    assert len(server._state["pending"]) <= 3
    # 淘汰确定性：最旧的先被清出
    assert "q0" not in server._state["quizzes"]
    assert "q1" not in server._state["quizzes"]
    # pending 不允许指向已被淘汰的 quiz
    live = set(server._state["quizzes"])
    assert all(p["quiz_id"] in live for p in server._state["pending"])


# ---- 验收：无效 project_root 返回明确错误且不生成 ----
def test_invalid_project_root_clear_error(env):
    bad = str(server.PROJECT_ROOT / "definitely_不存在_dir_2049")
    r = server.check_pending_quiz(project_root=bad)
    assert r["ok"] is False and r["has_pending"] is False
    assert "无效 project_root" in r["error"]
    assert env["gen"].calls == 0


# ---- 验收：白名单内显式传入有效 project_root 可用 ----
def test_check_with_explicit_valid_project_root(env, tmp_path):
    env["cfg"]["project_roots"].append(str(tmp_path))  # 隐私改造：必须先入白名单
    _set_diff(env["diff"], "redis 缓存穿透")
    r = server.check_pending_quiz(project_root=str(tmp_path))
    assert r["ok"] is True and r["has_pending"] is True
    assert env["gen"].calls == 1


# ---- seen 指纹容量上限：超限后最旧指纹被淘汰 ----
def test_seen_hash_capacity_bounded(env):
    cap = 3
    for i in range(10):
        key = "fp-%d" % i
        server._mark_seen_locked(key, cap)
    assert len(server._state["seen"]) == cap
    assert len(server._state["seen_set"]) == cap
    assert "fp-0" not in server._state["seen_set"]
    assert "fp-9" in server._state["seen_set"]