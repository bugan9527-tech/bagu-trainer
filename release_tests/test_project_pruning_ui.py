"""Cleanup feedback stays visible next to remaining startup requirements."""
import sys

import pytest

from bagu_trainer import startup
from bagu_trainer.desktop import DesktopApp
from bagu_trainer.desktop_monitor import DesktopMonitor
from release_tests.test_first_use import installation

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk feedback")


@pytest.mark.parametrize("ready", [False, True])
def test_cleanup_and_restored_notices_do_not_hide_readiness(installation, ready, tmp_path):
    coordinator = startup.StartupCoordinator()
    app = DesktopApp(DesktopMonitor(None), coordinator=coordinator)
    try:
        app.root.geometry("1200x800")
        app.show_restored_configuration()
        report = startup.StartupReport((startup.ReadinessCheck("model", ready, "模型未配置 Key"),))
        app.events.put(("projects_pruned", ([str(installation.watched / "deleted")], [str(installation.watched)])))
        app.events.put(("prepared", (report, object() if ready else None)))
        app._drain()
        app.root.update()
        assert "已自动移除 1 个失效监听路径" in app.project_cleanup_text.get()
        assert app.project_cleanup_notice.winfo_ismapped()
        assert app.restored_notice.winfo_ismapped()
        assert bool(app.readiness_area.winfo_ismapped()) is not ready
        visible = [app.restored_notice, app.project_cleanup_notice]
        if not ready:
            visible.insert(0, app.readiness_area)
            assert "模型未配置 Key" in app.readiness_text.get()
        for above, below in zip(visible, visible[1:]):
            assert above.winfo_y() + above.winfo_height() <= below.winfo_y()
        if not ready:
            from PIL import ImageGrab
            x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + app.root.winfo_width(),
                                y + app.root.winfo_height())).save(tmp_path / "dashboard.png")
    finally:
        app.root.destroy()


def test_project_panel_cleanup_preserves_unsaved_additions(installation):
    from bagu_trainer import config
    missing = str(installation.watched / "deleted")
    config._write_config_atomic({"project_roots": [missing, str(installation.watched)]})
    app = DesktopApp(DesktopMonitor(None), coordinator=startup.StartupCoordinator())
    try:
        app._navigate_page("projects")
        panel = app._cached_pages["projects"]
        added = str(installation.watched / "new project")
        panel.projects.append(added)
        panel._mark_dirty()
        app.events.put(("projects_pruned", ([missing], [str(installation.watched)])))
        app._drain()
        assert panel.current_projects() == [str(installation.watched), added]
        assert panel.dirty and not panel.save_button.instate(["disabled"])
    finally:
        app.root.destroy()
