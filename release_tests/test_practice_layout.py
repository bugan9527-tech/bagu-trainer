"""Visible answer controls with a populated pending list; synthetic quizzes only."""
import sys

import pytest

from bagu_trainer.practice_controller import PracticeController
from bagu_trainer.practice_page import PracticePage
from bagu_trainer.tk_runtime import create_root
from bagu_trainer.ui_state import window_var
from bagu_trainer.ui_theme import apply_theme

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk layout")


@pytest.fixture
def practice(request):
    root = create_root()
    root.tk.call("tk", "scaling", getattr(request, "param", 1) * 96 / 72)
    theme = apply_theme(root)
    # Windows limits a Tk window's default maxsize to the desktop work area.
    # The hosted runner's smaller desktop otherwise clamps our *simulated*
    # wide viewport below its breakpoint at 125%+. Allow the requested test
    # viewport explicitly; this is not native-DPI or physical-screen evidence.
    root.maxsize(theme.px(1200), theme.px(1000))
    submissions, entered, errors = [], [], []
    root.report_callback_exception = lambda *args: errors.append(args)
    controller = PracticeController(window_var(root), window_var(root), window_var(root),
        on_submit=lambda q, a: submissions.append((q, a)))
    quiz = {"quiz_id": "layout-fixture", "knowledge_point": "合成知识点",
            "question": "如何保证练习中心的题干和选项始终可以阅读并作答？",
            "options": {letter: f"合成选项 {letter}" for letter in "ABCD"}}
    def enter(quiz_id):
        entered.append(quiz_id)
        controller.enter(quiz)
    page = PracticePage(root, theme, controller, on_enter=enter)
    page.pack(fill="both", expand=True)
    try:
        yield root, theme, page, controller, quiz, submissions, entered
    finally:
        controller.close()
        root.destroy()
        assert not errors


def assert_reading_visible(root, page):
    root.update()
    workspace = page.workspace
    assert page.view.winfo_ismapped()
    assert page.view.winfo_width() >= page.theme.px(300), (
        workspace.winfo_width(), page.list_column.winfo_width(), page.view.winfo_width())
    assert page.view.winfo_rootx() >= workspace.winfo_rootx()
    assert page.view.winfo_rootx() + page.view.winfo_width() <= workspace.winfo_rootx() + workspace.winfo_width()
    for button in page.option_buttons:
        page.view.reveal(button)
        root.update()
        assert button.winfo_ismapped() and button.instate(["!disabled"])
        assert button.winfo_rootx() >= page.view.canvas.winfo_rootx()
        assert button.winfo_rootx() + button.winfo_width() <= page.view.canvas.winfo_rootx() + page.view.canvas.winfo_width()
        button.invoke()
        assert page.controller.answer.get() == button.cget("value")


@pytest.mark.parametrize("practice", [1, 1.25, 1.5, 2], indirect=True, ids=["tk100", "tk125", "tk150", "tk200"])
def test_enter_from_populated_wide_list_keeps_all_options_visible(practice):
    root, theme, page, controller, quiz, submissions, entered = practice
    root.geometry(f"{theme.px(940)}x800+0+0")
    root.update()
    page.set_pending([quiz], 4)
    root.update()
    assert page.workspace.winfo_width() >= theme.px(900)
    assert page.wide and controller.current is None
    page.pending_list.selection_set(quiz["quiz_id"])
    root.update()
    page.enter_button.invoke()
    assert entered == [quiz["quiz_id"]] and controller.quiz_id == quiz["quiz_id"]
    assert_reading_visible(root, page)
    page.submit_button.invoke()
    page.submit_button.invoke()
    assert submissions == [(quiz["quiz_id"], "D")]


def test_split_stack_compact_and_pending_changes_preserve_answer(practice):
    root, _, page, controller, quiz, submissions, _ = practice
    root.geometry("1000x800+0+0")
    root.update()
    page.set_pending([quiz], 0)
    controller.enter(quiz)
    root.update()
    assert page.wide
    assert_reading_visible(root, page)
    for width, height in ((760, 800), (760, 560), (1000, 800)):
        root.geometry(f"{width}x{height}+0+0")
        root.update()
        assert controller.answer.get() == "D"
        assert_reading_visible(root, page)
        if page.compact_list:
            page.compact_toggle.invoke()
            root.update()
            assert page.list_column.winfo_ismapped()
            page.compact_toggle.invoke()
            assert_reading_visible(root, page)
    for pending in ([], [quiz], [], [quiz]):
        page.set_pending(pending, 0)
        root.update()
        assert page.wide is bool(pending)
        assert_reading_visible(root, page)
        assert controller.quiz_id == quiz["quiz_id"] and not submissions
