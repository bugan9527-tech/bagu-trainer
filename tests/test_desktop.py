import json
import os
import queue
import threading
import subprocess
import sys
from pathlib import Path

import pytest

from bagu_trainer import server
from bagu_trainer import desktop_bridge as bridge
from bagu_trainer.desktop_monitor import DesktopMonitor, ActivityDiffReader

REAL_DESKTOP_RUNNING = bridge.desktop_running


def activity(text, source="watch"):
    return {"context": text, "sources": ["test change"],
            "changes": {"source": source, "files": ["app.py"]}}


def test_debounce_baseline_then_generate_once_and_grade(env, monkeypatch):
    clock = [0]
    current = [activity("old code", "git")]
    monkeypatch.setattr(server, "_collect_context", lambda *_: current[0])
    monitor = DesktopMonitor(server, quiet_seconds=15, clock=lambda: clock[0])
    monitor.poll()
    assert env["gen"].calls == 0
    current[0] = activity("redis cache changed", "git")
    clock[0] = 3
    monitor.poll()
    clock[0] = 14
    monitor.poll()
    assert env["gen"].calls == 0
    current[0] = activity("redis cache changed again", "git")
    clock[0] = 15
    monitor.poll()
    clock[0] = 29
    monitor.poll()
    assert env["gen"].calls == 0
    clock[0] = 30
    assert monitor.poll()["state"] == "quiz_ready"
    quiz = monitor.take_quiz()
    assert quiz and "answer" not in quiz and "analysis" not in quiz
    assert monitor.take_quiz() is None
    assert monitor.dispatch("check_pending_quiz", {})["has_pending"] is False
    result = monitor.dispatch("submit_answer", {"quiz_id": quiz["quiz_id"], "answer": "A"})
    assert result["ok"] and result["correct"]
    assert not monitor.dispatch("submit_answer", {"quiz_id": quiz["quiz_id"], "answer": "A"})["ok"]
    monitor.poll()
    assert env["gen"].calls == 1


def test_watch_event_is_retained_until_quiet(env, monkeypatch):
    now = [0]
    events = iter([activity(""), activity("redis new code"), activity(""), activity("")])
    monkeypatch.setattr(server, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(server, quiet_seconds=15, clock=lambda: now[0])
    monitor.poll()
    now[0] = 1
    monitor.poll()
    now[0] = 10
    monitor.poll()
    now[0] = 16
    monitor.poll()
    assert env["gen"].calls == 1


def test_reverted_git_change_does_not_generate(env, monkeypatch):
    now = [0]
    events = iter([activity("", "git"), activity("redis edit", "git"), activity("", "git")])
    monkeypatch.setattr(server, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(server, clock=lambda: now[0])
    monitor.poll()
    now[0] = 1
    monitor.poll()
    now[0] = 30
    monitor.poll()
    assert env["gen"].calls == 0


def test_all_whitelisted_projects_and_resume(env, monkeypatch, tmp_path):
    roots = [str(tmp_path / "a"), str(tmp_path / "b")]
    env["cfg"]["project_roots"] = roots
    now = [0]
    seen = []
    def collect(_description, root):
        seen.append(root)
        return activity("redis" if now[0] else "")
    monkeypatch.setattr(server, "_collect_context", collect)
    monitor = DesktopMonitor(server, quiet_seconds=0, clock=lambda: now[0])
    monitor.poll()
    now[0] = 1
    monitor.poll()
    assert set(seen) == {server._normalize_project_root(r) for r in roots}
    assert env["gen"].calls == 2
    first = monitor.take_quiz()
    monitor.active = None
    assert monitor.resume_quiz()["quiz_id"] == first["quiz_id"]
    restarted = DesktopMonitor(server)
    second = restarted.take_quiz()
    assert second["quiz_id"] != first["quiz_id"]
    assert restarted.take_quiz() is None


def test_incremental_reader_captures_file_tail_and_excludes_secrets(tmp_path):
    source = tmp_path / "service.py"
    prefix = "# header\n" * 100
    source.write_text(prefix + "value = 1\n", encoding="utf-8")
    reader = ActivityDiffReader(str(tmp_path))
    assert reader.watch_changes()["diff_text"] == ""
    source.write_text(prefix + "value = 'redis cache'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"secret":"hidden"}', encoding="utf-8")
    result = reader.watch_changes()
    assert "+value = 'redis cache'" in result["diff_text"]
    assert "-value = 1" in result["diff_text"]
    assert result["files"] == ["service.py"]
    source.unlink()
    assert "-value = 'redis cache'" in reader.watch_changes()["diff_text"]


def test_git_reader_includes_untracked_source_only(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / "new.py").write_text("redis cache", encoding="utf-8")
    (tmp_path / "config.json").write_text("private", encoding="utf-8")
    reader = ActivityDiffReader(str(tmp_path))
    monkeypatch.setattr(reader, "_run_git", lambda arguments: SimpleNamespace(
        stdout="new.py\0config.json\0" if arguments[0] == "ls-files" else ""))
    result = reader.git_changes()
    assert result["files"] == ["new.py"]
    assert "redis cache" in result["diff_text"] and "private" not in result["diff_text"]


def test_bridge_forwarding_and_failure_never_generate_locally(monkeypatch):
    calls = []
    @bridge.forward_to_desktop
    def example(value="default"):
        calls.append(value)
        return {"local": True}
    monkeypatch.setattr(bridge, "desktop_running", lambda: True)
    monkeypatch.setattr(bridge, "remote_call", lambda name, args: {"name": name, "args": args})
    assert example()["args"] == {"value": "default"}
    def unavailable(*_):
        raise TimeoutError()
    monkeypatch.setattr(bridge, "remote_call", unavailable)
    assert example()["ok"] is False
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_named_pipe_json_roundtrip_single_instance_and_stale_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RUNTIME", tmp_path)
    owner = bridge.OwnerLock()
    assert owner.acquire()
    assert not bridge.OwnerLock().acquire()
    broker = bridge.BridgeServer(lambda name, args: {"ok": True, "name": name, "value": args["value"]}, owner)
    broker.start()
    try:
        assert bridge.remote_call("get_learning_stats", {"value": "中文"})["value"] == "中文"
        assert not bridge.remote_call("arbitrary_method", {})["ok"]
    finally:
        broker.close()
    (tmp_path / "bridge-credentials.json").write_text("stale", encoding="utf-8")
    assert not REAL_DESKTOP_RUNNING()


def test_monitor_pause_never_collects_or_delivers(env, monkeypatch):
    monkeypatch.setattr(server, "_collect_context", lambda *_: pytest.fail("paused collector ran"))
    monitor = DesktopMonitor(server)
    monitor.paused = True
    assert monitor.poll()["state"] == "paused"
    assert monitor.take_quiz() is None


def test_real_file_change_to_quiz_answer_and_persistent_record(env, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.py"
    prefix = "# unchanged header\n" * 60
    source.write_text(prefix + "value = 1\n", encoding="utf-8")
    env["cfg"]["project_roots"] = [str(project)]
    now = [0]
    monitor = DesktopMonitor(server, quiet_seconds=15, clock=lambda: now[0])
    monitor.poll()
    assert env["gen"].calls == 0
    source.write_text(prefix + "value = 'redis cache'\n", encoding="utf-8")
    now[0] = 1
    monitor.poll()
    now[0] = 16
    monitor.poll()
    quiz = monitor.take_quiz()
    assert quiz
    assert "+value = 'redis cache'" in env["gen"].last_ctx
    assert server.submit_answer(quiz["quiz_id"], "A")["correct"]
    row = server._store.get_quiz(quiz["quiz_id"])
    assert row["status"] == "answered"
    assert server._store.attempt_count(quiz["quiz_id"]) == 1


def test_two_watch_events_in_one_task_keep_both_contexts(env, monkeypatch):
    now = [0]
    events = iter([activity(""), activity("redis cache edit"), activity("mysql index edit"), activity("")])
    monkeypatch.setattr(server, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(server, quiet_seconds=15, clock=lambda: now[0])
    monitor.poll()
    now[0] = 1
    monitor.poll()
    now[0] = 3
    monitor.poll()
    now[0] = 18
    monitor.poll()
    assert "redis cache edit" in env["gen"].last_ctx
    assert "mysql index edit" in env["gen"].last_ctx


def test_desktop_runtime_entrypoint_starts_tk_without_loading_user_config():
    entry = Path(__file__).resolve().parents[1] / "scripts" / "start_desktop.py"
    result = subprocess.run([sys.executable, "-B", str(entry), "--check-runtime"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_desktop_bridge_preserves_mcp_tool_schemas():
    import asyncio
    tools = asyncio.run(server.mcp.list_tools())
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert set(schemas) == {
        "list_knowledge_points", "report_ai_activity", "check_pending_quiz",
        "open_config_window", "configure_llm", "generate_quiz", "submit_answer",
        "get_learning_stats",
    }
    assert schemas["submit_answer"]["required"] == ["quiz_id", "answer"]
    assert "project_root" in schemas["check_pending_quiz"]["properties"]


def test_desktop_ui_can_show_answer_and_feedback(env, monkeypatch):
    monkeypatch.chdir(Path.cwd())  # GUI setup uses a stable interpreter cwd.
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)
    from bagu_trainer.desktop import DesktopApp
    monitor = DesktopMonitor(server)
    app = DesktopApp(monitor)
    app.root.withdraw()
    try:
        redraws = []
        app.status.trace_add("write", lambda *_: redraws.append(app.status.get()))
        app.events.put(("status", app.status.get()))
        app._drain()
        assert redraws == []
        app.events.put(("status", "new activity"))
        app._drain()
        assert redraws == ["new activity"]
        app._show_quiz({"quiz_id": "preview", "question": "Which option is correct?",
                        "options": {letter: letter * 20 for letter in "ABCD"},
                        "knowledge_point": "Example", "project_root": "test"})
        app.popup.withdraw()
        app.root.update_idletasks()
        app._submit("preview")
        assert app.jobs.empty()
        app.answer.set("A")
        app._submit("preview")
        assert app.jobs.get_nowait() == ("submit", ("preview", "A"))
        app._submit("preview")
        assert app.jobs.empty()  # Keyboard shortcuts cannot enqueue a duplicate submit.
        app._show_grade({"ok": True, "correct": True, "correct_answer": "A",
                         "analysis": "Example explanation", "schedule": {"interval_days": 1}})
        assert "Example explanation" in app.feedback.get()
        assert all(button.instate(["disabled"]) for button in app.option_buttons)
        assert monitor.dispatch("_show_dashboard", {})["ok"]
        app._drain()
        assert monitor.dispatch("_desktop_status", {})["window_state"] == "normal"
        app.root.withdraw()
        app.root.update_idletasks()
    finally:
        app.stop.set()
        app.root.destroy()
