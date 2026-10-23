"""Persistent shell regressions, using isolated SQLite and synthetic snapshots."""
import json
import sys
import threading

import pytest

from release_tests.test_answer_history import history, seed, desktop, dispose, pump, dump
from release_tests.test_first_use import installation
from bagu_trainer.tk_runtime import create_root
from bagu_trainer.ui_theme import apply_theme, work_area
from bagu_trainer.ui_window import WindowPlacement

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk shell")


def test_history_roundtrip_preserves_filters_page_selection_scroll_and_worker(history):
    engine, store, calls = history
    for number in range(65):
        seed(store, number)
    before = dump(store)
    app, polls, errors = desktop(history)
    app.monitor.initialized.add("unchanged-baseline")
    app._start_worker()
    try:
        app.root.geometry("560x560")
        app.shell.buttons["history"].invoke()
        page = app.history_page
        pump(app, lambda: page.total == 65)
        page.keyword.set("缓存")
        page.result.current(1)
        page.refresh()
        pump(app, lambda: "共 65 条" in page.message.get())
        page.turn(1)
        pump(app, lambda: page.page == 1 and "第 2 /" in page.message.get())
        selected = page.tree.get_children()[6]
        page.tree.selection_set(selected)
        page.tree.focus(selected)
        page.tree.yview_moveto(.35)
        page.tree.focus_force()
        app.root.update()
        scroll = page.tree.yview()[0]
        geometry, worker = app.root.geometry(), app.worker_thread
        token = page.token
        app.shell.buttons["dashboard"].invoke()
        app.root.update()
        assert app.history_page is None and not page.closed
        assert page.saved_focus is page.tree
        assert app.shell.current_page == "dashboard"
        for name in ("projects", "model", "bank", "settings", "practice"):
            app._navigate_page(name)
            app.root.update()
        app.shell.buttons["history"].invoke()
        app.root.update()
        assert app.history_page is page and page.token == token
        assert app.root.geometry() == geometry
        assert page.page == 1 and page.keyword.get() == "缓存" and page.result.current() == 1
        assert page.tree.selection() == (selected,) and page.tree.focus() == selected
        assert abs(page.tree.yview()[0] - scroll) < .06
        # An inactive Windows toplevel may report no system focus. It must
        # still remember the restored target without activating another app.
        assert app.root.focus_lastfor() is page.tree
        if app.root.focus_get() is not None:
            assert app.root.focus_get() is page.tree
        assert app.worker_thread is worker and worker.is_alive() and not app.stop.is_set()
        assert app.monitor.initialized == {"unchanged-baseline"}
        assert app.shell.buttons["history"].cget("style") == "Selected.Navigation.TButton"
        assert dump(store) == before and not calls and polls and not errors
    finally:
        dispose(app)


def test_history_detail_reflows_and_escape_keeps_shell(history):
    _, store, calls = history
    seed(store)
    app, _, errors = desktop(history)
    try:
        # A 1024px Windows runner needs the collapsed rail to leave the 900px
        # detail breakpoint available. The wide/narrow assertions stay real.
        app.shell.toggle_collapse()
        app.root.geometry("%dx%d" % app.theme.px((1280, 820)))
        app._open_history()
        page = app.history_page
        pump(app, lambda: bool(page.tree.get_children()))
        page.open_selected()
        pump(app, lambda: hasattr(page, "detail_view") and page.wide)
        assert page.list_frame.winfo_ismapped() and page.detail_frame.winfo_ismapped()
        assert page.detail_frame.grid_info()["column"] == 1
        app.root.geometry("560x560")
        app.root.update()
        assert not page.wide and not page.list_frame.winfo_ismapped()
        assert page.detail_frame.winfo_ismapped() and page.detail_frame.grid_info()["column"] == 0
        page.detail_view.canvas.focus_force()
        app.root.update()
        page.detail_view.canvas.event_generate("<Escape>")
        app.root.update()
        assert page.detail_id is None and page.list_frame.winfo_ismapped()
        assert app.root.winfo_exists() and app.shell.winfo_ismapped()
        assert app.root.focus_get() is page.tree and not calls and not errors
    finally:
        dispose(app)


def test_hidden_detail_receives_result_without_stealing_focus(history, monkeypatch):
    engine, store, calls = history
    seed(store)
    entered, release = threading.Event(), threading.Event()
    original = engine.get_answer_detail
    def slow_detail(**arguments):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(5)
        return original(**arguments)
    monkeypatch.setattr(engine, "get_answer_detail", slow_detail)
    app, _, errors = desktop(history)
    try:
        app._open_history()
        page = app.history_page
        pump(app, lambda: bool(page.tree.get_children()))
        page.open_selected()
        assert entered.wait(2)
        app._return_from_history()
        app._navigate_page("model")
        app.root.update()
        app.shell.heading.focus_force()
        release.set()
        pump(app, lambda: hasattr(page, "detail_view"))
        assert app.shell.current_page == "model" and not page.visible
        assert app.root.focus_get() is app.shell.heading
        page.receive("wrong-page", page.token, "detail", {"ok": True})
        page.receive(page.page_id, page.token - 1, "detail", {"ok": True})
        app._open_history()
        app.root.update()
        assert app.history_page is page and page.detail_id is not None
        assert page.detail_frame.winfo_ismapped() and not calls and not errors
    finally:
        release.set()
        dispose(app)


def test_cached_history_refreshes_after_grade_without_resetting_filters(history):
    _, store, _ = history
    for number in range(45):
        seed(store, number)
    app, _, errors = desktop(history)
    try:
        app._open_history()
        page = app.history_page
        pump(app, lambda: page.total == 45)
        page.keyword.set("缓存")
        page.refresh()
        pump(app, lambda: page.total == 45 and "正在查询" not in page.message.get())
        page.turn(1)
        pump(app, lambda: page.page == 1 and "第 2 /" in page.message.get())
        selected = page.tree.get_children()[5]
        page.tree.selection_set(selected)
        app._return_from_history()
        seed(store, 99)
        app.events.put(("grade", {"quiz_id": "completed", "answer": "A", "grade": {"ok": True}}))
        app._drain()
        assert page.dirty
        app._open_history()
        pump(app, lambda: page.total == 46)
        assert page.page == 1 and page.keyword.get() == "缓存"
        assert page.tree.selection() == (selected,) and not errors
    finally:
        dispose(app)


def test_settings_pages_mount_real_panels_without_stopping_worker(installation, monkeypatch):
    from release_tests.test_dashboard_navigation import dashboard, dispose as dispose_dashboard
    app, _ = dashboard(installation, monkeypatch)
    app.monitor.initialized.add("baseline-kept")
    app._start_worker()
    app.root.deiconify()
    try:
        app.root.geometry("1000x700")
        app.root.update()
        geometry = app.root.geometry()
        worker = app.worker_thread
        for name in ("model", "bank", "projects", "settings"):
            app.shell.buttons[name].invoke()
            app.root.update()
            assert app.shell.current_page == name and app.shell.current_frame is app._cached_pages[name]
            # Ordinary page switches must not stop the monitor or rebuild baselines.
            assert not app.stop.is_set() and app.worker_thread is worker and worker.is_alive()
            assert app.monitor.initialized == {"baseline-kept"}
            assert app.shell.current_page == name and app.shell.winfo_ismapped()
            assert app.root.geometry() == geometry
            assert app.shell.current_frame is app._cached_pages[name]
        app.shell.buttons["dashboard"].invoke()
        app.root.update()
        assert app.shell.current_page == "dashboard" and app.root.geometry() == geometry
    finally:
        dispose_dashboard(app)


def test_window_metadata_restores_clamps_and_ignores_corruption(tmp_path):
    root = create_root()
    # These assertions concern physical window metadata; isolate the Tcl
    # display scaling left by earlier 125/150/200% simulation fixtures.
    root.tk.call("tk", "scaling", 96 / 72)
    theme = apply_theme(root)
    path = tmp_path / "window.json"
    try:
        theme.window(root, (1200, 800), (560, 560))
        placement = WindowPlacement(root, theme, path)
        root.geometry("800x600+40+50")
        root.update()
        placement.save()
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record == {"normal": [800, 600, 40, 50], "maximized": False}
        root.geometry("900x650+80+90")
        root.update()
        placement.restore()
        root.update()
        assert root.winfo_width() == 800 and root.winfo_height() == 600
        root.state("zoomed")
        root.update()
        placement.save()
        assert json.loads(path.read_text(encoding="utf-8"))["maximized"] is True
        root.state("normal")
        root.update()
        placement.restore()
        root.update()
        assert root.state() == "zoomed"
        root.iconify()
        root.update()
        placement.save()
        assert json.loads(path.read_text(encoding="utf-8"))["maximized"] is True
        root.state("normal")
        path.write_text(json.dumps({"normal": [99999, 99999, -99999, -99999]}), encoding="utf-8")
        placement.restore()
        root.update()
        left, top, right, bottom = work_area(root)
        assert left <= root.winfo_x() and top <= root.winfo_y()
        assert root.winfo_x() + root.winfo_width() <= right
        assert root.winfo_y() + root.winfo_height() <= bottom
        for value in ("broken json", "null", "{}", '{"normal":[true,1,2,3]}'):
            path.write_text(value, encoding="utf-8")
            placement.restore()
    finally:
        root.destroy()


def test_all_seven_routes_use_one_shell_and_mount_real_pages(history):
    _, store, calls = history
    seed(store)
    before = dump(store)
    app, polls, errors = desktop(history)
    app.monitor.initialized.add("baseline-kept")
    app._start_worker()
    try:
        app.root.update()
        geometry, root, shell, worker = app.root.geometry(), app.root, app.shell, app.worker_thread
        for name, _ in (*app.shell.PAGES, ("dashboard", "工作台")):
            app.shell.buttons[name].invoke()
            app.root.update()
            assert app.root is root and app.shell is shell
            assert app.shell.current_page == name
            assert app.shell.current_frame.master is app.content_host
            assert app.root.geometry() == geometry and app.shell.winfo_ismapped()
            assert app.shell.buttons[name].cget("style") == "Selected.Navigation.TButton"
            assert all(not button.instate(["disabled"]) for button in app.shell.buttons.values())
            assert "迁移中" not in app.shell.buttons[name].cget("text")
            assert "迁移中" not in app.shell.heading.cget("text")
            if name not in {"dashboard", "history"}:
                expected = {"practice": "PracticePage", "projects": "ProjectsPanel", "bank": "BankPanel",
                            "model": "ModelPanel", "settings": "SettingsPage"}
                assert type(app.shell.current_frame).__name__ == expected[name]
            assert not any(child.winfo_class() == "Toplevel" for child in app.root.winfo_children())
            assert app.monitor.delivery_paused.is_set() == (name not in ("dashboard", "practice"))
            assert app.worker_thread is worker and worker.is_alive() and not app.stop.is_set()
            assert app.monitor.initialized == {"baseline-kept"}
        assert dump(store) == before and not calls and polls and not errors
    finally:
        dispose(app)


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_sidebar_keyboard_and_real_pages_at_four_tk_scales(history, monkeypatch, scale):
    from bagu_trainer.tk_runtime import prepare_tk
    prepare_tk()
    import tkinter as tk
    import ttkbootstrap
    original_tk = tk.Tk
    def scaled_root():
        root = original_tk()
        root.tk.call("tk", "scaling", scale * 96 / 72)
        return root
    monkeypatch.setattr(tk, "Tk", scaled_root)
    app, _, errors = desktop(history)
    try:
        app.root.geometry("%dx%d" % app.root.minsize())
        app.root.update()
        button = app.shell.buttons["dashboard"]
        button.focus_force()
        app.root.update()
        button.event_generate("<Down>")
        app.root.update()
        practice = app.shell.buttons["practice"]
        assert app.root.focus_get() is practice and app.shell.current_page == "dashboard"
        practice.event_generate("<Return>")
        app.root.update()
        assert app.shell.current_page == "practice"
        assert app._page_frame("practice").view.canvas.winfo_height() >= app.theme.px(96)
        practice.focus_force()
        app.root.update()
        practice.event_generate("<Tab>")
        app.root.update()
        assert app.root.focus_get() is app.shell.buttons["history"]
        app.shell.buttons["history"].event_generate("<End>")
        app.root.update()
        assert app.root.focus_get() is app.shell.buttons["settings"]
        app.shell.buttons["settings"].event_generate("<Home>")
        app.root.update()
        assert app.root.focus_get() is button
        for name in ("projects", "bank", "model", "settings"):
            print(f"Rendering {name} at scale {scale}", flush=True)
            app._navigate_page(name)
            app.root.update()
            assert app.shell.current_frame.winfo_ismapped()
            assert app.shell.current_frame.winfo_width() > 1 and app.shell.current_frame.winfo_height() > 1
        assert not errors
    finally:
        dispose(app)


def test_new_navigation_preserves_draft_defers_each_id_and_submits_once(history):
    engine, store, calls = history
    attempt = seed(store)
    first = engine.repractice_attempt(attempt)["quiz"]
    second = engine.repractice_attempt(seed(store, 2))["quiz"]
    app, _, errors = desktop(history)
    try:
        app._show_quiz(first)
        app.answer.set("B")
        for name in ("projects", "model", "history", "practice"):
            app._navigate_page(name)
            app.root.update()
            assert app.practice.quiz_id == first["quiz_id"] and app.answer.get() == "B"
        for item in (second, second, first):
            app._show_quiz(item)
        app.root.update()
        assert [item["quiz_id"] for item in app.practice.deferred] == [second["quiz_id"]]
        assert app.shell.current_page == "practice"
        app.root.focus_force()
        app.root.update()
        app.root.event_generate("<Control-r>")
        app.root.update()
        assert app.shell.current_page == "practice" and app.answer.get() == "B"
        assert app.practice.submit() is True
        assert app.practice.submit() is False  # duplicate guarded
        assert app.jobs.qsize() == 1
        app._start_worker()
        pump(app, lambda: app.practice.submit_state == "finished")
        assert store.attempt_count(first["quiz_id"]) == 1
        assert app.practice.submit() is False  # finished: no resubmit
        assert store.attempt_count(first["quiz_id"]) == 1
        app.practice.dismiss()
        assert app.practice.quiz_id == second["quiz_id"] and app.practice.submit_state == "ready"
        assert store.attempt_count(second["quiz_id"]) == 0 and not calls and not errors
    finally:
        dispose(app)


def test_settings_race_retains_first_and_additional_deliveries(history):
    # Renamed behaviour: switching pages no longer pauses the monitor or uses a
    # dedicated settings slot.  New deliveries while browsing a non-dashboard
    # page are deduped by quiz id and queued, then delivered on return.
    from release_tests.test_interaction_states import quiz
    app, _, errors = desktop(history)
    try:
        app._show_quiz(quiz("active"))
        app.answer.set("C")
        app._navigate_page("model")
        for identifier in ("first", "second", "first", "second", "active"):
            app._show_quiz(quiz(identifier))
        assert [item["quiz_id"] for item in app.practice.deferred] == ["first", "second"]
        app._navigate_page("practice")
        app.root.update()
        assert app.practice.quiz_id == "active" and app.answer.get() == "C"
        assert not errors
    finally:
        dispose(app)


def test_nav_icons_and_collapse_keep_text_hints(history):
    from bagu_trainer.ui_icons import make_icon
    app, _, errors = desktop(history)
    try:
        shell = app.shell
        # Every nav button carries a generated line icon (not emoji/text).
        for name in shell.buttons:
            image = shell.buttons[name].cget("image")
            assert image, name
            assert not any(ch in shell.buttons[name].cget("text") for ch in "▎►●◆")
        assert shell.buttons["practice"].cget("text") == "练习中心"
        assert not shell.collapsed
        shell.set_collapsed(True)
        app.root.update()
        assert shell.collapsed
        assert shell.buttons["practice"].cget("text") == ""  # icon-only rail
        for name, button in shell.buttons.items():
            assert button.winfo_width() >= button.winfo_reqwidth(), name
        assert shell.brand_mark.winfo_ismapped() and not shell.brand.winfo_ismapped()
        # Hover restores the text hint.
        shell._show_hint("practice")
        app.root.update_idletasks()
        assert shell.hover_hint.winfo_ismapped() and "练习中心" in shell.hover_hint.cget("text")
        shell._clear_hint()
        app.root.update_idletasks()
        assert not shell.hover_hint.winfo_ismapped()
        # Expand restores the full labels.
        shell.set_collapsed(False)
        assert shell.buttons["practice"].cget("text") == "练习中心"
        assert not errors
    finally:
        dispose(app)
