"""Display/keyboard regressions. Tk scale fixtures are NOT native DPI evidence."""
import queue
import sys
import time
from types import SimpleNamespace

import pytest

from bagu_trainer.tk_runtime import prepare_tk
from release_tests.test_ui_theme import assert_inside, cancel_timers

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop Tk")


@pytest.fixture
def display_roots(monkeypatch):
    prepare_tk()
    import tkinter as tk
    import ttkbootstrap
    from bagu_trainer.first_use import FirstUseWizard
    real_tk = tk.Tk
    monkeypatch.setattr(FirstUseWizard, "_restore", lambda self: None)
    def install(scale):
        def scaled():
            root = real_tk()
            root.tk.call("tk", "scaling", scale * 96 / 72)
            return root
        monkeypatch.setattr(tk, "Tk", scaled)
    return install


def focus_key(root, widget, key):
    root.update_idletasks()
    widget.focus_force()
    # Maximize/restore dispatches native focus events asynchronously. Deliver a
    # synthetic key only after the intended descendant owns stable focus.
    deadline = time.monotonic() + 2
    stable = 0
    while time.monotonic() < deadline and stable < 3:
        root.update()
        if root.focus_get() is widget:
            stable += 1
        else:
            stable = 0
            widget.focus_force()
        time.sleep(.02)
    assert root.focus_get() is widget, "Requested keyboard target did not acquire native focus"
    widget.event_generate(key)
    root.update()
    return root.focus_get()


def assert_in_view(widget, view):
    view.update()
    assert view.canvas.winfo_rooty() <= widget.winfo_rooty()
    assert widget.winfo_rooty() + widget.winfo_height() <= view.canvas.winfo_rooty() + view.canvas.winfo_height()


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2], ids=["tk100", "tk125", "tk150", "tk200"])
def test_mixed_text_resize_focus_and_scroll(display_roots, scale):
    display_roots(scale)
    from bagu_trainer.first_use import FirstUseWizard
    from bagu_trainer.settings_window import ModelSettingsWindow
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.ui_theme import work_area
    mixed = "中文 English Transaction<T>，标点 UTF-8。" * 12
    model = "provider/LongUnbrokenModelName-235B-Preview" * 12 + "-MODEL-END"
    path = "C:/学习项目/My English Project/" + "很长的目录/Service.API/" * 25 + "PATH-END"
    wizard = FirstUseWizard(SimpleNamespace(events=queue.Queue()))
    try:
        wizard.root.geometry("%dx%d" % wizard.root.minsize())
        wizard._show("model")
        wizard.model_name.set(model)
        wizard.status.set(mixed)
        assert focus_key(wizard.root, wizard.model_entries[0], "<Tab>") == wizard.model_entries[1]
        assert_in_view(wizard.model_entries[1], wizard.model_view.form_view)
        assert focus_key(wizard.root, wizard.model_entries[1], "<Shift-Tab>") == wizard.model_entries[0]
        assert_inside(wizard.start_button, wizard.root)
        assert_inside(wizard.back_button, wizard.root)
        # Both ends of a long single-line model remain editable/reachable.
        entry = wizard.model_entries[1]
        focus_key(wizard.root, entry, "<End>")
        assert entry.index("insert") == len(model)
        assert entry.xview()[0] > 0
        focus_key(wizard.root, entry, "<Home>")
        assert entry.index("insert") == 0
        assert entry.xview()[0] == 0
        wizard._show("bank")
        wizard.bank_view.reveal(wizard.bank_setup.start_button)
        assert_inside(wizard.bank_setup.start_button, wizard.root)
        assert_inside(wizard.bank_setup.cancel_button, wizard.root)
        wizard._show("projects")
        wizard.project_list.insert("end", path)
        wizard.root.update()
        wizard.project_list.xview_moveto(1)
        assert wizard.project_list.xview()[0] > 0
        for button in wizard.project_buttons:
            assert_inside(button, wizard.root)
    finally:
        cancel_timers(wizard.root)
        wizard._close()
    settings = ModelSettingsWindow()
    try:
        settings.root.geometry("%dx%d" % settings.root.minsize())
        settings.model_var.set(model)
        for index in range(len(settings.entries) - 1):
            settings.form_view.reveal(settings.entries[index])
            assert focus_key(settings.root, settings.entries[index], "<Tab>") == settings.entries[index + 1]
            assert_in_view(settings.entries[index + 1], settings.form_view)
            assert settings.form_view.cget("style") != "Focus.TFrame"
        settings.status.set(mixed + "STATUS-END")
        message = settings.status_area.text
        expected = message.tk_focusNext()
        assert focus_key(settings.root, message, "<Tab>") == expected
        assert focus_key(settings.root, expected, "<Shift-Tab>") == message
        focus_key(settings.root, message, "<Control-a>")
        assert message.get("sel.first", "sel.last") == mixed + "STATUS-END"
        assert int(message.cget("highlightthickness")) >= 2
        for size in (settings.root.minsize(), settings.theme.px((950, 800)), settings.root.minsize()):
            settings.root.geometry("%dx%d" % size)
            settings.root.update()
            for button in (settings.test_button, settings.save_button, settings.cancel_button):
                assert_inside(button, settings.root)
        assert settings.verified is None and settings.save_button.instate(["disabled"])
    finally:
        cancel_timers(settings.root)
        settings._close()
    app = DesktopApp(SimpleNamespace(paused=False))
    try:
        app.root.geometry("%dx%d" % app.root.minsize())
        app._render_dashboard({"state": "watching", "projects": [path], "stats": {"ok": True,
            "total_attempts": 123456, "accuracy": .875, "due_reviews": 123}})
        app.root.update()
        canvas = app.dashboard_view.canvas
        focus_key(app.root, canvas, "<End>")
        assert canvas.yview()[1] == 1
        assert app.dashboard_view.cget("style") == "Focus.TFrame"
        assert_inside(app.resume_button, app.root)
        quiz = {"quiz_id": "display", "knowledge_point": "中文 English Cache consistency",
                "project_root": path, "question": mixed * 5, "options": {l: model + mixed for l in "ABCD"}}
        app._show_quiz(quiz)
        practice = app._page_frame("practice")
        app._navigate_page("practice")
        app.root.update()
        narrow = practice.question_label.winfo_reqheight()
        app.root.state("zoomed")
        app.root.update()
        assert practice.question_label.winfo_reqheight() < narrow
        app.root.state("normal")
        app.root.geometry("%dx%d" % app.root.minsize())
        app.practice.apply_grade({"ok": True, "correct": True, "correct_answer": "B", "analysis": mixed * 8 + "ANALYSIS-END",
                                  "schedule": {"interval_days": 2}})
        focus_key(app.root, practice.view.canvas, "<End>")
        assert practice.view.canvas.yview()[1] == 1, (app.root.focus_get(), practice.view.follow_end, practice.view.region)
        # Late wrapping/content growth must not undo an explicit End key.
        app.feedback.set(app.feedback.get() + "\n" + mixed)
        app.root.update()
        assert practice.view.canvas.yview()[1] == 1
        focus_key(app.root, practice.view.canvas, "<Home>")
        assert practice.view.canvas.yview()[0] == 0
        assert "ANALYSIS-END" in app.feedback.get()
        assert_inside(practice.submit_button, app.root)
        assert_inside(practice.dismiss_button, app.root)
    finally:
        cancel_timers(app.root)
        app.stop.set()
        app.root.destroy()


def test_quiz_shortcuts_preserve_text_keys_and_require_intent(display_roots):
    display_roots(1)
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.ui_layout import MessageArea
    app = DesktopApp(SimpleNamespace(paused=False))
    try:
        app._show_quiz({"quiz_id": "keyboard", "question": "中 English？", "options": {l: l for l in "ABCD"}})
        practice = app._page_frame("practice")
        app._navigate_page("practice")
        app.root.update()
        focus_key(app.root, practice, "<Control-c>")
        focus_key(app.root, practice, "<Control-a>")
        focus_key(app.root, practice, "<Alt-b>")
        assert app.answer.get() == "" and app.jobs.empty()
        focus_key(app.root, practice, "<b>")
        assert app.answer.get() == "B"
        focus_key(app.root, practice, "<Return>")
        assert app.jobs.empty()
        focus_key(app.root, practice.pending_list, "<a>")
        focus_key(app.root, practice.pending_list, "<Control-Return>")
        focus_key(app.root, practice.dismiss_button, "<Control-Return>")
        assert app.answer.get() == "B" and app.jobs.empty()
        focus_key(app.root, practice, "<Control-Return>")
        assert app.jobs.get_nowait() == ("submit", ("keyboard", "B"))
        focus_key(app.root, practice, "<Control-Return>")
        assert app.jobs.empty() and practice.submit_button.instate(["disabled"])
        app.practice.apply_grade({"ok": True, "correct": True, "correct_answer": "B", "analysis": "解释"})
        assert practice.dismiss_button.instate(["!disabled"])
    finally:
        cancel_timers(app.root)
        app.stop.set()
        app.root.destroy()


def test_chinese_font_fallback_uses_installed_candidate(display_roots, monkeypatch):
    display_roots(1)
    import tkinter as tk
    from tkinter import font
    from bagu_trainer import ui_theme
    root = tk.Tk()
    try:
        available = set(font.families(root))
        candidate = next((name for name in ("Noto Sans SC", "SimHei", "Microsoft YaHei") if name in available), None)
        if candidate is None:
            pytest.skip("No secondary Chinese font installed")
        monkeypatch.setattr(ui_theme, "FONT_FAMILIES", ("Unavailable Chinese Font", candidate))
        theme = ui_theme.apply_theme(root)
        assert theme.fonts["body"].actual("family") == candidate
        assert theme.fonts["body"].measure("中文 English 混排") > 0
        # Glyph quality still needs the fallback screenshot to be inspected.
    finally:
        root.destroy()
