"""Automatic watch-list repair uses isolated settings and never deletes data."""
import errno
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bagu_trainer import config, startup
from release_tests.test_startup import install, mark_model_tested, write_configuration


def test_startup_removes_missing_project_and_keeps_working_projects(install):
    from bagu_trainer import onboarding
    mark_model_tested(install)
    missing = str(install.watched / "deleted")
    write_configuration(install, {**install.configuration,
                                 "project_roots": [missing, str(install.watched)]})
    before = {p: p.read_bytes() for p in (install.bank, install.database, onboarding.state_path())}
    engine = object()
    coordinator = startup.StartupCoordinator(engine_loader=lambda: engine)
    assert coordinator.prepare() is engine
    assert config.read_user_config() == install.configuration
    assert {p: p.read_bytes() for p in before} == before
    assert onboarding.saved_model_verified()
    assert coordinator.events.get_nowait() == (
        "projects_pruned", ([missing], [str(install.watched)]))


def test_all_missing_projects_leave_an_explicit_empty_watch_list(install):
    missing = str(install.watched / "deleted")
    write_configuration(install, {**install.configuration, "project_roots": [missing]})
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("no project"))
    assert coordinator.prepare() is None
    assert config.read_user_config()["project_roots"] == []
    assert not next(c for c in coordinator.report.checks if c.name == "projects").ready


@pytest.mark.parametrize("failure", [PermissionError(errno.EACCES, "denied"),
                                    OSError(errno.EIO, "temporarily unavailable")])
def test_inaccessible_project_is_retained(install, monkeypatch, failure):
    original = Path.stat
    def stat(path, *args, **kwargs):
        if path == install.watched:
            raise failure
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    before = install.settings.read_bytes()
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("inaccessible"))
    assert coordinator.prepare() is None
    assert install.settings.read_bytes() == before
    assert coordinator.events.empty()


def test_file_replacing_project_is_removed_without_deleting_file(install):
    install.watched.rmdir()
    install.watched.write_bytes(b"replacement file")
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("no project"))
    assert coordinator.prepare() is None
    assert config.read_user_config()["project_roots"] == []
    assert install.watched.read_bytes() == b"replacement file"


def test_valid_watch_list_does_not_rewrite_configuration(install, monkeypatch):
    monkeypatch.setattr(config, "_write_config_atomic", lambda *_: pytest.fail("unneeded write"))
    assert startup.StartupCoordinator(engine_loader=object).prepare() is not None


def test_failed_atomic_save_keeps_original_config_and_reports_failure(install, monkeypatch):
    write_configuration(install, {**install.configuration,
                                 "project_roots": [str(install.watched / "deleted")]})
    before = install.settings.read_bytes()
    def denied(*_):
        raise PermissionError("fixture")
    monkeypatch.setattr(config, "_write_config_atomic", denied)
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("save failed"))
    assert coordinator.prepare() is None
    assert install.settings.read_bytes() == before
    assert "PermissionError" in coordinator.report.error
    assert coordinator.events.empty()


def test_read_only_probe_does_not_remove_missing_paths(install):
    write_configuration(install, {**install.configuration,
                                 "project_roots": [str(install.watched / "deleted")]})
    before = install.settings.read_bytes()
    assert not startup.check_readiness().ready
    assert install.settings.read_bytes() == before


def test_disconnected_drive_is_retained(install, monkeypatch):
    from bagu_trainer import project_cleanup
    missing = str(install.watched / "deleted")
    write_configuration(install, {**install.configuration, "project_roots": [missing]})
    before = install.settings.read_bytes()
    def disconnected(*_):
        raise FileNotFoundError("drive unavailable")
    monkeypatch.setattr(project_cleanup.os, "scandir", disconnected)
    assert project_cleanup.prune_missing_projects() == ([], [missing])
    assert install.settings.read_bytes() == before


def test_network_share_is_retained_even_when_temporarily_missing(install):
    from bagu_trainer.project_cleanup import prune_missing_projects
    missing = r"\\server.invalid\share\project"
    write_configuration(install, {**install.configuration, "project_roots": [missing]})
    assert prune_missing_projects() == ([], [missing])


@pytest.mark.parametrize("keep_project", [False, True])
def test_worker_prunes_deleted_project_and_preserves_other_baselines(install, monkeypatch, keep_project):
    from bagu_trainer import desktop
    removed = str(install.watched / "deleted")
    kept = str(install.watched)
    stop = threading.Event()
    forgotten = []
    engine = SimpleNamespace(
        _normalize_project_root=str, PROJECT_ROOT=install.project,
        load_config=config.load_config, get_learning_stats=lambda: {"ok": False},
        session=SimpleNamespace(forget_projects=lambda roots: forgotten.extend(roots)))
    coordinator = startup.StartupCoordinator(engine_loader=lambda: engine)
    assert coordinator.prepare() is engine
    roots = [removed, kept] if keep_project else [removed]
    write_configuration(install, {**install.configuration, "project_roots": roots})
    def checker():
        stop.set()
        return startup.check_readiness()
    coordinator.checker = checker
    def unexpected_poll():
        pytest.fail("polled stale paths before preparation")
    monitor = SimpleNamespace(engine=engine, initialized=set(roots),
                              observed={p: "baseline" for p in roots},
                              waiting={p: "pending edit" for p in roots},
                              failed_generations={removed: "failed"}, poll=unexpected_poll)
    ticks = iter([0, 31, 31])
    monkeypatch.setattr(desktop.time, "monotonic", lambda: next(ticks, 31))
    context = SimpleNamespace(monitor=monitor, coordinator=coordinator, events=coordinator.events,
                              jobs=queue.Queue(), stop=stop, worker_done=threading.Event())
    desktop.DesktopApp._worker(context)
    expected = [kept] if keep_project else []
    assert config.read_user_config()["project_roots"] == expected
    assert monitor.initialized == set(expected)
    assert monitor.observed == {p: "baseline" for p in expected}
    assert monitor.waiting == {p: "pending edit" for p in expected}
    assert forgotten == [removed] and not monitor.failed_generations
    assert monitor.capabilities_ready is keep_project
    assert context.worker_done.is_set()


def test_empty_watch_list_never_falls_back_to_installation_directory(install):
    from bagu_trainer.desktop_monitor import DesktopMonitor
    engine = SimpleNamespace(PROJECT_ROOT=install.project,
                             load_config=lambda: {"project_roots": []},
                             _normalize_project_root=str)
    monitor = DesktopMonitor(engine)
    assert monitor.poll() == {"state": "not_ready", "projects": 0}
    assert monitor.retry_generation() == {"state": "not_ready", "projects": 0}
