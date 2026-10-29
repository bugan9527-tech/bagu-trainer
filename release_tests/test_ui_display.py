"""Display/keyboard regressions. Tk scale fixtures are NOT native DPI evidence."""
import sys
import time

import pytest
from release_tests.fakes import ui_monitor

from bagu_trainer.desktop.tk_runtime import prepare_tk
from release_tests.test_ui_theme import assert_inside, cancel_timers

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop Tk")


@pytest.fixture
def display_roots(monkeypatch):
    prepare_tk()
    import tkinter as tk
    import ttkbootstrap
    real_tk = tk.Tk
    def install(scale):
        def scaled():
            root = real_tk()
            root.tk.call("tk", "scaling", scale * 96 / 72)
            return root
        monkeypatch.setattr(tk, "Tk", scaled)
    return install


def focus_key(root, widget, key):
    root.deiconify()
    root.lift()
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
    assert root.focus_get() is widget, ("Requested keyboard target did not acquire native focus",
        str(widget), widget.winfo_ismapped(), widget.winfo_rooty(), root.winfo_geometry())
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
    from release_tests.fakes import make_model_panel
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.desktop.ui_theme import work_area
    mixed = "中文 English Transaction<T>，标点 UTF-8。" * 12
    model = "provider/LongUnbrokenModelName-235B-Preview" * 12 + "-MODEL-END"
    path = "C:/学习项目/My English Project/" + "很长的目录/Service.API/" * 25 + "PATH-END"
    settings = make_model_panel()
    try:
        settings.root.geometry("%dx%d" % settings.root.minsize())
        settings.model_name.set(model)
        entry = settings.entries[1]
        focus_key(settings.root, entry, "<End>")
        assert entry.index("insert") == len(model) and entry.xview()[0] > 0
        focus_key(settings.root, entry, "<Home>")
        assert entry.index("insert") == 0 and entry.xview()[0] == 0
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
        settings.close()
        settings.root.destroy()
    app = DesktopApp(ui_monitor())
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
        practice = app._answer_window()
        app._navigate_page("practice")
        practice.show()
        practice.geometry("%dx%d" % practice.minsize())
        app.root.update()
        narrow = practice.question_label.winfo_reqheight()
        practice.state("zoomed")
        app.root.update()
        assert practice.question_label.winfo_reqheight() < narrow
        practice.state("normal")
        practice.geometry("%dx%d" % practice.minsize())
        app.practice.apply_grade({"ok": True, "correct": True, "correct_answer": "B", "analysis": mixed * 8 + "ANALYSIS-END",
                                  "schedule": {"interval_days": 2}})
        focus_key(practice, practice.view.canvas, "<End>")
        assert practice.view.canvas.yview()[1] == 1, (app.root.focus_get(), practice.view.follow_end, practice.view.region)
        # Late wrapping/content growth must not undo an explicit End key.
        app.feedback.set(app.feedback.get() + "\n" + mixed)
        app.root.update()
        assert practice.view.canvas.yview()[1] == 1
        focus_key(practice, practice.view.canvas, "<Home>")
        assert practice.view.canvas.yview()[0] == 0
        assert "ANALYSIS-END" in app.feedback.get()
        assert_inside(practice.submit_button, practice)
        assert_inside(practice.dismiss_button, practice)
    finally:
        cancel_timers(app.root)
        app.stop.set()
        app.root.destroy()


def test_quiz_shortcuts_preserve_text_keys_and_require_intent(display_roots):
    display_roots(1)
    from bagu_trainer.desktop import DesktopApp
    from tkinter import ttk
    app = DesktopApp(ui_monitor())
    try:
        app._show_quiz({"quiz_id": "keyboard", "question": "中 English？", "options": {l: l for l in "ABCD"}})
        practice = app._answer_window()
        queue_page = app._page_frame("practice")
        app._navigate_page("practice")
        practice.show()
        app.root.update()
        canvas = practice.view.canvas
        focus_key(practice, canvas, "<Control-c>")
        focus_key(practice, canvas, "<Control-a>")
        focus_key(practice, canvas, "<Alt-b>")
        assert app.answer.get() == "" and app.jobs.empty()
        focus_key(practice, canvas, "<b>")
        assert app.answer.get() == "B"
        focus_key(practice, canvas, "<Return>")
        assert app.jobs.empty()
        focus_key(app.root, queue_page.pending_list, "<a>")
        focus_key(app.root, queue_page.pending_list, "<Control-Return>")
        entry = ttk.Entry(practice.view.body)
        entry.grid()
        focus_key(practice, entry, "<a>")
        focus_key(practice, entry, "<Control-Return>")
        assert app.answer.get() == "B" and app.jobs.empty()
        focus_key(practice, canvas, "<Control-Return>")
        assert app.jobs.get_nowait() == ("submit", ("keyboard", "B"))
        focus_key(practice, canvas, "<Control-Return>")
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
    from bagu_trainer.desktop import ui_theme
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
