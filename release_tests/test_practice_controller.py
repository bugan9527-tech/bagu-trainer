"""PracticeController and pending-list delivery tests (no network/model)."""
import sys

import pytest


def test_review_time_uses_ascii_format_on_english_windows(monkeypatch):
    import bagu_trainer.desktop.practice_controller as module
    original = module.datetime
    class Moment:
        def strftime(self, fmt):
            assert fmt.isascii(), "Windows locale cannot encode Unicode strftime literals"
            return original.fromtimestamp(1).strftime(fmt)
    class Clock:
        @staticmethod
        def fromtimestamp(*args):
            return Moment()
    class Value:
        def __init__(self): self.value = ""
        def get(self): return self.value
        def set(self, value): self.value = value
    monkeypatch.setattr(module, "datetime", Clock)
    ctl = module.PracticeController(Value(), Value(), Value())
    ctl.deliver({"quiz_id": "locale-fixture"})
    ctl.apply_grade({"ok": True, "correct": True, "correct_answer": "A", "analysis": "合成解析",
                     "schedule": {"interval_days": 1, "next_review_at": 1}}, "A")
    assert ctl.submit_state == "finished" and "北京时间" in ctl.feedback.get()

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk practice")


def controller():
    import tkinter as tk
    from bagu_trainer.desktop.practice_controller import PracticeController
    root = tk.Tk()
    root.withdraw()
    answer = tk.StringVar(root)
    feedback = tk.StringVar(root)
    hint = tk.StringVar(root)
    submit_calls, dismiss_calls = [], []
    ctl = PracticeController(answer, feedback, hint,
                             on_submit=lambda q, a: submit_calls.append((q, a)),
                             on_dismiss=lambda q: dismiss_calls.append(q))
    return root, ctl, submit_calls, dismiss_calls


def quiz(identifier, **kw):
    base = {"quiz_id": identifier, "knowledge_point": "缓存", "question": "题干？",
            "options": {letter: f"选项{letter}" for letter in "ABCD"}}
    base.update(kw)
    return base


def test_auto_and_manual_never_overwrite_each_other():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("auto"))
        assert ctl.quiz_id == "auto" and not ctl.manual
        ctl.deliver(quiz("manual", manual_review=True), manual=True)
        assert ctl.quiz_id == "auto" and not ctl.manual
        assert [q["quiz_id"] for q in ctl.deferred] == ["manual"]
        ctl.answer.set("B")
        ctl.dismiss()
        assert ctl.quiz_id == "manual" and ctl.manual
        assert dismiss == ["auto"]
    finally:
        ctl.close()
        root.destroy()


def test_enter_parks_current_and_switches():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("one"))
        ctl.answer.set("A")
        ctl.enter(quiz("two"))
        assert ctl.quiz_id == "two" and ctl.answer.get() == ""
        assert [q["quiz_id"] for q in ctl.deferred] == ["one"]
        # The parked draft is restored when we return to it.
        ctl.dismiss()
        assert ctl.quiz_id == "one" and ctl.answer.get() == "A"
    finally:
        ctl.close()
        root.destroy()


def test_submit_deduped_and_grade_written_back_by_id():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("one"))
        ctl.answer.set("B")
        assert ctl.submit() is True
        assert ctl.submit() is False  # busy
        assert submit == [("one", "B")]
        ctl.apply_grade({"ok": True, "correct": True, "correct_answer": "B",
                         "analysis": "解析", "schedule": {"interval_days": 1}}, "B")
        assert ctl.submit_state == "finished" and "回答正确" in ctl.feedback.get()
        assert ctl.submit() is False  # finished
    finally:
        ctl.close()
        root.destroy()


def test_submit_failure_retryable_then_terminal():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("one"))
        ctl.answer.set("C")
        ctl.submit()
        ctl.apply_grade({"ok": False, "retryable": True, "note": "临时写入失败"}, "C")
        assert ctl.submit_state == "failed" and "重试" in ctl.answer_hint.get()
        ctl.submit()
        ctl.apply_grade({"ok": False, "retryable": True, "note": "该题已作答，重复提交已拒绝"}, "C")
        assert ctl.submit_state == "finished" and "不能再次提交" in ctl.answer_hint.get()
    finally:
        ctl.close()
        root.destroy()


def test_dismiss_advances_deferred_and_clears():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("one"))
        ctl.deliver(quiz("two"))
        ctl.deliver(quiz("one"))  # duplicate ignored
        assert len(ctl.deferred) == 1
        ctl.dismiss()
        assert ctl.quiz_id == "two"
        ctl.dismiss()
        assert ctl.quiz_id is None and "没有待答题" in ctl.feedback.get()
        assert dismiss == ["one", "two"]
    finally:
        ctl.close()
        root.destroy()


def test_dismiss_blocked_while_submitting():
    root, ctl, submit, dismiss = controller()
    try:
        ctl.deliver(quiz("one"))
        ctl.answer.set("D")
        ctl.submit()
        ctl.dismiss()
        assert ctl.quiz_id == "one" and "不可取消" in ctl.answer_hint.get()
        assert dismiss == []
    finally:
        ctl.close()
        root.destroy()
