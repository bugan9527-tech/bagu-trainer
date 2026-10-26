"""Runtime facts, acknowledged scopes and stale scan retirement; no Tk/network."""
import threading
from concurrent.futures import ThreadPoolExecutor

from bagu_trainer import engine
from bagu_trainer.desktop_monitor import DesktopMonitor


def setup_monitor(env, tmp_path, monkeypatch):
    roots = [str(tmp_path / name) for name in ("first", "second")]
    from pathlib import Path
    for root in roots:
        Path(root).mkdir()
    env["cfg"]["project_roots"] = roots
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    monitor.apply_projects(roots)
    return monitor, [engine._normalize_project_root(root) for root in roots]


def test_initialization_requires_success_then_pause_resume_requires_fresh_check(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    assert all(row["state"] == "initializing" and row["last_success_at"] is None
               for row in monitor.project_snapshot()["projects"].values())
    monitor.initialized.update(roots)  # Membership alone is not proof of health.
    assert monitor.project_snapshot()["projects"][roots[0]]["state"] == "initializing"
    monkeypatch.setattr(engine, "observe_project", lambda *_a, **_kw: {"context": ""})
    monitor.poll()
    before = monitor.project_snapshot()
    assert all(row["state"] == "watching" and row["last_success_at"] is not None
               for row in before["projects"].values())
    monitor.set_paused(True)
    assert all(row["state"] == "paused" for row in monitor.project_snapshot()["projects"].values())
    monitor.set_paused(False)
    assert all(row["state"] == "initializing" for row in monitor.project_snapshot()["projects"].values())
    monitor.poll()
    assert all(row["state"] == "watching" for row in monitor.project_snapshot()["projects"].values())


def test_one_scan_failure_is_safe_and_does_not_skip_other_project(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    def observe(root, **_):
        if root == roots[0]:
            raise PermissionError("secret private server response")
        return {"context": ""}
    monkeypatch.setattr(engine, "observe_project", observe)
    monitor.poll()
    snapshot = monitor.project_snapshot()["projects"]
    assert snapshot[roots[0]]["state"] == "unavailable"
    assert snapshot[roots[0]]["last_success_at"] is None
    assert "secret" not in snapshot[roots[0]]["reason"]
    assert snapshot[roots[1]]["state"] == "watching"


def test_saved_config_is_not_applied_scope_and_application_failure_keeps_old_scope(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    visited = []
    monkeypatch.setattr(engine, "observe_project", lambda root, **_: visited.append(root) or {"context": ""})
    env["cfg"]["project_roots"] = [roots[1]]
    monitor.poll()
    assert visited == roots
    original = engine.session.set_watch_roots
    def fail(_roots):
        raise OSError("private failure")
    monkeypatch.setattr(engine.session, "set_watch_roots", fail)
    previous = monitor.project_snapshot()
    import pytest
    with pytest.raises(OSError):
        monitor.apply_projects([roots[1]])
    assert monitor.project_snapshot() == previous
    monkeypatch.setattr(engine.session, "set_watch_roots", original)
    monitor.apply_projects([roots[1]])
    assert monitor.project_snapshot()["applied_roots"] == [roots[1]]
    assert engine._project_roots() == [roots[1]]


def test_removed_or_paused_old_scan_cannot_restore_a_status(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def blocked(root, **_):
        entered.set()
        assert release.wait(3)
        return {"context": ""}
    monkeypatch.setattr(engine, "observe_project", blocked)
    with ThreadPoolExecutor(max_workers=1) as worker:
        task = worker.submit(monitor.poll)
        try:
            assert entered.wait(1)
            monitor.apply_projects([roots[1]])
            monitor.set_paused(True)
        finally:
            release.set()
        task.result(1)
    snapshot = monitor.project_snapshot()
    assert roots[0] not in snapshot["projects"]
    assert snapshot["projects"][roots[1]]["state"] == "paused"
    assert snapshot["projects"][roots[1]]["last_success_at"] is None


def test_ai_failure_does_not_change_successful_scan_state(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    context = [""]
    monkeypatch.setattr(engine, "observe_project", lambda *_a, **_k: {"context": context[0]})
    monitor.poll()
    context[0] = "redis code edit"
    monkeypatch.setattr(env["gen"], "generate", lambda *_: {"available": False, "failure_kind": "service"})
    assert monitor.poll()["state"] == "generation_unavailable"
    assert all(row["state"] == "watching" for row in monitor.project_snapshot()["projects"].values())


def test_actual_deleted_directory_is_unavailable_and_readiness_is_not_health(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    from pathlib import Path
    Path(roots[0]).rmdir()
    monitor.poll()
    snapshot = monitor.project_snapshot()
    assert snapshot["projects"][roots[0]]["state"] == "unavailable"
    assert snapshot["projects"][roots[1]]["state"] == "watching"
    monitor.set_ready(False)
    assert all(row["state"] == "not_ready" for row in monitor.project_snapshot()["projects"].values())


def test_walker_reports_access_failure_instead_of_empty_success(tmp_path, monkeypatch):
    from bagu_trainer.desktop_monitor import ActivityDiffReader
    import os
    def walk(*_a, onerror=None, **_kw):
        onerror(PermissionError("private directory"))
        return iter(())
    monkeypatch.setattr(os, "walk", walk)
    import pytest
    with pytest.raises(PermissionError):
        ActivityDiffReader(str(tmp_path)).changes()


def test_readiness_change_retires_an_inflight_success(env, tmp_path, monkeypatch):
    monitor, roots = setup_monitor(env, tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def blocked(*_a, **_kw):
        entered.set()
        assert release.wait(3)
        return {"context": ""}
    monkeypatch.setattr(engine, "observe_project", blocked)
    with ThreadPoolExecutor(max_workers=1) as workers:
        task = workers.submit(monitor.poll)
        try:
            assert entered.wait(1)
            monitor.set_ready(False)
        finally:
            release.set()
        task.result(1)
    assert all(row["state"] == "not_ready" and row["last_success_at"] is None
               for row in monitor.project_snapshot()["projects"].values())
