"""Real Tk state transitions with gated workers and isolated localhost requests."""
import sys
import threading
from types import SimpleNamespace

import pytest

from release_tests.test_first_use import installation, model_http, KEY, wait_ui
from release_tests.test_ui_theme import cancel_timers

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk interactions")


def close_root(root):
    if root.winfo_exists():
        cancel_timers(root)
        root.destroy()


def test_settings_cancel_test_blocks_repeat_and_stale_save(installation, model_http):
    from release_tests.fakes import make_model_panel
    from bagu_trainer.infrastructure import config
    app = make_model_panel()
    app.root.withdraw()
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    try:
        app.base_url.set(model_http.base_url)
        app.api_key.set(KEY)
        app.timeout.set("1")
        model_http.delay = .15
        app._test()
        worker = app.worker
        app._test()
        assert app.worker is worker
        assert app.test_button.instate(["disabled"])
        assert app.save_button.instate(["disabled"])
        assert not app.cancel_button.instate(["disabled"])
        assert "可以取消" in app.operation_hint.get()
        app._cancel_test()
        app._cancel_test()
        assert app.cancel_button.instate(["disabled"])
        wait_ui(app, lambda: not app.busy)
        assert app.verified is None and app.save_button.instate(["disabled"])
        app._save()
        assert not config.CONFIG_PATH.exists()
        assert len(model_http.requests) <= 1
        model_http.delay = 0
        app._test()
        wait_ui(app, lambda: not app.busy)
        assert app.verified is not None and not app.save_button.instate(["disabled"])
        app._save()
        wait_ui(app, lambda: not app.busy)
        assert "已保存" in app.result_heading.get()
        assert not errors
    finally:
        app.close()
        app.root.destroy()


def test_bank_cancel_close_and_external_destroy_ignore_late_events(installation):
    from bagu_trainer.desktop.tk_runtime import prepare_tk
    from bagu_trainer.desktop.tk_runtime import create_root
    prepare_tk()
    import tkinter as tk
    from bagu_trainer.desktop.bank_setup import BankSetupWindow
    from bagu_trainer.infrastructure.bank_build.control import BuildCancelled
    from bagu_trainer.infrastructure.bank_build.control import BuildProgress
    parent = create_root()
    parent.withdraw()
    entered, release = threading.Event(), threading.Event()
    calls, results, ui_errors = [], [], []
    def build(*args, cancel, progress, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        progress(BuildProgress("download", "late progress"))
        if cancel.is_set():
            raise BuildCancelled("download", "已取消，旧数据保留。")
        return SimpleNamespace(questions=3)
    service = SimpleNamespace(manifest=installation.project / "interview-banks/sources.json", build=build)
    window = BankSetupWindow(parent, service=service, on_success=results.append)
    window.root.withdraw()
    parent.report_callback_exception = lambda *args: ui_errors.append(args)
    try:
        window.source_list.selection_set(next(iter(window.sources)))
        window._start()
        assert entered.wait(2)
        window._start()
        assert calls == [1] and window.start_button.instate(["disabled"])
        window._close()
        assert window.close_when_idle and window.cancel.is_set()
        release.set()
        wait_ui(window, lambda: window.closed)
        window.worker.join(2)
        window._drain()  # a delayed caller is harmless after destruction
        assert not results and not ui_errors
        entered.clear(); release.clear()
        second = BankSetupWindow(parent, service=service, on_success=results.append)
        second.root.withdraw()
        second.source_list.selection_set(next(iter(second.sources)))
        second._start()
        assert entered.wait(2)
        second.root.destroy()
        assert second.closed and second.cancel.is_set()
        release.set()
        second.worker.join(2)
        second._drain()
        parent.update()
        assert not results and not ui_errors
    finally:
        release.set()
        close_root(parent)


def desktop_stub():
    from bagu_trainer.desktop import DesktopApp
    from release_tests.fakes import ui_monitor
    monitor = ui_monitor()
    return DesktopApp(monitor)


def quiz(identifier):
    return {"quiz_id": identifier, "knowledge_point": "缓存", "question": "如何保持一致？",
            "options": {letter: "选项 " + letter for letter in "ABCD"}}


def practice_page(app):
    return app._answer_window()


def test_submission_failure_retry_late_grade_and_duplicate_guard():
    app = desktop_stub()
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(args)
    try:
        app._show_quiz(quiz("one"))
        page = practice_page(app)
        app.answer.set("B")
        assert app.practice.submit() is True
        assert app.practice.submit() is False  # busy: duplicate guarded
        assert app.jobs.get_nowait() == ("submit", ("one", "B"))
        assert app.jobs.empty()
        assert page.dismiss_button.instate(["disabled"])
        app.practice.dismiss()
        assert app.practice.current is not None and "不可取消" in app.answer_hint.get()
        app._apply_grade("one", {"ok": False, "retryable": True, "note": "临时写入失败"}, "B")
        assert page.submit_button.cget("text") == "重试提交"
        assert not page.submit_button.instate(["disabled"])
        assert app.practice.submit() is True
        assert app.jobs.get_nowait() == ("submit", ("one", "B"))
        # Direct entry cannot bypass the submit/switch exclusion.
        app.practice.enter(quiz("two"))
        assert app.practice.quiz_id == "one" and app.practice.submit_state == "submitting"
        app._apply_grade("other", {"ok": True, "correct": True, "correct_answer": "B"}, "B")
        assert app.practice.submit_state == "submitting"
        app._apply_grade("one", {"ok": True, "correct": False, "correct_answer": "A", "analysis": "缓存失效解析",
                               "schedule": {"interval_days": 2, "next_review_at": 1800000000}}, "B")
        for word in ("回答错误", "你的答案：B", "正确答案：A", "缓存失效解析", "间隔 2 天", "北京时间", "已记录作答"):
            assert word in app.feedback.get()
        assert page.submit_button.instate(["disabled"])
        assert app.practice.submit() is False  # finished: cannot resubmit
        assert app.jobs.empty()
        app.practice.dismiss()
        app.root.update()
        assert not errors
    finally:
        close_root(app.root)


def test_generation_busy_disables_retry_and_close_waits_for_worker(monkeypatch):
    from tkinter import messagebox
    app = desktop_stub()
    entered, release = threading.Event(), threading.Event()
    def blocked_worker():
        entered.set()
        assert release.wait(3)
        # The worker posts data even if a close has been requested.
        app.events.put(("quiz", quiz("late")))
    app.worker_thread = threading.Thread(target=blocked_worker)
    app.worker_thread.start()
    assert entered.wait(1)
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    try:
        app.events.put(("generation", {"busy": True, "project": "C:/project"}))
        app._drain()
        assert "不支持取消" in app.task_hint.get()
        assert app.retry_button.instate(["disabled"])
        assert app._request_action("resume")  # Existing practice stays available during generation.
        app.events.put(("generation", {"busy": False, "retryable": True, "reason": "HTTP 429，请稍后重试。"}))
        app._drain()
        assert "失败" in app.listen_state.get() and "429" in app.task_hint.get()
        assert app._request_action("retry_generation")
        assert not app._request_action("retry_generation")
        app.events.put(("generation", {"busy": True}))
        app._drain()
        app._close()
        assert app.closing and not app.closed and app.stop.is_set()
        release.set()
        app.worker_thread.join(2)
        app._drain()
        assert app.closed and app.practice.current is None
        app._drain()
    finally:
        release.set()
        if not app.closed:
            close_root(app.root)


def test_real_desktop_worker_converts_submit_exception_without_tk_access():
    app = desktop_stub()
    main_thread = threading.get_ident()
    finished = threading.Event()
    def fail_submit(*args):
        assert threading.get_ident() != main_thread
        finished.set()
        raise OSError("secret-error-body")
    app.monitor.engine = SimpleNamespace(submit_answer=fail_submit,
        load_config=lambda: {"project_roots": []}, get_learning_stats=lambda: {"ok": False}, PROJECT_ROOT="C:/none")
    from release_tests.fakes import LearningFake
    app.monitor.engine.learning = LearningFake(app.monitor.engine)
    app.monitor.poll = lambda: {"state": "watching", "projects": 0}
    app.monitor.take_quiz = lambda: None
    app.monitor.list_pending = lambda: []
    try:
        app._show_quiz(quiz("worker"))
        app.answer.set("A")
        assert app.practice.submit() is True
        context = app._worker_context()
        assert not hasattr(context, "root") and not hasattr(context, "answer")
        from bagu_trainer.desktop import DesktopApp
        app.worker_thread = threading.Thread(target=DesktopApp._worker, args=(context,))
        app.worker_thread.start()
        assert finished.wait(2)
        wait_ui(app, lambda: app.practice.submit_state == "failed")
        assert "OSError" in app.feedback.get() and "secret-error-body" not in app.feedback.get()
        assert not practice_page(app).submit_button.instate(["disabled"])
        assert app._request_action("resume")
        assert not app._request_action("resume")
    finally:
        app.stop.set()
        app.worker_thread.join(2)
        close_root(app.root)
