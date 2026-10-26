"""Normal/long content in real Tk layouts; no network or credential writes."""
import queue
import sys
from types import SimpleNamespace

import pytest

from release_tests.test_ui_theme import assert_inside, cancel_timers
from bagu_trainer.tk_runtime import prepare_tk

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk layout")


@pytest.mark.parametrize("scale", [1, 1.5])
@pytest.mark.parametrize("long", [False, True], ids=["normal", "long"])
def test_window_content_reflows_and_actions_remain_reachable(monkeypatch, scale, long):
    prepare_tk()
    import tkinter as tk
    import ttkbootstrap  # load subclasses before replacing Tk with a test factory
    from bagu_trainer.first_use import FirstUseWizard
    from bagu_trainer.settings_window import ModelSettingsWindow
    from bagu_trainer.desktop import DesktopApp
    real_tk = tk.Tk
    def scaled_root():
        root = real_tk()
        root.tk.call("tk", "scaling", scale * 96 / 72)
        return root
    monkeypatch.setattr(tk, "Tk", scaled_root)
    monkeypatch.setattr(FirstUseWizard, "_restore", lambda self: None)
    text = "长文本说明与中文路径；" * (180 if long else 1) + "末尾标记"
    wizard = FirstUseWizard(SimpleNamespace(events=queue.Queue()))
    try:
        wizard.root.geometry("%dx%d" % wizard.root.minsize())
        wizard._show("model")
        wizard.status.set(text)
        wizard.key_hint.set(text)
        wizard.root.update()
        wizard.model_view.reveal(wizard.test_button)
        assert_inside(wizard.test_button, wizard.root)
        assert_inside(wizard.start_button, wizard.root)
        assert_inside(wizard.back_button, wizard.root)
        assert wizard.steps_text.get() == "第 2 / 4 步"
        assert float(wizard.step_progress.cget("value")) == 2
        assert "当前" in wizard.step_labels[1].cget("text")
        assert wizard.status_area.text.get("1.0", "end-1c") == text
        wizard.status_area.text.yview_moveto(1)
        wizard.root.update()
        assert wizard.status_area.text.yview()[1] == 1
        wizard._show("projects")
        wizard.project_list.insert("end", "C:/学习项目/" + text)
        for button in wizard.project_buttons:
            assert_inside(button, wizard.root)
        if long:
            wizard.project_list.xview_moveto(1)
            assert wizard.project_list.xview()[0] > 0
    finally:
        cancel_timers(wizard.root)
        wizard._close()

    settings = ModelSettingsWindow()
    try:
        settings.root.geometry("%dx%d" % settings.root.minsize())
        settings.status.set(text)
        settings.root.update()
        settings.url_var.set("https://example.invalid/" + text)
        settings.status.set(text)
        settings.form_view.reveal(settings.entries[-1])
        assert_inside(settings.entries[-1], settings.root)
        assert_inside(settings.save_button, settings.root)
        assert_inside(settings.test_button, settings.root)
        assert settings.verified is None and settings.save_button.instate(["disabled"])
        assert settings.status_area.text.get("1.0", "end-1c") == text
    finally:
        cancel_timers(settings.root)
        settings._close()

    app = DesktopApp(SimpleNamespace(paused=False))
    try:
        app.root.geometry("%dx%d" % app.root.minsize())
        app.status.set(text)
        app._render_dashboard({"state": "watching", "projects": ["C:/代码/" + text],
            "stats": {"ok": True, "total_attempts": 12, "accuracy": .75, "due_reviews": 3}})
        assert_inside(app.pause_button, app.root)
        assert app.learning_values["answered"].get() == "12"
        assert app.learning_values["accuracy"].get() == "75%"
        quiz = {"quiz_id": "layout", "knowledge_point": "缓存一致性", "project_root": text,
                "question": text, "options": {letter: text for letter in "ABCD"}}
        app._show_quiz(quiz)
        practice = app._answer_window()
        app._navigate_page("practice")
        practice.show()
        practice.geometry("%dx%d" % practice.minsize())
        app.root.update()
        assert_inside(practice.submit_button, practice)
        app.root.update()
        narrow_height = practice.question_label.winfo_reqheight()
        practice.geometry("%dx%d" % app.theme.px((1000, 700)))
        app.root.update()
        assert practice.question_label.winfo_reqheight() <= narrow_height
        assert practice.question_label.cget("wrap") == "word"
        app.practice.apply_grade({"ok": True, "correct": True, "correct_answer": "A",
                                  "analysis": text * 4, "schedule": {"interval_days": 1}})
        app.root.update()
        assert text * 4 in app.feedback.get()
        if long:
            assert practice.view.canvas.bbox("all")[3] > practice.view.canvas.winfo_height()
            practice.view.canvas.yview_moveto(1)
            app.root.update()
            assert practice.view.canvas.yview()[1] == 1
        # Footer belongs to the outer layout, so scrolling cannot hide it.
        assert_inside(practice.submit_button, practice)
        assert all(button.instate(["disabled"]) for button in practice.option_buttons)
    finally:
        cancel_timers(app.root)
        app.stop.set()
        app.root.destroy()


def test_dashboard_reads_existing_aggregates_through_worker_queue():
    from bagu_trainer.desktop import DesktopApp
    calls = []
    engine = SimpleNamespace(PROJECT_ROOT="C:/fallback",
        load_config=lambda: {"project_roots": ["C:/one", "C:/two"]},
        get_learning_stats=lambda: calls.append("read") or {"ok": True, "total_attempts": 4,
            "accuracy": .5, "due_reviews": 1})
    app = SimpleNamespace(monitor=SimpleNamespace(engine=engine), events=queue.Queue())
    DesktopApp._post_dashboard(app, "watching")
    name, snapshot = app.events.get_nowait()
    assert calls == ["read"] and name == "dashboard"
    assert snapshot["projects"] == ["C:/one", "C:/two"]
    assert snapshot["stats"]["total_attempts"] == 4


def test_single_line_message_height_is_not_expanded_by_scrollbar():
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.ui_theme import apply_theme, SPACE
    from bagu_trainer.ui_layout import MessageArea
    from bagu_trainer.ui_state import window_var
    root = create_root()
    try:
        root.tk.call("tk", "scaling", 2 * 96 / 72)
        theme = apply_theme(root)
        root.geometry("600x300")
        area = MessageArea(root, window_var(root, "需要滚动查看的说明。" * 100), theme, lines=1)
        area.pack(fill="x")
        root.update()
        assert area.scrollbar.winfo_ismapped()
        assert area.winfo_height() == area.text.winfo_reqheight() + 2 * theme.px(SPACE.md)
    finally:
        root.destroy()
