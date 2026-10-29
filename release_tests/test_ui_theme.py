"""Real Tk regressions for theme lifecycle, DPI layout and keyboard actions."""
import sys

import pytest
from release_tests.fakes import ui_monitor

from bagu_trainer.desktop.tk_runtime import prepare_tk
from bagu_trainer.desktop.tk_runtime import create_root
from bagu_trainer.desktop.ui_theme import apply_theme
from bagu_trainer.desktop.ui_theme import COLORS
from bagu_trainer.desktop.ui_theme import PRIMARY
from bagu_trainer.desktop.ui_theme import DANGER

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk UI")


def assert_inside(widget, window):
    window.update()
    assert widget.winfo_ismapped(), str(widget)
    x = widget.winfo_rootx() - window.winfo_rootx()
    y = widget.winfo_rooty() - window.winfo_rooty()
    assert x >= 0 and y >= 0
    assert x + widget.winfo_width() <= window.winfo_width()
    assert y + widget.winfo_height() <= window.winfo_height()
    assert widget.winfo_width() >= widget.winfo_reqwidth()


def cancel_timers(window):
    # Layout-only windows have no active workers; discard their scheduled polls
    # before destroying Tcl commands so later roots cannot run stale callbacks.
    for timer in window.tk.call("after", "info"):
        window.tk.call("after", "cancel", timer)


def test_fresh_interpreters_and_toplevels_keep_their_images_and_fonts():
    prepare_tk()
    import tkinter as tk
    from tkinter import ttk
    for _ in range(2):
        first = create_root()
        second = None
        try:
            original = apply_theme(first)
            assert apply_theme(tk.Toplevel(first)) is original
            second = create_root()
            another = apply_theme(second)
            assert another is not original
            for root in (first, second, first):
                theme = apply_theme(root)
                ttk.Combobox(root).pack()
                ttk.Radiobutton(root, text="中文选项").pack()
                button = ttk.Button(root, text="继续", style=PRIMARY)
                button.pack()
                root.update_idletasks()
                assert theme.style.theme_use() == "bagu-light"
                assert theme.fonts["body"].measure("中文学习") > 0
                assert theme.style.lookup(PRIMARY, "background") == COLORS.primary
                assert theme.style.lookup(PRIMARY, "background", ("disabled",)) == COLORS.disabled
                assert theme.style.lookup(DANGER, "background") == COLORS.danger
        finally:
            if second is not None:
                second.destroy()
            first.destroy()


def test_closed_model_panel_releases_callbacks_even_with_retained_variables():
    import gc
    import weakref
    from release_tests.fakes import make_model_panel
    panel = make_model_panel()
    root = panel.root
    variables = [panel.base_url, panel.model_name, panel.api_key, panel.timeout]
    assert all(variable.trace_info() for variable in variables)
    reference = weakref.ref(panel)
    panel.close()
    root.destroy()
    del panel
    gc.collect()
    assert all(not variable.trace_info() for variable in variables)
    assert reference() is None


def test_closed_variable_can_be_collected_by_worker_without_calling_tcl():
    import gc
    import threading
    from bagu_trainer.desktop.ui_state import window_var
    root = create_root()
    variable = window_var(root, "closed snapshot")
    holder = [variable]
    root.destroy()
    assert variable.get() == "closed snapshot"
    del variable
    failures = []
    def collect():
        try:
            value = holder.pop()
            assert value.get() == "closed snapshot"
            del value
            gc.collect()
        except Exception as error:
            failures.append(error)
    thread = threading.Thread(target=collect)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive() and not failures


@pytest.mark.parametrize("scale", [1, 1.25, 1.5])
def test_all_windows_keep_actions_visible_at_minimum_size(monkeypatch, scale):
    prepare_tk()
    import tkinter as tk
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.desktop.bank_setup import BankSetupWindow
    from release_tests.fakes import make_model_panel

    real_tk = tk.Tk
    def scaled_root():
        root = real_tk()
        root.tk.call("tk", "scaling", scale * 96 / 72)
        return root
    monkeypatch.setattr(tk, "Tk", scaled_root)
    bank_parent = create_root()
    bank_parent.withdraw()
    bank = BankSetupWindow(bank_parent)
    try:
        bank.root.geometry("%dx%d" % bank.root.minsize())
        assert_inside(bank.start_button, bank.root)
        assert_inside(bank.cancel_button, bank.root)
    finally:
        cancel_timers(bank.root)
        bank._close()
        bank_parent.destroy()
    settings = make_model_panel()
    try:
        settings.root.geometry("%dx%d" % settings.root.minsize())
        assert_inside(settings.save_button, settings.root)
        assert_inside(settings.test_button, settings.root)
    finally:
        settings.close()
        settings.root.destroy()

    app = DesktopApp(ui_monitor())
    try:
        app.root.geometry("%dx%d" % app.root.minsize())
        assert_inside(app.pause_button, app.root)
        quiz = {"quiz_id": "theme-only", "knowledge_point": "并发与缓存",
                "question": "较长的中文题干。" * 25,
                "options": {letter: "中文选项解释。" * 12 for letter in "ABCD"}}
        app._show_quiz(quiz)
        practice = app._answer_window()
        app._navigate_page("practice")
        practice.show()
        practice.geometry("%dx%d" % practice.minsize())
        app.root.update()
        practice.view.canvas.focus_force()
        practice.update()
        practice.view.canvas.event_generate("<KeyPress-b>")
        assert app.answer.get() == "B"
        practice.view.canvas.event_generate("<Control-Return>")
        assert app.jobs.get_nowait() == ("submit", ("theme-only", "B"))
        assert practice.submit_button.instate(["disabled"])
        # Long questions remain in the scrollable body rather than clipped.
        canvas = practice.view.canvas
        assert canvas.bbox("all")[3] > canvas.winfo_height()
        canvas.yview_moveto(1)
        assert_inside(practice.submit_button, practice)
    finally:
        cancel_timers(app.root)
        app.stop.set()
        app.root.destroy()
