"""Isolated SQLite and real Tk automation; no real model or user data."""
import collections
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bagu_trainer.scheduler import next_state
from bagu_trainer.store import Store


SNAP = {
    "kp_id": "historic-kp", "kp_name": "缓存穿透", "generated_by": "llm",
    "question": "当缓存查询不存在的键时，应如何防止重复访问数据库？",
    "options": {"A": "缓存空值", "B": "删除所有缓存", "C": "增加重复查询", "D": "关闭数据库"},
    "answer": "A", "analysis": "缓存空值可避免相同的不存在键持续访问数据库。",
    "excerpt_source": {"repo": "历史来源", "path": "缓存.md", "url": "https://example.org/source"},
}
NOW = 1_800_000_000.0


@pytest.fixture
def history(tmp_path, monkeypatch):
    from bagu_trainer import config
    bank = config.USER_DATA_ROOT / "data"
    bank.mkdir(parents=True, exist_ok=True)
    (bank / "questions.json").write_text(json.dumps([{
        "id": "fixture", "knowledge_point": "测试知识点", "question": "测试来源题目",
        "tags": ["缓存"], "keywords": ["缓存"]}], ensure_ascii=False), encoding="utf-8")
    from bagu_trainer import engine
    store = Store(tmp_path / "history.db")
    monkeypatch.setattr(engine, "_store", store)
    monkeypatch.setattr(engine, "_state", {"quizzes": {}, "pending": [], "seen": collections.deque(), "seen_set": set()})
    monkeypatch.setattr(engine, "_diff_readers", {})
    monkeypatch.setattr(engine, "_now", lambda: NOW)
    monkeypatch.setattr(engine, "load_config", lambda: {
        "project_roots": [str(tmp_path)], "quiz_ttl_seconds": 3600,
        "max_pending_quizzes": 1, "max_quizzes": 2, "seen_hash_capacity": 100})
    monkeypatch.setattr(engine, "kb", SimpleNamespace(get=lambda _: None, all=lambda: []))
    calls = []
    monkeypatch.setattr(engine, "generator", SimpleNamespace(generate=lambda *args: calls.append(args)))
    yield engine, store, calls
    store.close()


def seed(store, number=1, *, snapshot=None, correct=False, root=r"C:\removed-project", stamp=NOW):
    quiz_id = f"original-{number}"
    snap = dict(SNAP) if snapshot is None else snapshot
    store.create_quiz(quiz_id, "historic-kp", snap, "answered", root, "历史来源摘要", stamp-5, stamp+100)
    store.record_attempt(quiz_id, "historic-kp", "A" if correct else "B", "A" if correct else "B",
                         correct, stamp, next_state(correct, None, stamp))
    return store._conn.execute("SELECT MAX(id) FROM attempts").fetchone()[0]


def dump(store):
    return list(store._conn.iterdump())


def test_history_sort_paging_search_filters_and_attempt_identity(history):
    engine, store, calls = history
    for n in range(45):
        seed(store, n, correct=n % 2 == 0, root="old" if n < 25 else "new", stamp=NOW+n//3)
    before = dump(store)
    pages = [engine.get_answer_history(page=n) for n in range(3)]
    assert [len(p["items"]) for p in pages] == [20, 20, 5]
    records = [item for page in pages for item in page["items"]]
    assert [(r["answered_at"], r["id"]) for r in records] == sorted(
        [(r["answered_at"], r["id"]) for r in records], reverse=True)
    assert len({r["id"] for r in records}) == 45
    assert engine.get_answer_history(result="wrong")["total"] == 22
    assert engine.get_answer_history(result="correct", project="old")["total"] == 13
    assert engine.get_answer_history(keyword="穿透")["total"] == 45
    assert engine.get_answer_history(keyword="数据库")["total"] == 45
    assert engine.get_answer_history(keyword="%' OR 1=1 --")["total"] == 0
    assert engine.get_answer_history(project="old")["projects"] == ["new", "old"]
    assert engine.get_answer_history(page=999)["page"] == 2
    assert before == dump(store)
    # Old DBs can contain multiple attempts on the same quiz: retain each answer.
    store.record_attempt("original-0", "historic-kp", "D", "D", False, NOW+90, next_state(False, None, NOW+90))
    detail = engine.get_answer_detail(46)
    assert detail["answer"] == "D" and detail["snapshot"]["answer"] == "A"
    assert detail["snapshot"]["analysis"] == SNAP["analysis"]
    assert not calls


def test_browse_is_read_only_and_independent_of_generation_lock(history):
    engine, store, calls = history
    attempt = seed(store)
    store.create_quiz("old-pending", "x", SNAP, "ready", "old", "", NOW-100, NOW-1)
    before = dump(store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine._STATE_LOCK:  # model generation may hold this lock for minutes
            page = pool.submit(engine.get_answer_history).result(timeout=1)
            detail = pool.submit(engine.get_answer_detail, attempt).result(timeout=1)
    assert page["total"] == 1 and detail["can_retry"]
    assert detail["project_root"] == r"C:\removed-project"
    assert detail["snapshot"]["excerpt_source"] == SNAP["excerpt_source"]
    assert dump(store) == before and not calls
    assert "08:00:00" in engine.history_time(0)  # test machine local zone Asia/Shanghai


def test_repractice_new_id_deduplicated_persisted_and_submission_once(history):
    engine, store, calls = history
    attempt = seed(store)
    before = store.stats(NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(engine.repractice_attempt, [attempt, attempt]))
    quiz_id = results[0]["quiz"]["quiz_id"]
    assert results[1]["quiz"]["quiz_id"] == quiz_id != "original-1"
    assert "answer" not in results[0]["quiz"] and "analysis" not in results[0]["quiz"]
    assert store.stats(NOW) == before
    snap = store.get_quiz(quiz_id)["snapshot"]
    assert snap["retry_of_attempt_id"] == attempt and snap["retry_of_quiz_id"] == "original-1"
    assert snap["question"] == SNAP["question"]
    engine._state["quizzes"].clear()
    engine._state["pending"].clear()
    assert engine.repractice_attempt(attempt)["quiz"]["quiz_id"] == quiz_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        grades = list(pool.map(lambda _: engine.submit_answer(quiz_id, "A"), range(2)))
    assert sum(g["ok"] for g in grades) == 1
    grade = next(g for g in grades if g["ok"])
    assert grade["correct"] and grade["analysis"] == SNAP["analysis"]
    assert store.attempt_count(quiz_id) == 1 and store.attempt_count("original-1") == 1
    assert store.stats(NOW)["total_attempts"] == before["total_attempts"] + 1
    assert store.concept_state("historic-kp")["attempts"] == 2
    assert not calls


@pytest.mark.parametrize("snapshot", [{}, {**SNAP, "generated_by": "offline"},
    {**SNAP, "options": {"A": "a"}}, {**SNAP, "answer": None},
    {**SNAP, "analysis": ""}, {**SNAP, "question": "短"},
    {**SNAP, "options": dict.fromkeys("ABCD", "重复")}])
def test_legacy_snapshots_readable_but_retry_disabled(history, snapshot):
    engine, store, _ = history
    attempt = seed(store, snapshot=snapshot)
    before = dump(store)
    detail = engine.get_answer_detail(attempt)
    assert detail["ok"] and not detail["can_retry"] and detail["retry_reason"]
    assert not engine.repractice_attempt(attempt)["ok"]
    assert dump(store) == before


def test_invalid_json_empty_missing_and_query_failure_recovery(history, monkeypatch):
    engine, store, _ = history
    assert engine.get_answer_history()["total"] == 0
    attempt = seed(store)
    store._conn.execute("UPDATE quizzes SET snapshot='broken json'")
    store._conn.commit()
    assert engine.get_answer_history()["total"] == 1
    assert engine.get_answer_detail(attempt)["snapshot"] == {}
    assert not engine.get_answer_detail(999)["ok"]
    original = Store.history_page
    monkeypatch.setattr(Store, "history_page", lambda *a, **k: (_ for _ in ()).throw(OSError("private detail")))
    result = engine.get_answer_history()
    assert not result["ok"] and "private detail" not in result["note"]
    monkeypatch.setattr(Store, "history_page", original)
    assert engine.get_answer_history()["ok"]


def test_pending_automatic_quiz_survives_manual_retry_and_caps(history):
    engine, store, _ = history
    from bagu_trainer.desktop_monitor import DesktopMonitor
    attempt = seed(store)
    snap = {**SNAP, "review": True}
    store.create_quiz("auto", "historic-kp", snap, "ready", engine.load_config()["project_roots"][0], "", NOW, NOW+3600)
    engine._hydrate_pending_locked(store)
    monitor = DesktopMonitor(engine)
    monitor.delivery_paused.set()
    assert monitor.take_quiz() is None
    retry = engine.repractice_attempt(attempt)
    engine._cleanup_locked(engine._caps())
    assert store.get_quiz("auto")["status"] == "ready"
    assert any(p["quiz_id"] == "auto" for p in engine._state["pending"])
    monitor.delivery_paused.clear()
    assert monitor.take_quiz()["quiz_id"] == "auto"
    assert engine.submit_answer("auto", "B")["ok"]
    monitor.active = None
    # The old project is outside the whitelist but this snapshot needs no IO.
    assert monitor.resume_quiz()["quiz_id"] == retry["quiz"]["quiz_id"]


def test_pending_list_count_and_enter_are_read_only_until_enter(history):
    engine, store, _ = history
    from bagu_trainer.desktop_monitor import DesktopMonitor
    attempt = seed(store)
    project = engine.load_config()["project_roots"][0]
    for qid in ("p1", "p2"):
        store.create_quiz(qid, "historic-kp", {**SNAP, "review": True}, "ready", project, "", NOW, NOW + 3600)
    engine._hydrate_pending_locked(store)
    retry = engine.repractice_attempt(attempt)  # manual, outside project scope
    monitor = DesktopMonitor(engine)
    # Viewing the list must NOT deliver anything.  (The automatic cap here is 1,
    # so only the newest ready quiz and the manual retry survive.)
    pending = monitor.list_pending()
    assert {p["quiz_id"] for p in pending} == {"p2", retry["quiz"]["quiz_id"]}
    assert monitor.pending_count() == 2
    assert store.get_quiz("p1")["status"] == "ready"
    assert store.get_quiz("p2")["status"] == "ready"
    # enter_quiz delivers only the selected one.
    entered = monitor.enter_quiz("p2")
    assert entered["quiz_id"] == "p2"
    assert store.get_quiz("p2")["status"] == "delivered"
    assert store.get_quiz("p1")["status"] == "ready"
    assert monitor.active == "p2"
    # An out-of-scope or already-answered id is rejected.
    assert monitor.enter_quiz("missing") is None
    engine.submit_answer("p2", "A")
    assert monitor.enter_quiz("p2") is None


def pump(app, condition, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        if condition():
            return
        time.sleep(.01)
    raise AssertionError("UI timed out")


def desktop(history):
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.desktop_monitor import DesktopMonitor
    engine, store, calls = history
    polls = []
    monitor = DesktopMonitor(engine)
    def poll():
        polls.append(1)
        return {"state": "watching", "projects": 1}
    monitor.poll = poll
    app = DesktopApp(monitor)
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    return app, polls, errors


def dispose(app):
    app.stop.set()
    if app.worker_thread:
        app.worker_thread.join(4)
    if app.history_page:
        app.history_page.close()
        app.history_page = None
    app.root.destroy()


def test_real_tk_history_navigation_stale_results_and_long_text(history):
    engine, store, calls = history
    for n in range(25):
        seed(store, n, snapshot={**SNAP, "question": SNAP["question"] * 20, "analysis": SNAP["analysis"] * 100})
    app, polls, errors = desktop(history)
    original_root = app.root
    app._start_worker()
    try:
        app.history_button.invoke()
        page = app.history_page
        app.root.geometry("560x560")
        pump(app, lambda: len(page.tree.get_children()) == 20)
        assert app.root is original_root and app.worker_thread.is_alive() and not app.stop.is_set()
        assert app.monitor.delivery_paused.is_set()
        assert app.monitor.initialized == set()
        page.tree.selection_set("10")
        page.tree.yview_moveto(.4)
        saved = page.tree.yview()[0]
        page.tree.focus_force()
        page.tree.event_generate("<Return>")
        pump(app, lambda: hasattr(page, "detail_view"))
        assert "答错" in page.message.get()
        page.detail_view.canvas.focus_force()
        page.detail_view.canvas.event_generate("<End>")
        app.root.update()
        assert page.detail_view.canvas.yview()[1] == 1
        assert page.home_button.winfo_ismapped() and page.retry_button.winfo_ismapped()
        page.back_button.invoke()
        app.root.update()
        assert page.tree.selection() == ("10",)
        assert abs(page.tree.yview()[0] - saved) < .06
        page.keyword.set("没有匹配关键词")
        page.refresh()
        old_token = page.token - 1
        page.receive(page.page_id, old_token, "list", {"ok": True})  # stale result ignored
        pump(app, lambda: "没有匹配" in page.message.get())
        page.keyword.set("")
        page.refresh()
        pump(app, lambda: len(page.tree.get_children()) == 20)
        # Delivery raced with entering history: retain every ID without popup.
        app._show_quiz({"quiz_id": "race-one"})
        app._show_quiz({"quiz_id": "race-two"})
        assert app.practice.current["quiz_id"] == "race-one" and len(app.practice.deferred) == 1
        app.practice.deferred.clear()
        page.home_button.invoke()
        assert app.history_page is None and not app.monitor.delivery_paused.is_set()
        assert app.root is original_root and app.worker_thread.is_alive()
        assert not calls and polls and not errors
    finally:
        dispose(app)


def test_real_tk_manual_practice_uses_existing_submit_and_keyboard(history):
    engine, store, calls = history
    attempt = seed(store)
    app, _, errors = desktop(history)
    app._start_worker()
    try:
        app._open_history()
        page = app.history_page
        pump(app, lambda: len(page.tree.get_children()) == 1)
        page.open_selected()
        pump(app, lambda: hasattr(page, "retry_button"))
        page.retry_button.invoke()
        page.retry()  # repeated event cannot create a second job
        pump(app, lambda: app.practice.current is not None and app.practice.manual)
        qid = app.practice.quiz_id
        assert "缓存空值可避免" not in app.feedback.get()
        practice = app._page_frame("practice")
        practice.focus_force()
        practice.event_generate("<KeyPress-a>")
        app.root.update()
        assert app.answer.get() == "A"
        practice.event_generate("<Control-Return>")
        app.root.update()
        app.practice.submit()
        pump(app, lambda: app.practice.submit_state == "finished")
        assert "回答正确" in app.feedback.get() and SNAP["analysis"] in app.feedback.get()
        assert store.attempt_count(qid) == 1 and store.attempt_count("original-1") == 1
        app.practice.dismiss()
        page.home_button.invoke()
        assert not calls and not errors
    finally:
        dispose(app)


def test_history_page_error_empty_retry_and_late_result_after_exit(history, monkeypatch):
    app, _, errors = desktop(history)
    engine, _, _ = history
    try:
        app._open_history()
        page = app.history_page
        pump(app, lambda: "暂无答题记录" in page.message.get())
        original = engine.get_answer_history
        monkeypatch.setattr(engine, "get_answer_history", lambda **kw: {"ok": False, "note": "查询失败，请重试。"})
        page.refresh()
        pump(app, lambda: "查询失败" in page.message.get())
        monkeypatch.setattr(engine, "get_answer_history", original)
        page.refresh()
        pump(app, lambda: "暂无答题记录" in page.message.get())
        token = page.token
        page.home_button.invoke()
        # Hiding keeps this page alive; only obsolete requests are discarded.
        app.events.put(("history", (page.page_id, token - 1, "list", {"ok": True})))
        app._drain()
        assert app.history_page is None and not errors
    finally:
        dispose(app)


def test_real_monitor_continues_file_observation_and_queues_during_history(history, tmp_path, monkeypatch):
    engine, store, calls = history
    from bagu_trainer.desktop_monitor import DesktopMonitor
    monkeypatch.setattr(engine, "find_trae_home", lambda *args: None)
    path = tmp_path / "cache.py"
    path.write_text("cache = 1\n", encoding="utf-8")
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    generated = []
    def generate(context, root, *args, **kwargs):
        generated.append(context)
        store.create_quiz("from-watch", "historic-kp", {**SNAP, "review": True}, "ready", root, "", NOW, NOW+3600)
        engine._hydrate_pending_locked(store)
        return [1], [1], 0, []
    monkeypatch.setattr(engine, "_generate_matches_locked", generate)
    monitor.poll()
    baseline = dict(engine._diff_readers)
    monitor.delivery_paused.set()
    path.write_text("cache = 200\n# 文件变化触发监测\n", encoding="utf-8")
    assert monitor.poll()["state"] == "quiz_ready"
    assert generated and baseline == engine._diff_readers
    assert monitor.take_quiz() is None
    assert store.get_quiz("from-watch")["status"] == "ready"
    monitor.delivery_paused.clear()
    assert monitor.take_quiz()["quiz_id"] == "from-watch"
    assert not calls


def test_worker_finishes_already_queued_submission_on_exit(history):
    engine, store, _ = history
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.desktop_monitor import DesktopMonitor
    attempt = seed(store)
    qid = engine.repractice_attempt(attempt)["quiz"]["quiz_id"]
    stop, done = threading.Event(), threading.Event()
    jobs, events = queue.Queue(), queue.Queue()
    jobs.put(("submit", (qid, "A")))
    stop.set()
    DesktopApp._worker(SimpleNamespace(monitor=DesktopMonitor(engine), stop=stop,
                                      worker_done=done, jobs=jobs, events=events))
    assert done.is_set() and store.attempt_count(qid) == 1


def test_dismiss_each_deferred_popup_keeps_its_own_id(history):
    app, _, errors = desktop(history)
    try:
        quiz = {"quiz_id": "first", "question": SNAP["question"], "options": SNAP["options"]}
        app._show_quiz(quiz)
        app._show_quiz({**quiz, "quiz_id": "second"})
        assert len(app.practice.deferred) == 1
        app.practice.dismiss()
        assert app.practice.quiz_id == "second"
        app.practice.dismiss()  # before worker has consumed the first dismissal
        assert [app.jobs.get_nowait(), app.jobs.get_nowait()] == [("dismiss", "first"), ("dismiss", "second")]
        assert not errors
    finally:
        dispose(app)
