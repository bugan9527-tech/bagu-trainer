"""Real job queue -> engine -> event queue -> Tk/controller latency regressions.

Models, files and SQLite are isolated by conftest; no real network or credentials.
"""
import threading
import time
import gc
import _tkinter

import pytest

from bagu_trainer import engine
from bagu_trainer.desktop import DesktopApp
from bagu_trainer.desktop_monitor import DesktopMonitor


def pump(app, predicate, timeout=4):
    deadline = time.perf_counter() + timeout
    tick = threading.Event()
    while not predicate() and time.perf_counter() < deadline:
        # Stop at the controller/widget transition being measured. update()
        # also drains subsequent native painting and idle layout, so it can
        # charge unrelated redraw work to an already completed worker result.
        if not app.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
            tick.wait(.001)
    assert predicate(), "UI state did not complete before deadline"


def seed(env, monkeypatch, count=3):
    quizzes = []
    original = dict(env["matcher"].KP)
    for index in range(count):
        monkeypatch.setattr(env["matcher"], "KP", {**original, "id": f"latency-{index}"})
        _, generated, _, failures = engine.generate_activity(str(engine.PROJECT_ROOT),
                                                            {"context": f"redis edit {index}"})
        assert not failures
        quizzes.extend(generated)
    # Windows clocks can give multiple generated quizzes the same timestamp.
    # The pending list then breaks ties by id; next-question expectations must
    # follow the actual displayed queue rather than generation-call order.
    order = {item["quiz_id"]: index for index, item in enumerate(engine.session.list_pending())}
    return sorted(quizzes, key=lambda quiz: order[quiz["quiz_id"]])


@pytest.fixture
def live_app(env, monkeypatch, request):
    mode = getattr(request, "param", "idle")
    if mode == "tied-clock":
        import uuid
        now = engine._now()
        monkeypatch.setattr(engine, "_now", lambda: now)
        ids = iter(uuid.UUID(int=index << 96) for index in (3, 2, 1))
        original_uuid = uuid.uuid4
        def next_id():
            try:
                return next(ids)
            except StopIteration:
                return original_uuid()
        monkeypatch.setattr(uuid, "uuid4", next_id)
        mode = "idle"
    quizzes = seed(env, monkeypatch)
    monitor = DesktopMonitor(engine)
    scanned = threading.Event()
    polls = []
    entered, release = threading.Event(), threading.Event()
    tail_entered, tail_release = threading.Event(), threading.Event()
    gc.collect()  # Release prior Tk cycles on the UI thread before starting workers.
    actual_poll = monitor.poll
    original_generate = env["gen"].generate
    if mode != "idle":
        root = engine._normalize_project_root(engine.PROJECT_ROOT)
        monitor.quiet_seconds = 0
        monitor.initialized.add(root)
        monitor.observed[root] = "baseline"
        def collect(*_):
            if mode == "scan":
                entered.set()
                assert release.wait(6), "test failed to release scanner"
            return {"context": "redis background edit", "changes": {"source": "watch"}, "sources": []}
        monkeypatch.setattr(engine, "_collect_context", collect)
        monkeypatch.setattr(env["matcher"], "KP", {**env["matcher"].KP, "id": "background-new"})
        def slow_generate(*args):
            entered.set()
            assert release.wait(6), "test failed to release model"
            if mode.startswith("failure"):
                raise OSError("private model response must not reach UI")
            return original_generate(*args)
        if mode != "scan":
            monkeypatch.setattr(env["gen"], "generate", slow_generate)
    def poll():
        polls.append(time.perf_counter())
        scanned.set()
        result = actual_poll() if mode != "idle" else {"state": "watching", "projects": 1}
        if mode == "failure-tail":
            tail_entered.set()
            assert tail_release.wait(6)
        return result
    monkeypatch.setattr(monitor, "poll", poll)
    monitor.delivery_paused.set()
    app = DesktopApp(monitor)
    app.root.withdraw()
    app._pending_items = monitor.list_pending()
    app._show_quiz(monitor.enter_quiz(quizzes[0]["quiz_id"]))
    app._answer_window().show()
    app.root.update()  # Settle initial widget/layout work before measuring an existing quiz switch.
    app._start_worker()
    assert scanned.wait(2)
    app.test_entered, app.test_release = entered, release
    app.test_tail_entered, app.test_tail_release = tail_entered, tail_release
    # A job queued after a completed scan exposes the old fixed three-second wait.
    try:
        yield app, quizzes, polls
    finally:
        release.set()
        tail_release.set()
        app.stop.set()
        app.jobs.put(("wake", None))
        app.worker_thread.join(6)
        assert not app.worker_thread.is_alive()
        if not app.closed:
            app.root.update_idletasks()
            app.root.destroy()
        gc.collect()


def test_idle_switch_queue_to_tk_under_200ms(live_app):
    app, quizzes, _ = live_app
    target = quizzes[1]["quiz_id"]
    started = time.perf_counter()
    assert app._enter_pending(target)
    pump(app, lambda: app.practice.quiz_id == target)
    elapsed = (time.perf_counter() - started) * 1000
    print(f"idle queue-to-Tk switch: {elapsed:.1f} ms")
    assert elapsed < 200


@pytest.mark.parametrize("live_app", ["slow", "failure", "scan"], indirect=True)
def test_slow_model_does_not_block_switch_or_submit(live_app):
    app, quizzes, _ = live_app
    assert app.test_entered.wait(2)
    target = quizzes[1]["quiz_id"]
    started = time.perf_counter()
    assert app._enter_pending(target)
    pump(app, lambda: app.practice.quiz_id == target, timeout=1)
    switch_ms = (time.perf_counter() - started) * 1000
    app.practice.choose("A")
    started = time.perf_counter()
    assert app.practice.submit()
    assert not app.practice.submit()
    assert not app._enter_pending(quizzes[2]["quiz_id"])
    pump(app, lambda: app.practice.submit_state == "finished", timeout=1)
    submit_ms = (time.perf_counter() - started) * 1000
    assert not app.test_release.is_set()  # Completion precedes model response.
    assert engine.session._store.attempt_count(target) == 1
    print(f"blocked model switch: {switch_ms:.1f} ms; submit-to-Tk: {submit_ms:.1f} ms")
    assert switch_ms < 200 and submit_ms < 200


@pytest.mark.parametrize("live_app", ["failure"], indirect=True)
def test_generation_failure_retains_explicit_retry(live_app, env, monkeypatch):
    app, _, _ = live_app
    assert app.test_entered.wait(2)
    app.test_release.set()
    pump(app, lambda: app.generation_retryable)
    assert "OSError" in app.task_hint.get() and "private model response" not in app.task_hint.get()
    monkeypatch.setattr(env["gen"], "generate", type(env["gen"])().generate)
    assert app._request_action("retry_generation")
    pump(app, lambda: not app.generation_retryable and not app.generation_busy)
    assert any(q["kp_id"] == "background-new" for q in app.monitor.list_pending())


def test_consecutive_next_keeps_drafts_and_avoids_empty_state(live_app):
    app, quizzes, polls = live_app
    first, second, third = [q["quiz_id"] for q in quizzes]
    app.practice.choose("B")
    app.practice.dismiss()
    assert app.practice.quiz_id == first and app.practice.busy
    pump(app, lambda: not app.practice.busy)
    assert app.practice.quiz_id == second
    app.practice.choose("C")
    assert app._enter_pending(first)
    pump(app, lambda: not app.practice.busy)
    assert app.answer.get() == "B"
    app.practice.choose("A")
    assert app.practice.submit()
    pump(app, lambda: app.practice.submit_state == "finished")
    page = app._answer_window()
    assert page.dismiss_button.cget("text") == "下一题"
    for target in (second, third):
        previous = app.practice.quiz_id
        app.practice.dismiss()
        assert app.practice.quiz_id == previous
        pump(app, lambda: app.practice.quiz_id == target)
        if target == second:
            assert app.answer.get() == "C"
        app.practice.choose("A")
        assert app.practice.submit()
        pump(app, lambda: app.practice.submit_state == "finished")
    assert page.dismiss_button.cget("text") == "结束练习"
    app.practice.dismiss()
    pump(app, lambda: page.state() == "withdrawn")
    assert app.practice.quiz_id == third and app.practice.submit_state == "finished"
    assert len(polls) == 1  # Clicks never advance the independent scan deadline.


@pytest.mark.parametrize("live_app", ["slow"], indirect=True)
def test_close_drains_queued_submit_and_waits_for_generation(live_app):
    app, quizzes, _ = live_app
    assert app.test_entered.wait(2)
    app.practice.choose("A")
    assert app.practice.submit()
    app._close()
    assert app.closing and not app.worker_done.is_set()
    # The queued write must finish even though shutdown has already begun.
    deadline = time.perf_counter() + 1
    done = threading.Event()
    while engine.session._store.attempt_count(quizzes[0]["quiz_id"]) == 0 and time.perf_counter() < deadline:
        done.wait(.001)
    assert engine.session._store.attempt_count(quizzes[0]["quiz_id"]) == 1
    app.test_release.set()
    assert app.worker_done.wait(2)


def test_idle_shutdown_wakes_worker_immediately(live_app):
    app, _, _ = live_app
    started = time.perf_counter()
    app.stop.set()
    assert app.worker_done.wait(.2)
    print(f"idle shutdown: {(time.perf_counter() - started) * 1000:.1f} ms")


def test_repeated_switches_stay_under_budget_without_extra_scans(live_app):
    app, quizzes, polls = live_app
    measurements = []
    for target in [q["quiz_id"] for q in quizzes] * 3:
        started = time.perf_counter()
        assert app._enter_pending(target)
        pump(app, lambda: not app.practice.busy)
        assert app.practice.quiz_id == target
        measurements.append((time.perf_counter() - started) * 1000)
    print("consecutive switches ms: " + ", ".join(f"{ms:.1f}" for ms in measurements))
    assert max(measurements) < 200
    assert len(polls) == 1


@pytest.mark.parametrize("live_app", ["scan"], indirect=True)
def test_pause_during_slow_scan_cannot_block_practice(live_app):
    app, quizzes, _ = live_app
    assert app.test_entered.wait(1)
    assert app._request_action("pause", True)
    assert app._enter_pending(quizzes[1]["quiz_id"])
    pump(app, lambda: app.monitor.paused and not app.practice.busy, timeout=1)
    assert app.practice.quiz_id == quizzes[1]["quiz_id"]
    assert not app.test_release.is_set()


def test_next_shortcut_respects_text_selection_and_input_focus(live_app):
    from tkinter import ttk
    app, quizzes, _ = live_app
    app._navigate_page("practice")
    app.root.deiconify()
    app.root.update()
    page = app._answer_window()
    page.show()
    app.root.update()
    page.question_label.focus_force()
    app.root.update()
    page.question_label.tag_add("sel", "1.0", "end")
    page.question_label.event_generate("<Alt-n>")
    app.root.update()
    assert not app.practice.busy and app.practice.quiz_id == quizzes[0]["quiz_id"]
    entry = ttk.Entry(page.view.body)
    entry.grid()
    entry.focus_force()
    app.root.update()
    entry.event_generate("<Alt-n>")
    app.root.update()
    assert not app.practice.busy
    page.view.canvas.focus_force()
    app.root.update()
    page.view.canvas.event_generate("<Alt-n>")
    pump(app, lambda: app.practice.quiz_id == quizzes[1]["quiz_id"])


@pytest.mark.parametrize("live_app", ["slow"], indirect=True)
@pytest.mark.parametrize("action", ["reload_bank", "bank_cleanup"])
def test_queued_bank_maintenance_finishes_while_model_is_blocked(live_app, monkeypatch, env, action):
    from types import SimpleNamespace
    from bagu_trainer.bank_cleanup import CleanupResult
    app, _, _ = live_app
    assert app.test_entered.wait(1)
    maintained = threading.Event()
    if action == "reload_bank":
        monkeypatch.setattr(engine, "KnowledgeBase", lambda: engine.kb)
        monkeypatch.setattr(engine, "Matcher", lambda _: env["matcher"])
        original = engine.reload_bank
        def reload():
            original()
            maintained.set()
        monkeypatch.setattr(engine, "reload_bank", reload)
        app.jobs.put((action, None))
    else:
        def cleanup(*_args, **_kwargs):
            engine.invalidate_content()
            maintained.set()
            return CleanupResult([], [])
        app.jobs.put((action, (SimpleNamespace(execute=cleanup), object(), False)))
    assert maintained.wait(1), "maintenance waited for model I/O"
    assert not app.test_release.is_set()
    app.test_release.set()
    pump(app, lambda: not app.monitor.generating and not app.generation_busy)
    assert not any(q["kp_id"] == "background-new" for q in app.monitor.list_pending())


@pytest.mark.parametrize("live_app", ["idle", "tied-clock"], indirect=True)
def test_next_cycles_pending_order_and_restores_unsubmitted_drafts(live_app):
    app, quizzes, _ = live_app
    app.practice.choose("B")
    for target in (quizzes[1]["quiz_id"], quizzes[2]["quiz_id"], quizzes[0]["quiz_id"]):
        app.practice.dismiss()
        pump(app, lambda: app.practice.quiz_id == target)
    assert app.answer.get() == "B"


@pytest.mark.parametrize("live_app", ["failure-tail"], indirect=True)
def test_retry_arriving_before_failed_poll_returns_is_retained(live_app, env, monkeypatch):
    app, _, _ = live_app
    assert app.test_entered.wait(1)
    app.test_release.set()
    assert app.test_tail_entered.wait(1)
    pump(app, lambda: app.generation_retryable)
    monkeypatch.setattr(env["gen"], "generate", type(env["gen"])().generate)
    assert app._request_action("retry_generation")
    app.test_tail_release.set()
    pump(app, lambda: not app.generation_retryable and "retry_generation" not in app.pending_actions)
    assert any(q["kp_id"] == "background-new" for q in app.monitor.list_pending())
