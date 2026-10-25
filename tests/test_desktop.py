import json
import os
import queue
import threading
import subprocess
import sys
from pathlib import Path

import pytest

from bagu_trainer import engine
from bagu_trainer import desktop_instance as bridge
from bagu_trainer.desktop_monitor import DesktopMonitor, ActivityDiffReader

REAL_DESKTOP_RUNNING = bridge.desktop_running


def activity(text, source="watch"):
    return {"context": text, "sources": ["test change"],
            "changes": {"source": source, "files": ["app.py"]}}


def test_debounce_baseline_then_generate_once_and_grade(env, monkeypatch):
    clock = [0]
    current = [activity("old code", "git")]
    monkeypatch.setattr(engine, "_collect_context", lambda *_: current[0])
    monitor = DesktopMonitor(engine, quiet_seconds=15, clock=lambda: clock[0])
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
    result = engine.submit_answer(quiz["quiz_id"], "A")
    assert result["ok"] and result["correct"]
    assert not engine.submit_answer(quiz["quiz_id"], "A")["ok"]
    monitor.poll()
    assert env["gen"].calls == 1


def test_watch_event_is_retained_until_quiet(env, monkeypatch):
    now = [0]
    events = iter([activity(""), activity("redis new code"), activity(""), activity("")])
    monkeypatch.setattr(engine, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(engine, quiet_seconds=15, clock=lambda: now[0])
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
    monkeypatch.setattr(engine, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(engine, clock=lambda: now[0])
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
    monkeypatch.setattr(engine, "_collect_context", collect)
    monitor = DesktopMonitor(engine, quiet_seconds=0, clock=lambda: now[0])
    monitor.poll()
    now[0] = 1
    monitor.poll()
    assert set(seen) == {engine._normalize_project_root(r) for r in roots}
    assert env["gen"].calls == 2
    first = monitor.take_quiz()
    monitor.active = None
    assert monitor.resume_quiz()["quiz_id"] == first["quiz_id"]
    restarted = DesktopMonitor(engine)
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


def test_desktop_detects_edit_committed_between_polls(env, tmp_path):
    project = tmp_path / "committed-project"
    project.mkdir()
    reader = ActivityDiffReader(str(project))
    try:
        assert reader._run_git(["init"]).returncode == 0
    except FileNotFoundError:
        pytest.skip("Git is not installed")
    source = project / "缓存.py"
    source.write_text("value = 1\n", encoding="utf-8")
    def commit():
        assert reader._run_git(["add", "."]).returncode == 0
        result = reader._run_git(["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                  "-c", "commit.gpgsign=false", "commit", "-m", "test"])
        assert result.returncode == 0, result.stderr
    commit()
    env["cfg"]["project_roots"] = [str(project)]
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    monitor.poll()
    source.write_text("value = 'redis cache'\n", encoding="utf-8")
    commit()
    assert reader._run_git(["status", "--porcelain"]).stdout == ""
    assert monitor.poll()["state"] == "quiz_ready"
    assert "+value = 'redis cache'" in env["gen"].last_ctx
    monitor.poll()
    assert env["gen"].calls == 1


def test_desktop_old_large_diff_cannot_hide_new_edits(tmp_path, monkeypatch):
    (tmp_path / "old.py").write_text("# old change\n" * 1000, encoding="utf-8")
    source = tmp_path / "new.py"
    source.write_text("value = 1\n", encoding="utf-8")
    reader = ActivityDiffReader(str(tmp_path))
    monkeypatch.setattr(reader, "_run_git", lambda *_: pytest.fail("Desktop scans must not depend on Git output"))
    assert reader.changes()["diff_text"] == ""
    source.write_text("value = 'redis cache'\n", encoding="utf-8")
    result = reader.changes()
    assert result["files"] == ["new.py"]
    assert "+value = 'redis cache'" in result["diff_text"]
    assert reader.changes()["diff_text"] == ""


def test_no_match_and_cooldown_have_distinct_diagnostics(env, monkeypatch, caplog):
    monitor = DesktopMonitor(engine, quiet_seconds=0)
    events = []
    monitor.ui_post = events.append
    root = str(engine.PROJECT_ROOT)
    info = activity("redis private-code-marker")
    with caplog.at_level("INFO", logger="bagu-trainer"):
        assert monitor._generate(root, info) == "quiz_ready"
        assert monitor._generate(root, info) == "watching"
        assert "跳过" in events[-1][1]["reason"]
        monkeypatch.setattr(env["matcher"], "rank", lambda *_, **__: [])
        assert monitor._generate(root, info) == "watching"
    assert "未匹配" in events[-1][1]["reason"]
    assert "未调用 AI" in events[-1][1]["reason"]
    assert "matched=0" in caplog.text
    assert "private-code-marker" not in caplog.text
    assert env["gen"].calls == 1


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




@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_named_pipe_json_roundtrip_single_instance_and_stale_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RUNTIME", tmp_path)
    owner = bridge.OwnerLock()
    assert owner.acquire()
    assert not bridge.OwnerLock().acquire()
    broker = bridge.WindowBroker(lambda name, args: {"ok": True, "name": name, "value": args["value"]}, owner)
    broker.start()
    try:
        assert bridge.remote_call("_desktop_status", {"value": "中文"})["value"] == "中文"
        assert not bridge.remote_call("arbitrary_method", {})["ok"]
        assert not bridge.remote_call("submit_answer", {"quiz_id": "test", "answer": "A"})["ok"]
    finally:
        broker.close()
    (tmp_path / "bridge-credentials.json").write_text("stale", encoding="utf-8")
    assert not REAL_DESKTOP_RUNNING()


def test_monitor_pause_never_collects_or_delivers(env, monkeypatch):
    monkeypatch.setattr(engine, "_collect_context", lambda *_: pytest.fail("paused collector ran"))
    monitor = DesktopMonitor(engine)
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
    monitor = DesktopMonitor(engine, quiet_seconds=15, clock=lambda: now[0])
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
    assert engine.submit_answer(quiz["quiz_id"], "A")["correct"]
    row = engine.session._store.get_quiz(quiz["quiz_id"])
    assert row["status"] == "answered"
    assert engine.session._store.attempt_count(quiz["quiz_id"]) == 1


def test_two_watch_events_in_one_task_keep_both_contexts(env, monkeypatch):
    now = [0]
    events = iter([activity(""), activity("redis cache edit"), activity("mysql index edit"), activity("")])
    monkeypatch.setattr(engine, "_collect_context", lambda *_: next(events))
    monitor = DesktopMonitor(engine, quiet_seconds=15, clock=lambda: now[0])
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


def test_window_broker_is_not_a_learning_interface():
    assert bridge.WINDOW_COMMANDS == {"_show_dashboard", "_desktop_status", "_navigate"}
    monitor = DesktopMonitor(engine)
    assert monitor.window_command("submit_answer", {})["ok"] is False
    assert monitor.window_command("get_learning_stats", {})["ok"] is False


def test_desktop_ui_can_show_answer_and_feedback(env, monkeypatch):
    monkeypatch.chdir(Path.cwd())  # GUI setup uses a stable interpreter cwd.
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)
    from bagu_trainer.desktop import DesktopApp
    monitor = DesktopMonitor(engine)
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
        practice = app._page_frame("practice")
        app._navigate_page("practice")
        app.root.update_idletasks()
        assert app.practice.submit() is False  # no answer chosen
        assert app.jobs.empty()
        app.answer.set("A")
        assert app.practice.submit() is True
        assert app.jobs.get_nowait() == ("submit", ("preview", "A"))
        assert app.practice.submit() is False  # Keyboard shortcuts cannot enqueue a duplicate submit.
        assert app.jobs.empty()
        app.practice.apply_grade({"ok": True, "correct": True, "correct_answer": "A",
                                  "analysis": "Example explanation", "schedule": {"interval_days": 1}})
        assert "Example explanation" in app.feedback.get()
        assert all(button.instate(["disabled"]) for button in practice.option_buttons)
        assert monitor.window_command("_show_dashboard", {})["ok"]
        app._drain()
        assert monitor.window_command("_desktop_status", {})["window_state"] == "normal"
        app.root.withdraw()
        app.root.update_idletasks()
    finally:
        app.stop.set()
        app.root.destroy()


@pytest.mark.parametrize("paused", [False, True])
def test_quality_skip_clears_failure_text_and_progress_in_real_ui(env, monkeypatch, paused):
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)
    from bagu_trainer.desktop import DesktopApp
    monitor = DesktopMonitor(engine)
    app = DesktopApp(monitor)
    app.root.withdraw()
    root = engine._normalize_project_root(engine.PROJECT_ROOT)
    kind = ["service"]

    def generate(*_):
        monitor.paused = paused and kind[0] == "quality"
        return {"available": False, "failure_kind": kind[0], "unavailable_reason": "private diagnostic"}

    monkeypatch.setattr(env["gen"], "generate", generate)
    try:
        monitor._generate(root, {"context": "redis edit"})
        app._drain()
        assert app.generation_retryable
        assert "private diagnostic" in app.task_hint.get()
        kind[0] = "quality"
        state = monitor._generate(root, {"context": "redis edit"}, ("test-kp-1",))
        app.events.put(("dashboard", {"state": state, "projects": [root], "stats": None}))
        app._drain()
        app.root.update_idletasks()
        assert not app.generation_busy and not app.generation_retryable
        assert app.retry_button.instate(["disabled"])
        assert app.task_progress.winfo_manager() == ""
        assert app.listen_state.get() == ("监听已暂停" if paused else "正在监听代码改动")
        assert app.task_hint.get() == "等待新的代码改动。"
        for text in (app.task_hint.get(), app.global_status.get(), app.listen_state.get()):
            assert "失败" not in text and "不可用" not in text and "private diagnostic" not in text
        assert not monitor.list_pending()
    finally:
        app.stop.set()
        app.root.destroy()
