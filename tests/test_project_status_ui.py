"""Real Tk/project queue integration on an isolated test desktop."""
import gc
import threading
import time
from pathlib import Path

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.application.learning import engine
from bagu_trainer.desktop import startup
from bagu_trainer.desktop import DesktopApp
from bagu_trainer.application.monitor import DesktopMonitor


def pump(app, check, timeout=3):
    end = time.perf_counter() + timeout
    tick = threading.Event()
    while not check() and time.perf_counter() < end:
        app.root.update()
        tick.wait(.001)
    assert check()


def status(panel, path):
    from bagu_trainer.desktop.projects_panel import _key
    return panel.tree.set(panel._row_ids[_key(path)], "status")


@pytest.fixture
def status_app(env, tmp_path, monkeypatch):
    gc.collect()
    roots = [str(tmp_path / "project-a"), str(tmp_path / "project-b")]
    for root in roots:
        Path(root).mkdir()
    env["cfg"]["project_roots"] = roots[:1]
    monkeypatch.setattr(config, "load_config", lambda: dict(env["cfg"]))
    monkeypatch.setattr(startup, "set_projects", lambda paths: env["cfg"].update(project_roots=list(paths)))
    monkeypatch.setattr(engine, "observe_project", lambda *_a, **_k: {"context": ""})
    monitor = DesktopMonitor(engine)
    monitor.apply_projects(roots[:1])
    app = DesktopApp(monitor)
    app.root.withdraw()
    app._navigate_page("projects")
    panel = app._page_frame("projects")
    app.root.update()
    try:
        yield app, panel, monitor, roots
    finally:
        app.stop.set()
        if app.worker_thread:
            app.worker_thread.join(5)
            assert not app.worker_thread.is_alive()
        from release_tests.test_ui_theme import cancel_timers
        cancel_timers(app.root)
        app.root.destroy()
        gc.collect()


def test_draft_save_acknowledgement_baseline_and_watching(status_app, monkeypatch):
    app, panel, monitor, (a, b) = status_app
    monitor.poll()
    app._drain()
    panel.projects.append(b)
    panel._mark_dirty()
    assert status(panel, a) == "监听中" and status(panel, b) == "待应用"
    panel._save()
    assert status(panel, b) == "应用中"  # Config persistence is not acknowledgement.
    assert panel._state(a)["last_success_at"] is not None
    entered, release = threading.Event(), threading.Event()
    def observe(root, **_):
        if root == engine._normalize_project_root(b):
            entered.set()
            assert release.wait(3)
        return {"context": ""}
    monkeypatch.setattr(engine, "observe_project", observe)
    app._start_worker()
    try:
        assert entered.wait(1)
        app._drain()
        assert status(panel, b) == "初始化中"
        assert panel._state(b)["last_success_at"] is None
        release.set()
        pump(app, lambda: status(panel, b) == "监听中")
        assert panel._state(b)["last_success_at"] is not None
    finally:
        release.set()


def test_pause_resume_and_immediate_cached_page_return(status_app):
    app, panel, monitor, (a, _) = status_app
    monitor.poll()
    app._drain()
    app._navigate_page("dashboard")
    monitor.set_paused(True)
    app._drain()
    app._navigate_page("projects")
    assert status(panel, a) == "已暂停"
    monitor.set_paused(False)
    app._drain()
    assert status(panel, a) == "初始化中"
    monitor.poll()
    app._drain()
    assert status(panel, a) == "监听中"


def test_save_and_application_failure_are_distinct_and_retryable(status_app, monkeypatch):
    app, panel, monitor, (a, b) = status_app
    monitor.poll()
    app._drain()
    panel.projects.append(b)
    panel._mark_dirty()
    save = startup.set_projects
    with monkeypatch.context() as failure:
        def fail_save(_):
            raise OSError("secret-write-body")
        failure.setattr(startup, "set_projects", fail_save)
        panel._save()
    assert "secret-write-body" not in panel.hint.get() and "保存失败" in panel.hint.get()
    assert status(panel, b) == "待应用" and app.jobs.empty()
    setter = engine.session.set_watch_roots
    def fail_apply(_):
        raise OSError("secret-apply-body")
    monkeypatch.setattr(engine.session, "set_watch_roots", fail_apply)
    panel._save()
    app._start_worker()
    pump(app, lambda: bool(panel.apply_error))
    assert status(panel, a) == "监听中" and status(panel, b) == "未就绪"
    assert engine._normalize_project_root(a) in panel.hint.get() and "secret-apply-body" not in panel.hint.get()
    assert monitor.project_snapshot()["applied_roots"] == [engine._normalize_project_root(a)]
    monkeypatch.setattr(engine.session, "set_watch_roots", setter)
    panel.save_button.invoke()
    pump(app, lambda: not panel.apply_error and panel.applying_request is None)
    assert set(monitor.project_snapshot()["applied_roots"]) == {engine._normalize_project_root(a), engine._normalize_project_root(b)}


def test_incremental_refresh_preserves_draft_selection_focus_and_scroll(status_app, monkeypatch):
    app, panel, monitor, (a, _) = status_app
    panel.projects += [str(Path(a) / f"unsaved-{i:02d}") for i in range(45)]
    panel._mark_dirty()
    app.root.deiconify()
    app.root.geometry("1250x800")
    app.root.update()
    iid = panel.tree.get_children()[25]
    panel.tree.selection_set(iid)
    panel.tree.focus(iid)
    panel.tree.yview_moveto(.4)
    app.root.update()
    original = (list(panel.projects), panel.tree.selection(), panel.tree.focus(), panel.tree.yview())
    mutations = []
    for name in ("insert", "delete", "move"):
        actual = getattr(panel.tree, name)
        def record(*args, _name=name, _actual=actual, **kwargs):
            mutations.append(_name)
            return _actual(*args, **kwargs)
        monkeypatch.setattr(panel.tree, name, record)
    monitor.poll()
    app._drain()
    app._navigate_page("dashboard")
    app._navigate_page("projects")
    app.root.update()
    assert original == (panel.projects, panel.tree.selection(), panel.tree.focus(), panel.tree.yview())
    assert panel.dirty and mutations == []


def test_removed_rows_ignore_old_snapshots_and_auto_pruning_preserves_other_edits(status_app):
    app, panel, monitor, (a, b) = status_app
    monitor.poll()
    old = monitor.project_snapshot()
    panel.projects.append(b)
    panel._mark_dirty()
    monitor.apply_projects([])
    app.events.put(("projects_pruned", ([a], [])))
    app._drain()
    app.events.put(("project_states", old))
    app._drain()
    assert panel.projects == [b] and panel.dirty and status(panel, b) == "待应用"
    assert len(panel.tree.get_children()) == 1


def test_snapshot_changes_only_one_cell_and_detail_uses_full_path(status_app, monkeypatch):
    app, panel, monitor, (a, _) = status_app
    monitor.poll()
    app._drain()
    iid = panel.tree.get_children()[0]
    panel.tree.selection_set(iid)
    panel._selection_changed()
    assert a in panel.detail.get() and "北京时间" in panel.detail.get()
    actual = panel.tree.set
    changed = []
    def set_cell(item, column=None, value=None):
        if value is not None:
            changed.append((item, column, value))
        return actual(item, column, value)
    monkeypatch.setattr(panel.tree, "set", set_cell)
    snapshot = monitor.project_snapshot()
    with monkeypatch.context() as offline:
        import os
        def no_filesystem(*_):
            raise AssertionError("UI status refresh resolved a filesystem path")
        offline.setattr(os.path, "realpath", no_filesystem)
        panel.set_runtime_snapshot(snapshot)
    assert changed == []
    monitor.set_paused(True)
    app._drain()
    assert changed == [(iid, "status", "已暂停")]


def test_screenshot_real_projects_page_on_hidden_desktop(status_app, monkeypatch):
    app, panel, monitor, (a, b) = status_app
    # Display actual monitor facts plus explicitly identified draft states.
    monitor.poll()
    app._drain()
    panel.projects.append(b)
    panel._mark_dirty()
    iid = panel.tree.get_children()[0]
    panel.tree.selection_set(iid)
    panel._selection_changed()
    app.root.deiconify()
    app.root.geometry("1300x800")
    app.root.update()
    from release_tests.window_capture import capture_window
    output = Path(__file__).resolve().parents[1] / "validation/project-status"
    output.mkdir(parents=True, exist_ok=True)
    capture_window(app.root, output / "projects-status.png")
    panel._save()
    monitor.apply_projects([a, b])
    panel.application_finished(panel.applying_request, [a, b], monitor.project_snapshot())
    def check(root, **_):
        if root == engine._normalize_project_root(b):
            raise PermissionError("private body never displayed")
        return {"context": ""}
    monkeypatch.setattr(engine, "observe_project", check)
    monitor.poll()
    app._drain()
    from bagu_trainer.desktop.projects_panel import _key
    panel.tree.selection_set(panel._row_ids[_key(b)])
    panel._selection_changed()
    assert status(panel, b) == "目录不可用" and "尚未检查" in panel.detail.get()
    capture_window(app.root, output / "projects-unavailable.png")
    app.root.geometry("650x800")
    app.root.update()
    assert not panel.wide
    assert panel.detail_text.winfo_width() <= panel.workspace.winfo_width()
    assert b in panel.detail_text.get("1.0", "end-1c")
    capture_window(app.root, output / "projects-narrow.png")


def test_old_application_ack_cannot_finish_a_newer_request(status_app):
    app, panel, monitor, (a, b) = status_app
    panel.projects.append(b)
    panel._mark_dirty()
    panel._save()
    first = panel.applying_request
    panel.projects.remove(b)
    panel._mark_dirty()
    panel._save()
    second = panel.applying_request
    assert first != second
    panel.application_finished(first, [a, b], monitor.project_snapshot())
    assert panel.applying_request == second and status(panel, a) == "应用中"
    panel.application_finished(second, [a], monitor.project_snapshot())
    assert panel.applying_request is None


@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_standalone_panel_dpi_keyboard_and_responsive_layout(status_app, scale):
    from dataclasses import replace
    from bagu_trainer.desktop.projects_panel import ProjectsPanel
    app, _, _, (a, _) = status_app
    theme = replace(app.theme, scale=scale)
    panel = ProjectsPanel(app.content_host, theme)
    assert int(panel.tree.column("status", "width")) == theme.px(120)
    assert not panel.tree.column("status", "stretch")
    assert status(panel, a) == "未就绪"
    from types import SimpleNamespace
    for width, wide in ((theme.px(900), True), (theme.px(500), False)):
        panel._resize(SimpleNamespace(widget=panel.workspace, width=width))
        assert panel.wide is wide
    panel.tree.selection_set(panel.tree.get_children()[0])
    panel._selection_changed()
    assert "尚未检查" in panel.detail.get()
    assert not panel.remove_button.instate(["disabled"])
    panel.projects.append(str(Path(a) / "keyboard-draft"))
    panel._mark_dirty()
    panel.place(x=0, y=0, relwidth=1, relheight=1)
    app.root.deiconify()
    app.root.update()
    first, second = panel.tree.get_children()
    panel.tree.focus(first)
    panel.tree.selection_set(first)
    panel.tree.focus_force()
    app.root.update()
    panel.tree.event_generate("<KeyPress-Down>")
    app.root.update()
    assert panel.tree.selection() == (second,)
    panel.destroy()
