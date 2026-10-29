"""Project watch module tests at its Activity-producing Interface."""
import threading

from bagu_trainer.application.projects import Activity
from bagu_trainer.application.projects import ProjectWatch
from bagu_trainer.application.learning import engine
from bagu_trainer.application.monitor import DesktopMonitor


def test_watch_establishes_baseline_then_emits_stable_activity(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    clock = [0.0]
    contexts = ["startup baseline", "redis changed", "redis changed", "redis changed"]
    watch = ProjectWatch(quiet_seconds=15, clock=lambda: clock[0])
    watch.commit_scope(watch.prepare_scope([str(root)]))
    watch.set_ready(True)

    def observe(project_root, _reader):
        return {"project_root": project_root, "context": contexts.pop(0),
                "sources": ["synthetic file edit"],
                "changes": {"source": "watch", "files": ["app.py"]}}

    scan = lambda: watch.scan(
        observe, bank_available=True, context_limit=4000,
        stop=threading.Event())

    assert scan().activities == ()  # First observation only establishes a baseline.
    clock[0] = 1
    assert scan().activities == ()
    clock[0] = 10
    assert scan().activities == ()  # Nine seconds is below the stable window.
    clock[0] = 16
    result = scan()
    assert len(result.activities) == 1
    activity = result.activities[0]
    assert isinstance(activity, Activity)
    assert activity.project_root == str(root.resolve()).casefold() or activity.project_root == str(root.resolve())
    assert activity.context == "redis changed"
    assert activity.sources == ("synthetic file edit",)
    assert activity.learning_input()["changes"]["files"] == ["app.py"]


def test_scope_update_keeps_unchanged_baseline_and_retires_removed_project(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    watch = ProjectWatch()
    clock = [0.0]
    watch.clock = lambda: clock[0]
    scope = watch.prepare_scope([str(first)])
    watch.commit_scope(scope)
    first_root = scope.roots[0]
    contexts = {first_root: "startup", str(second.resolve()): "second startup"}

    def observe(root, _reader):
        return {"project_root": root, "context": contexts[root], "sources": [],
                "changes": {"source": "watch", "files": ["app.py"]}}

    def scan():
        return watch.scan(observe, bank_available=True, context_limit=4000,
                          stop=threading.Event())

    watch.set_ready(True)
    assert scan().activities == ()  # First observation establishes each baseline.

    expanded = watch.prepare_scope([str(first), str(second)])
    watch.commit_scope(expanded)
    assert scan().activities == ()  # An unchanged project's baseline was retained.

    contexts[first_root] = "redis changed"
    clock[0] = 1
    assert scan().activities == ()
    clock[0] = 16
    emitted = scan().activities
    assert len(emitted) == 1 and emitted[0].project_root == first_root
    assert emitted[0].context == "redis changed"

    reduced = watch.prepare_scope([str(second)])
    watch.commit_scope(reduced)
    assert first_root not in watch.snapshot()["projects"]


def test_removed_runtime_scope_discards_inflight_generation(env, tmp_path, monkeypatch):
    root = tmp_path / "watched"
    root.mkdir()
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    monitor.apply_projects([str(root)])
    observations = iter(("startup", "redis changed"))
    monkeypatch.setattr(engine, "observe_project", lambda project, **_kwargs: {
        "project_root": project, "context": next(observations), "sources": ["app.py"],
        "changes": {"source": "watch", "files": ["app.py"]}})
    assert monitor.poll()["state"] == "watching"

    entered, release = threading.Event(), threading.Event()
    original = env["gen"].generate

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(env["gen"], "generate", delayed)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as workers:
        task = workers.submit(monitor.poll)
        try:
            assert entered.wait(1)
            monitor.apply_projects([])
        finally:
            release.set()
        task.result(1)

    assert monitor.project_snapshot()["applied_roots"] == []
    assert monitor.list_pending() == []


def test_paused_watcher_keeps_explicit_practice_available(env, tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    monitor = DesktopMonitor(engine)
    monitor.apply_projects([str(root)])
    result = engine.learning.generate_for_activity(
        str(root), {"context": "redis cache", "sources": ["app.py"]})
    quiz = result["result"][1][0]

    monitor.set_paused(True)
    assert monitor.list_pending()[0]["quiz_id"] == quiz["quiz_id"]
    entered = monitor.enter_quiz(quiz["quiz_id"])
    assert entered["quiz_id"] == quiz["quiz_id"]
    assert engine.learning.submit_answer(quiz["quiz_id"], "A")["ok"]
