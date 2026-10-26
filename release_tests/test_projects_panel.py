"""Selection events must keep project actions in sync with the visible list."""
import sys
from pathlib import Path

import pytest

from bagu_trainer.tk_runtime import create_root
from bagu_trainer.ui_theme import apply_theme

from release_tests.test_first_use import installation


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_empty_save_keeps_specific_error_after_control_refresh(installation):
    from bagu_trainer import config, startup
    from bagu_trainer.projects_panel import ProjectsPanel

    startup.set_projects([installation.watched])
    original = config.CONFIG_PATH.read_bytes()
    applied = []
    root = create_root()
    try:
        panel = ProjectsPanel(root, apply_theme(root), on_saved=applied.append)
        panel.pack(fill="both", expand=True)
        root.update()
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.tree.event_generate("<<TreeviewSelect>>")
        panel.remove_button.invoke()
        panel.save_button.invoke()
        root.update()
        assert config.CONFIG_PATH.read_bytes() == original
        assert applied == []
        assert panel.dirty and not panel.busy
        assert "请选择至少一个" in panel.hint.get()
        assert "已保存的配置未更改" in panel.hint.get()
        assert not panel.save_button.instate(["disabled"])
        error = panel.hint.get()
        panel.tree.event_generate("<<TreeviewSelect>>")
        panel._sync_controls()
        panel._render()
        root.geometry("600x500")
        root.update()
        assert panel.hint.get() == error

        # A cancelled picker is not an edit and must retain the explanation.
        from tkinter import filedialog
        from unittest.mock import patch
        with patch.object(filedialog, "askdirectory", return_value=""):
            panel.add_button.invoke()
        assert panel.hint.get() == error
        with patch.object(filedialog, "askdirectory", return_value=str(installation.watched)):
            panel.add_button.invoke()
        assert "保存失败" not in panel.hint.get()
        panel.save_button.invoke()
        assert not panel.dirty and panel.save_button.instate(["disabled"])
        assert "保存失败" not in panel.hint.get()
        assert config.read_user_config()["project_roots"] == [str(installation.watched)]
        assert applied == [[str(installation.watched)]]
    finally:
        root.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
@pytest.mark.parametrize("failure", [PermissionError("配置目录不可写"), OSError("配置磁盘写入失败")])
def test_write_failure_survives_refresh_and_can_retry(installation, monkeypatch, failure):
    from bagu_trainer import config, startup
    from bagu_trainer.projects_panel import ProjectsPanel

    second = installation.project / "second"
    second.mkdir()
    startup.set_projects([installation.watched, second])
    before = config.CONFIG_PATH.read_bytes()
    applied = []
    root = create_root()
    try:
        panel = ProjectsPanel(root, apply_theme(root), on_saved=applied.append)
        panel.pack(fill="both", expand=True)
        root.update()
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.tree.event_generate("<<TreeviewSelect>>")
        panel.remove_button.invoke()
        def fail_replace(*_):
            raise failure
        with monkeypatch.context() as failing:
            # Exercise real validation, transaction and temporary-file cleanup.
            failing.setattr(config.os, "replace", fail_replace)
            panel.save_button.invoke()
        error = panel.hint.get()
        assert type(failure).__name__ in error and str(failure) not in error
        assert config.CONFIG_PATH.read_bytes() == before and applied == []
        assert not list(config.CONFIG_PATH.parent.glob(".config.json.*.tmp"))
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.tree.event_generate("<<TreeviewSelect>>")
        root.update()
        panel._sync_controls()
        assert panel.hint.get() == error
        assert panel.dirty and not panel.busy
        assert not panel.save_button.instate(["disabled"])

        # The same draft can be retried when the write problem is resolved.
        panel.save_button.invoke()
        root.update()
        assert applied == [[str(second)]]
        assert config.read_user_config()["project_roots"] == [str(second)]
        assert not panel.dirty and panel.save_button.instate(["disabled"])
        assert "保存失败" not in panel.hint.get()
    finally:
        root.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_desktop_applies_only_successfully_saved_scope(installation, monkeypatch):
    import threading
    from types import SimpleNamespace
    from tkinter import filedialog
    from bagu_trainer import config, startup
    from bagu_trainer.desktop import DesktopApp
    from bagu_trainer.desktop_monitor import DesktopMonitor
    from release_tests.test_ui_theme import cancel_timers

    first = str(installation.watched)
    second = installation.project / "second"
    second.mkdir()
    startup.set_projects([first])
    visited = []
    engine = SimpleNamespace(load_config=config.load_config, PROJECT_ROOT=config.PROJECT_ROOT,
        _normalize_project_root=str,
        session=SimpleNamespace(_diff_readers={}, forget_projects=lambda roots: None),
        observe_project=lambda path, **_: visited.append(path) or {"context": ""})
    monitor = DesktopMonitor(engine)
    monitor.poll()
    assert visited == [first]
    app = DesktopApp(monitor)
    try:
        app._navigate_page("projects")
        panel = app._page_frame("projects")
        app.root.update()
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.tree.event_generate("<<TreeviewSelect>>")
        panel.remove_button.invoke()
        panel.save_button.invoke()
        assert "请选择至少一个" in panel.hint.get()
        assert app.jobs.empty()
        visited.clear()
        monitor.poll()
        assert visited == [first] and monitor.initialized == {first}

        # Navigate away/back through the real shell: the error remains visible.
        app._navigate_page("dashboard")
        app._navigate_page("projects")
        assert app._page_frame("projects") is panel
        assert "请选择至少一个" in panel.hint.get()
        monkeypatch.setattr(filedialog, "askdirectory", lambda **_: str(second))
        panel.add_button.invoke()
        panel.save_button.invoke()
        name, paths = app.jobs.get_nowait()
        assert name == "apply_projects" and paths["roots"] == [str(second)]
        assert app.jobs.empty()
        app._apply_projects_serial(paths["roots"])
        visited.clear()
        monitor.poll()
        assert visited == [str(second)] and monitor.initialized == {str(second)}
        assert config.read_user_config()["project_roots"] == [str(second)]
        assert not panel.dirty and "保存失败" not in panel.hint.get()
    finally:
        cancel_timers(app.root)
        app.root.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_selection_enables_remove_and_removal_requires_save(monkeypatch, tmp_path):
    from bagu_trainer import config, startup
    from bagu_trainer.projects_panel import ProjectsPanel

    projects = [str(tmp_path / "first"), str(tmp_path / "second")]
    for project in projects:
        Path(project).mkdir()
        (Path(project) / "代码.py").write_text("# 原项目文件\n", encoding="utf-8")
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*.py")}
    monkeypatch.setattr(config, "load_config", lambda: {"project_roots": projects})
    saved = []
    applied = []
    monkeypatch.setattr(startup, "set_projects", lambda roots: saved.append(list(roots)))
    root = create_root()
    try:
        panel = ProjectsPanel(root, apply_theme(root), on_saved=lambda roots: applied.append(list(roots)))
        panel.pack(fill="both", expand=True)
        root.update()
        assert panel.remove_button.instate(["disabled"])
        assert panel.save_button.instate(["disabled"])

        panel.tree.selection_set(panel.tree.get_children()[1])
        panel.tree.event_generate("<<TreeviewSelect>>")
        assert projects[1] in panel.detail.get()
        assert not panel.remove_button.instate(["disabled"])

        panel.tree.selection_remove(*panel.tree.selection())
        panel.tree.event_generate("<<TreeviewSelect>>")
        assert panel.remove_button.instate(["disabled"])

        panel.tree.selection_set(panel.tree.get_children()[1])
        panel.tree.event_generate("<<TreeviewSelect>>")
        panel.remove_button.invoke()
        assert panel.current_projects() == projects[:1]
        assert panel.remove_button.instate(["disabled"])
        assert not panel.save_button.instate(["disabled"])
        assert not saved
        assert not applied
        assert {p: p.read_bytes() for p in originals} == originals

        panel.save_button.invoke()
        assert saved == [projects[:1]]
        assert applied == [projects[:1]]
        assert {p: p.read_bytes() for p in originals} == originals
        assert panel.save_button.instate(["disabled"])
    finally:
        root.destroy()
