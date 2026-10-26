"""Answer-window layout, long content and simulated Tk scaling on hidden desktop."""
import sys
from pathlib import Path
import pytest
from bagu_trainer.practice_controller import PracticeController
from bagu_trainer.practice_page import PracticePage
from bagu_trainer.practice_window import PracticeWindow
from bagu_trainer.tk_runtime import create_root
from bagu_trainer.ui_state import window_var
from bagu_trainer.ui_theme import apply_theme
from release_tests.window_capture import capture_window

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk layout")


@pytest.fixture
def practice(request):
    root = create_root()
    root.tk.call("tk", "scaling", getattr(request, "param", 1) * 96 / 72)
    theme = apply_theme(root)
    root.geometry("900x650")
    submissions, entered, errors = [], [], []
    root.report_callback_exception = lambda *args: errors.append(args)
    controller = PracticeController(window_var(root), window_var(root), window_var(root),
        on_submit=lambda q, a: submissions.append((q, a)))
    quiz = {"quiz_id": "layout-fixture", "knowledge_point": "缓存一致性与事务控制",
            "project_root": "C:/Projects/cache-service",
            "question": "订单服务更新数据库后需要失效 Redis 缓存。两个并发请求可能在删除缓存后回填旧值。哪种方案最能降低不一致风险，并支持失败后重试？",
            "options": {
                "A": "先删除缓存再提交数据库事务，不再处理后续读取。",
                "B": "事务提交后通过可靠事件触发缓存失效，失败时重试，并为缓存设置合理的过期时间。",
                "C": "为所有缓存取消过期时间，避免重复查询数据库。",
                "D": "在数据库事务内持续持有锁，等待所有外部网络调用完成。"}}
    win = PracticeWindow(root, theme, controller)
    def enter(quiz_id):
        entered.append(quiz_id)
        controller.enter(quiz)
        win.show()
    queue = PracticePage(root, theme, controller, on_enter=enter)
    queue.pack(fill="both", expand=True)
    win.queue_page = queue
    controller.on_changed = lambda: (queue.refresh(), win.refresh())
    try:
        yield root, theme, win, controller, quiz, submissions, entered
    finally:
        controller.close()
        root.destroy()
        assert not errors


def assert_reading_visible(root, win):
    root.update()
    assert win.view.winfo_ismapped()
    assert win.view.winfo_width() >= win.theme.px(300)
    assert win.footer.winfo_ismapped()
    assert win.footer.winfo_y() + win.footer.winfo_height() <= win.winfo_height()
    assert win.submit_button.winfo_ismapped() and win.dismiss_button.winfo_ismapped()
    for button in win.option_buttons:
        win.view.reveal(button)
        root.update()
        assert button.winfo_ismapped() and button.instate(["!disabled"])
        assert button.winfo_rootx() >= win.view.canvas.winfo_rootx()
        assert button.winfo_rootx() + button.winfo_width() <= win.view.canvas.winfo_rootx() + win.view.canvas.winfo_width()
        button.invoke()
        assert win.controller.answer.get() == button.cget("value")


@pytest.mark.parametrize("practice", [1, 1.25, 1.5, 2], indirect=True, ids=["tk100", "tk125", "tk150", "tk200"])
def test_list_entry_separate_window_long_text_scaling_and_small_footer(practice):
    root, theme, win, controller, quiz, submissions, entered = practice
    page = win.queue_page
    page.set_pending([quiz], 4)
    page.pending_list.selection_set(quiz["quiz_id"])
    root.update()
    assert controller.current is None
    assert not hasattr(page, "submit_button")
    page.enter_button.invoke()
    root.update()
    assert entered == [quiz["quiz_id"]] and controller.quiz_id == quiz["quiz_id"]
    assert win.master is root and not root.grab_current()
    assert_reading_visible(root, win)
    out = Path(__file__).resolve().parents[1] / "validation/practice-window"
    out.mkdir(parents=True, exist_ok=True)
    scale = round(theme.scale * 100)
    if scale == 100:
        controller.choose("")
        win.view.canvas.yview_moveto(0)
        root.update()
        capture_window(win, out / "answer-window.png")
    controller.enter({**quiz, "quiz_id": "long", "question": (quiz["question"] + "\n") * 20,
                      "options": {k: (v + "\n") * 5 for k, v in quiz["options"].items()}})
    win.geometry(f"{theme.px(520)}x{theme.px(440)}")
    root.update()
    assert_reading_visible(root, win)
    win.submit_button.invoke()
    win.submit_button.invoke()
    assert submissions == [("long", "D")]
    controller.apply_grade({"ok": True, "correct": False, "correct_answer": "A",
                            "analysis": ("通过同一控制器保留草稿，并按题号回写判分。\n" * 50) + "解析末尾：验证完成。"}, "D")
    root.update()
    win.view.canvas.yview_moveto(1)
    root.update()
    assert win.view.canvas.yview()[1] == 1
    assert "解析末尾" in win.feedback_label.get("1.0", "end-1c")
    assert win.footer.winfo_y() + win.footer.winfo_height() <= win.winfo_height()
    capture_window(win, out / f"long-analysis-small-tk{scale}.png")
    win.view.canvas.yview_moveto(0)
    root.update()
    capture_window(win, out / f"long-question-small-tk{scale}.png")
