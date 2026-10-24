"""Real Tk selection, clipboard and answer-visibility tests with synthetic data."""
from contextlib import contextmanager
import sys
import tkinter as tk

import pytest

from release_tests.test_practice_layout import practice
from release_tests.test_answer_history import history, desktop, dispose, seed, pump, SNAP


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk clipboard")
QUESTION = "中文题干第一行\n第二行包含 English 与标点？"
OPTIONS = {letter: f"选项 {letter} 中文正文\n多行说明 {letter}" for letter in "ABCD"}
ANALYSIS = "第一行解析：保持数据一致。\n第二行解释。\n\n最后一段中文与 English。"


@contextmanager
def clipboard(root):
    try:
        previous = root.clipboard_get()
    except tk.TclError:
        previous = None
    try:
        root.clipboard_clear()
        root.clipboard_append("未复制答案")
        yield
    finally:
        root.clipboard_clear()
        if previous is not None:
            root.clipboard_append(previous)


def enter(practice):
    root, _, page, controller, quiz, submissions, _ = practice
    root.geometry("1000x800+0+0")
    controller.enter({**quiz, "question": QUESTION, "options": OPTIONS,
                      "answer": "D", "analysis": "不得读取题目对象中的隐藏解析"})
    root.update()
    return root, page, controller, submissions


def key(root, widget, sequence):
    widget.focus_force()
    root.update()
    widget.event_generate(sequence)
    root.update()


@pytest.mark.parametrize("field", ["question", "option", "feedback"])
def test_mouse_selection_ctrl_c_context_copy_and_readonly(practice, field, monkeypatch):
    root, page, ctl, _ = enter(practice)
    if field == "feedback":
        ctl.apply_grade({"ok": True, "correct": True, "correct_answer": "A", "analysis": ANALYSIS}, "A")
    widget = {"question": page.question_label, "option": page.option_texts[0], "feedback": page.feedback_label}[field]
    page.view.reveal(widget)
    root.update()
    before = widget.get("1.0", "end-1c")
    with clipboard(root):
        # Native Windows tk_popup waits for a human menu click in an OS modal
        # loop. Exercise the right-click binding and real menu command directly.
        posted = []
        monkeypatch.setattr(widget.menu, "tk_popup", lambda *position: posted.append(position))
        start, end = widget.bbox("1.0"), widget.bbox("1.3")
        assert start and end
        widget.event_generate("<ButtonPress-1>", x=start[0], y=start[1] + start[3] // 2)
        widget.event_generate("<B1-Motion>", x=end[0], y=end[1] + end[3] // 2)
        widget.event_generate("<ButtonRelease-1>", x=end[0], y=end[1] + end[3] // 2)
        root.update()
        assert widget.get("sel.first", "sel.last") == before[:3]
        key(root, widget, "<Control-c>")
        assert root.clipboard_get() == before[:3]
        widget.event_generate("<Button-3>", x=end[0], y=end[1])
        root.update()
        try:
            assert posted
            assert widget.menu.entrycget(0, "state") == "normal"
            root.clipboard_clear()
            widget.menu.invoke(0)
            assert root.clipboard_get() == before[:3]
        finally:
            widget.menu.unpost()
        key(root, widget, "<Control-a>")
        key(root, widget, "<Control-c>")
        assert root.clipboard_get() == before
        # Both native edit events and programmatic insertion fail in disabled state.
        for sequence in ("<KeyPress-x>", "<BackSpace>", "<Delete>", "<Control-v>", "<<Paste>>", "<<Cut>>"):
            key(root, widget, sequence)
        widget.insert("1.0", "误改")
        widget.delete("1.0", "end")
        assert widget.get("1.0", "end-1c") == before
        assert widget.cget("state") == "disabled"


def test_copy_buttons_visibility_success_failure_expiry_and_next_question(practice):
    root, page, ctl, _ = enter(practice)
    expected = QUESTION + "\n\n" + "\n".join(f"{letter}. {OPTIONS[letter]}" for letter in "ABCD")
    with clipboard(root):
        assert page.copy_solution_button.instate(["disabled"])
        page._copy_solution()  # Guard the callback, not just the button state.
        assert root.clipboard_get() == "未复制答案"
        page.copy_question_button.invoke()
        assert root.clipboard_get() == expected
        assert "隐藏解析" not in root.clipboard_get()
        ctl.choose("B")
        ctl.submit()
        assert page.copy_solution_button.instate(["disabled"])
        page._copy_solution()
        assert root.clipboard_get() == expected
        ctl.apply_grade({"ok": False, "note": "网络错误，请重试", "analysis": "不能复制"})
        assert page.copy_solution_button.instate(["disabled"])
        page._copy_solution()
        assert root.clipboard_get() == expected
        ctl.apply_grade({"ok": False, "note": "已过期", "correct_answer": "D", "analysis": "不能复制"})
        assert ctl.submit_state == "finished" and page.copy_solution_button.instate(["disabled"])
        page._copy_solution()
        assert root.clipboard_get() == expected
        ctl.enter({"quiz_id": "second", "question": QUESTION, "options": OPTIONS})
        ctl.choose("A")
        ctl.submit()
        ctl.apply_grade({"ok": True, "correct": True, "correct_answer": "A", "analysis": ANALYSIS}, "A")
        assert page.copy_solution_button.instate(["!disabled"])
        page.copy_solution_button.invoke()
        assert root.clipboard_get() == "正确答案：A\n\n解析：\n" + ANALYSIS
        ctl.enter({"quiz_id": "third", "question": "新题", "options": OPTIONS, "manual_review": True})
        assert page.copy_solution_button.instate(["disabled"])
        assert ANALYSIS not in page.feedback_label.get("1.0", "end-1c")
        root.clipboard_clear()
        root.clipboard_append("新题未提交")
        page._copy_solution()
        assert root.clipboard_get() == "新题未提交"


def test_text_selection_shortcuts_do_not_change_answer_and_ctrl_enter_still_submits(practice):
    root, page, ctl, submissions = enter(practice)
    ctl.choose("B")
    with clipboard(root):
        for widget in [page.question_label, *page.option_texts, page.feedback_label]:
            page.view.reveal(widget)
            key(root, widget, "<Control-a>")
            key(root, widget, "<Control-c>")
            key(root, widget, "<KeyPress-a>")
            key(root, widget, "<KeyPress-d>")
            assert ctl.answer.get() == "B" and not submissions
            assert widget.tag_ranges("sel")
        # Refreshing an unchanged question must retain the selected passage.
        page.refresh()
        assert page.question_label.tag_ranges("sel")
        key(root, page.option_texts[0], "<Control-Return>")
        assert submissions == [(ctl.quiz_id, "B")]
        key(root, page.option_texts[0], "<Control-Return>")
        assert len(submissions) == 1


def test_a_to_d_navigation_without_selection_and_tab_out_of_text(practice):
    root, page, ctl, submissions = enter(practice)
    for letter in "ABCD":
        key(root, page.view.canvas, f"<KeyPress-{letter.lower()}>")
        assert ctl.answer.get() == letter
    text = page.option_texts[0]
    text.tag_remove("sel", "1.0", "end")
    key(root, text, "<KeyPress-a>")
    assert ctl.answer.get() == "A"
    key(root, text, "<Tab>")
    assert root.focus_get() is not text
    page.pending_list.focus_force()
    key(root, page.pending_list, "<Control-Return>")
    assert not submissions


def test_history_copy_uses_saved_snapshot_and_repractice_hides_solution(history):
    engine, store, calls = history
    seed(store, snapshot={**SNAP, "question": QUESTION, "options": OPTIONS, "analysis": ANALYSIS})
    app, _, errors = desktop(history)
    app._start_worker()
    try:
        with clipboard(app.root):
            app._open_history()
            page = app.history_page
            pump(app, lambda: len(page.tree.get_children()) == 1)
            page.open_selected()
            pump(app, lambda: hasattr(page, "copy_solution_button"))
            page.copy_question_button.invoke()
            assert app.root.clipboard_get() == QUESTION + "\n\n" + "\n".join(f"{l}. {OPTIONS[l]}" for l in "ABCD")
            page.copy_solution_button.invoke()
            assert app.root.clipboard_get() == "正确答案：A\n\n解析：\n" + ANALYSIS
            for title in ("题干", "选项 A", "正确答案", "解析"):
                widget = page.detail_texts[title]
                page.detail_view.reveal(widget)
                key(app.root, widget, "<Control-a>")
                key(app.root, widget, "<Control-c>")
                assert app.root.clipboard_get() == widget.get("1.0", "end-1c")
                key(app.root, widget, "<KeyPress-d>")
                assert widget.cget("state") == "disabled"
            page.retry_button.invoke()
            pump(app, lambda: app.practice.current is not None and app.practice.manual)
            practice_page = app._page_frame("practice")
            assert practice_page.copy_solution_button.instate(["disabled"])
            assert ANALYSIS not in practice_page.feedback_label.get("1.0", "end-1c")
            app.root.clipboard_clear()
            app.root.clipboard_append("重练未提交")
            practice_page._copy_solution()
            assert app.root.clipboard_get() == "重练未提交"
            assert not calls and not errors
    finally:
        dispose(app)
