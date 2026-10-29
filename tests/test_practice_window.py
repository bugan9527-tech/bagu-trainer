"""Explicit answer window integration: real queues, SQLite and main-thread Tk."""
import threading
from types import SimpleNamespace

from test_practice_latency import live_app, pump
from bagu_trainer.application.learning import engine


def window(app):
    app._continue_practice()
    app.root.update()
    return app.practice_window


def test_pending_entry_routes_and_hit_testing(live_app, monkeypatch):
    app, quizzes, _ = live_app
    app.root.deiconify()
    app._navigate_page("practice")
    app.root.update()
    page = app._page_frame("practice")
    assert not hasattr(page, "submit_button") and not hasattr(page, "question_label")
    tree = page.pending_list
    called = []
    original = page.on_enter
    monkeypatch.setattr(page, "on_enter", lambda qid: (called.append(qid), original(qid)))
    for index, route in enumerate(("button", "enter", "double")):
        target = quizzes[index]["quiz_id"]
        tree.selection_set(target)
        tree.focus(target)
        app.root.update()
        tree.focus_force()
        app.root.update()
        if route == "button":
            page.enter_button.invoke()
        elif route == "enter":
            tree.event_generate("<Return>")
        else:
            x, y, width, height = tree.bbox(target)
            tree.selection_set(quizzes[0]["quiz_id"])
            assert "_double_click" in tree.bind("<Double-1>")
            page._double_click(SimpleNamespace(x=x + 30, y=y + height // 2))
        pump(app, lambda: app.practice.quiz_id == target and not app.practice.busy)
        assert app.practice_window.state() == "normal"
    assert called == [q["quiz_id"] for q in quizzes]
    app.root.update()
    page._double_click(SimpleNamespace(x=30, y=2))  # heading
    page._double_click(SimpleNamespace(x=30, y=tree.winfo_height() - 5))  # empty
    assert len(called) == 3
    assert not app.practice.busy


def test_single_window_restore_and_drafts(live_app):
    app, quizzes, _ = live_app
    win = window(app)
    first, second = (q["quiz_id"] for q in quizzes[:2])
    app.practice.choose("C")
    win.hide()
    assert app.practice.drafts[first] == "C" and win.state() == "withdrawn"
    assert app._enter_pending(first)
    assert app.practice_window is win and win.state() == "normal"
    assert app._enter_pending(second)
    assert not app._enter_pending(second)
    assert app._practice_request["target"] == second
    pump(app, lambda: not app.practice.busy)
    win.hide()
    assert app._enter_pending(first)
    pump(app, lambda: app.practice.quiz_id == first)
    assert app.answer.get() == "C"
    assert app.practice_window is win
    assert [w for w in app.root.winfo_children() if w.winfo_class() == "Toplevel"] == [win]


def test_hide_during_submit_keeps_real_status_and_one_persisted_attempt(live_app, monkeypatch):
    app, quizzes, _ = live_app
    win = window(app)
    qid = quizzes[0]["quiz_id"]
    entered, release = threading.Event(), threading.Event()
    submit = app.monitor.engine.learning.submit_answer
    def slow_submit(*args):
        entered.set()
        assert release.wait(4)
        return submit(*args)
    monkeypatch.setattr(app.monitor.engine.learning, "submit_answer", slow_submit)
    try:
        app.practice.choose("A")
        assert app.practice.submit()
        assert entered.wait(1)
        assert not app.practice.submit() and not app._enter_pending(quizzes[1]["quiz_id"])
        assert win.submit_button.instate(["disabled"]) and win.dismiss_button.instate(["disabled"])
        win.hide()
        assert app.practice.submit_state == "submitting"
        app._continue_practice()
        assert app.practice_window is win and win.submit_button.cget("text") == "正在提交…"
        win.hide()
        release.set()
        pump(app, lambda: app.practice.submit_state == "finished")
        assert win.state() == "withdrawn"  # late grade does not focus/reopen
        app._continue_practice()
        assert "回答正确" in win.feedback_label.get("1.0", "end-1c")
        assert engine.session._store.attempt_count(qid) == 1
        app._apply_grade("obsolete-id", {"ok": False, "note": "wrong"})
        assert app.practice.grade["ok"] and not app.practice.submit()
    finally:
        release.set()


def test_failure_expiry_and_stale_delivery_preserve_original(live_app, monkeypatch):
    app, quizzes, _ = live_app
    win = window(app)
    first, second, third = (q["quiz_id"] for q in quizzes)
    app.practice.choose("B")
    enter = app.monitor.enter_quiz
    for outcome in (None, OSError("private details")):
        def fail(_):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(app.monitor, "enter_quiz", fail)
        assert app._enter_pending(second)
        pump(app, lambda: not app.practice.busy)
        assert app.practice.quiz_id == first and app.answer.get() == "B"
        assert "private details" not in win.notice.get()
        assert "过期" in win.notice.get() or "失败" in win.notice.get()
    monkeypatch.setattr(app.monitor, "enter_quiz", enter)
    assert app._enter_pending(second)
    old_request = dict(app._practice_request)
    pump(app, lambda: app.practice.quiz_id == second)
    assert app._enter_pending(third)
    app._finish_practice_open(dict(request=old_request, quiz=quizzes[0], failed=False, note=""))
    assert app.practice.entering_quiz_id == third
    pump(app, lambda: app.practice.quiz_id == third)
    assert app._enter_pending(first)
    pump(app, lambda: app.practice.quiz_id == first)
    assert app.answer.get() == "B"


def test_background_pending_and_legacy_arrival_never_focus_or_replace(live_app):
    app, quizzes, _ = live_app
    win = window(app)
    app.practice.choose("D")
    win.hide()
    app.root.deiconify()
    page = app._page_frame("practice")
    page.pending_list.focus_force()
    app.root.update()
    focus = app.root.focus_get()
    extra = {**quizzes[1], "quiz_id": "new-arrival"}
    app.events.put(("pending", [*quizzes, extra]))
    app.events.put(("quiz", extra))
    app._drain()
    app.root.update()
    assert win.state() == "withdrawn" and app.root.focus_get() is focus
    assert app.practice.quiz_id == quizzes[0]["quiz_id"] and app.answer.get() == "D"
    assert page.pending_list.exists("new-arrival")


def test_loading_hide_and_content_invalidation_rejects_late_result(live_app, monkeypatch):
    app, quizzes, _ = live_app
    win = window(app)
    entered, release = threading.Event(), threading.Event()
    enter = app.monitor.enter_quiz
    def slow_enter(qid):
        entered.set()
        assert release.wait(4)
        return enter(qid)
    monkeypatch.setattr(app.monitor, "enter_quiz", slow_enter)
    try:
        assert app._enter_pending(quizzes[1]["quiz_id"])
        assert "加载" in win.notice.get() and win.submit_button.instate(["disabled"])
        assert entered.wait(1)
        win.hide()
        app.practice.clear()  # content teardown clears reservation
        release.set()
        pump(app, lambda: app._practice_request is None)
        assert app.practice.current is None and win.state() == "withdrawn"
    finally:
        release.set()


def test_shortcuts_scope_hide_destroy_and_reopen(live_app):
    from tkinter import ttk
    app, quizzes, _ = live_app
    win = window(app)
    def key(widget, sequence):
        app.root.update()
        widget.focus_force()
        app.root.update()
        widget.event_generate(sequence)
        app.root.update()
    key(win.view.canvas, "<KeyPress-b>")
    assert app.answer.get() == "B"
    win.question_label.tag_add("sel", "1.0", "end")
    key(win.question_label, "<KeyPress-d>")
    assert app.answer.get() == "B"
    entry = ttk.Entry(win.view.body)
    entry.grid()
    key(entry, "<KeyPress-c>")
    key(entry, "<Control-Return>")
    key(entry, "<Alt-n>")
    assert app.answer.get() == "B" and not app.practice.busy
    key(app._page_frame("practice").pending_list, "<KeyPress-a>")
    assert app.answer.get() == "B"
    count = len(win._key_bindings)
    for _ in range(3):
        win.hide()
        app._continue_practice()
        app.root.update()
    assert len(win._key_bindings) == count
    win.destroy()
    assert app.practice_window is None and win.focus_timer is None and not win._key_bindings
    app._continue_practice()
    new = app.practice_window
    assert new is not win and len(new._key_bindings) == count
    key(new.view.canvas, "<Alt-n>")
    pump(app, lambda: app.practice.quiz_id == quizzes[1]["quiz_id"])
    assert not app.practice.busy


def test_page_switch_and_list_refresh_preserve_selection_scroll_and_draft(live_app):
    app, quizzes, _ = live_app
    project_watch = app.monitor.projects
    project_scope = app.monitor.project_snapshot()["applied_roots"]
    win = window(app)
    app.root.deiconify()
    app._navigate_page("practice")
    app.practice.choose("B")
    page = app._page_frame("practice")
    items = [{**quizzes[0], "quiz_id": f"row-{i}"} for i in range(60)]
    app._pending_items = items
    app._refresh_practice()
    app.root.update()
    tree = page.pending_list
    tree.selection_set("row-30")
    tree.focus("row-30")
    tree.yview_moveto(.5)
    app.root.update()
    scroll = tree.yview()[0]
    win.hide()
    app._navigate_page("projects")
    app.events.put(("pending", [*items, {**quizzes[1], "quiz_id": "added-row"}]))
    app._drain()
    app._navigate_page("practice")
    app.root.update()
    assert tree.selection() == ("row-30",) and tree.focus() == "row-30"
    assert abs(tree.yview()[0] - scroll) < .03
    assert app.answer.get() == "B" and app.practice.drafts[quizzes[0]["quiz_id"]] == "B"
    assert win.state() == "withdrawn"
    assert app.monitor.projects is project_watch
    assert app.monitor.project_snapshot()["applied_roots"] == project_scope


def test_background_does_not_deliver_until_explicit_entry(live_app):
    app, quizzes, _ = live_app
    app.practice_window.hide()
    app.practice.clear()
    app._practice_changed()
    app.jobs.put(("wake", None))
    pump(app, lambda: len(app._pending_items) == 3)
    assert app.practice.current is None and app.practice_window.state() == "withdrawn"
    qid = quizzes[1]["quiz_id"]
    assert engine.session._store.get_quiz(qid)["status"] == "ready"
    assert app._enter_pending(qid)
    pump(app, lambda: app.practice.quiz_id == qid)
    assert engine.session._store.get_quiz(qid)["status"] == "delivered"


def test_main_exit_drains_save_and_destroys_unique_child(live_app):
    app, quizzes, _ = live_app
    win = window(app)
    app.practice.choose("A")
    assert app.practice.submit()
    app._close()
    pump(app, lambda: app.closed)
    assert app.worker_done.is_set() and not app.worker_thread.is_alive()
    assert engine.session._store.attempt_count(quizzes[0]["quiz_id"]) == 1
    assert app.practice_window is None and win.closed and win.focus_timer is None
