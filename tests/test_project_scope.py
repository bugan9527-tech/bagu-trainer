# -*- coding: utf-8 -*-
"""多项目 ready/pending 作用域集成测试。

所有项目目录均为 pytest tmp_path 下的临时白名单目录；依赖 conftest 的
假文件源、假生成器与临时 SQLite，不读取真实 config.json、会话或密钥。
"""
import os
import threading

import pytest

import bagu_trainer.server as server


@pytest.fixture
def two_projects(env, tmp_path):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    env["cfg"]["project_roots"] = [str(project_a), str(project_b)]
    return project_a, project_b


def _scope(path):
    return os.path.normcase(os.path.realpath(str(path)))


def _set_diff(env, text="redis 缓存穿透"):
    env["diff"].results = [
        {"source": "git", "files": ["x.py"], "diff_text": text}
    ]


def test_ready_queue_and_unanswered_cooldown_are_project_scoped(
        env, two_projects):
    project_a, project_b = two_projects
    _set_diff(env)

    # A 只预生成 ready，不交付；B 的同知识点不应因此被冷却。
    generated_a = server._refresh(str(project_a), "")
    assert len(generated_a["generated"]) == 1
    quiz_a = generated_a["generated"][0]
    pending_a = next(
        p for p in server._state["pending"] if p["quiz_id"] == quiz_a["quiz_id"])
    assert pending_a["status"] == "ready"
    assert quiz_a["project_root"] == _scope(project_a)

    result_b = server.check_pending_quiz(project_root=str(project_b))
    assert result_b["ok"] is True and result_b["has_pending"] is True
    quiz_b = result_b["quiz"]
    assert quiz_b["quiz_id"] != quiz_a["quiz_id"]
    assert quiz_b["question"] != quiz_a["question"]
    assert quiz_b["project_root"] == _scope(project_b)
    assert env["gen"].calls == 2

    # B 的检查没有取走或改写 A 的 ready 题。
    assert pending_a["status"] == "ready"
    assert pending_a["delivered_at"] is None

    # A 仍可取回自己的 ready 题，且不会拿到 B 的题。
    result_a = server.check_pending_quiz(project_root=str(project_a))
    assert result_a["ok"] is True and result_a["has_pending"] is True
    assert result_a["quiz"]["quiz_id"] == quiz_a["quiz_id"]
    assert result_a["quiz"]["project_root"] == _scope(project_a)
    assert pending_a["status"] == "delivered"


def test_concurrent_checks_do_not_cross_project_queues(env, two_projects):
    project_a, project_b = two_projects
    _set_diff(env)
    generated_a = server._refresh(str(project_a), "")
    generated_b = server._refresh(str(project_b), "")
    assert len(generated_a["generated"]) == 1
    assert len(generated_b["generated"]) == 1
    assert env["gen"].calls == 2

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def worker(name, root):
        try:
            barrier.wait(timeout=5)
            results[name] = server.check_pending_quiz(project_root=str(root))
        except Exception as exc:  # 让主线程看到并发路径中的失败
            errors[name] = exc

    thread_a = threading.Thread(
        target=worker, args=("a", project_a), daemon=True)
    thread_b = threading.Thread(
        target=worker, args=("b", project_b), daemon=True)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert errors == {}
    assert results["a"]["has_pending"] is True
    assert results["b"]["has_pending"] is True
    quiz_a = results["a"]["quiz"]
    quiz_b = results["b"]["quiz"]
    assert quiz_a["quiz_id"] != quiz_b["quiz_id"]
    assert quiz_a["project_root"] == _scope(project_a)
    assert quiz_b["project_root"] == _scope(project_b)
    expected = {
        quiz_a["quiz_id"]: _scope(project_a),
        quiz_b["quiz_id"]: _scope(project_b),
    }
    actual = {
        p["quiz_id"]: _scope(p["project_root"])
        for p in server._state["pending"] if p["status"] == "delivered"
    }
    assert actual == expected


def test_report_delivery_is_atomic_for_memory_and_store(env, two_projects):
    project_a, _ = two_projects
    _set_diff(env)

    result = server.report_ai_activity(project_root=str(project_a))
    assert result["ok"] is True and len(result["quizzes"]) == 1
    quiz = result["quizzes"][0]
    qid = quiz["quiz_id"]
    assert quiz["project_root"] == _scope(project_a)

    pending = next(p for p in server._state["pending"] if p["quiz_id"] == qid)
    assert pending["status"] == "delivered"
    assert pending["delivered_at"] is not None
    assert server._state["quizzes"][qid]["_status"] == "delivered"

    stored = server._store.get_quiz(qid)
    assert stored["status"] == "delivered"
    assert stored["delivered_at"] is not None
