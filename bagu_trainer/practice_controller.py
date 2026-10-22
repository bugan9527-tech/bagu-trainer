"""Practice controller: the single delivery entry point for the practice center.

Owns question number, selected option, submit state, grade and analysis, plus the
deferred display queue.  It is UI-independent: the page renders its state and
forwards user intent; the application wires submit/dismiss callbacks to the
worker.  It converges the previous ``history_deferred`` / ``deferred_quiz`` /
``monitor.active`` UI-side bookkeeping into one ordered queue plus one "current"
question.

The Tk variables (answer/feedback/answer_hint) are owned by the application root
and passed in, so they survive page switches; the controller only reads/writes
their string values.

Rules preserved:
- Automatic quizzes and manual repractice never overwrite each other; a new quiz
  arriving while another is current is queued by quiz id (deduped).
- The selected option for the current question survives page switches (a per-quiz
  draft), so hiding the page does not drop the answer.
- A submission in flight belongs to the application task layer; switching pages
  does not cancel it, and the late grade is written back by quiz id.
- Viewing the pending list never delivers; only an explicit enter/submit drives
  the ready -> delivered transition through the worker.
"""
from datetime import datetime, timedelta, timezone


class PracticeController:
    def __init__(self, answer, feedback, answer_hint, *,
                 on_submit=None, on_dismiss=None, on_changed=None, can_submit=None):
        self.answer = answer            # tk.StringVar (current selection)
        self.feedback = feedback        # tk.StringVar (result/analysis)
        self.answer_hint = answer_hint  # tk.StringVar (footer guidance)
        self.current = None             # public quiz dict currently displayed
        self.manual = False             # whether current is a manual repractice
        self.submit_state = "ready"     # ready | submitting | finished | failed
        self.entering_quiz_id = None    # worker request; mutually exclusive with submit
        self.grade = None
        self.submitted_answer = None
        self.deferred = []              # ordered quiz dicts awaiting display
        self.drafts = {}                # quiz_id -> selected letter (session only)
        self.closed = False
        self.on_submit = on_submit or (lambda quiz_id, answer: None)
        self.can_submit = can_submit or (lambda quiz_id: True)
        self.on_dismiss = on_dismiss or (lambda quiz_id: None)
        self.on_changed = on_changed or (lambda: None)
        self._reset_state()

    # -- read state -----------------------------------------------------------
    @property
    def quiz_id(self):
        return self.current["quiz_id"] if self.current else None

    @property
    def busy(self):
        return self.submit_state == "submitting" or self.entering_quiz_id is not None

    def begin_enter(self, quiz_id):
        """Reserve the question switch before handing the request to a worker."""
        if self.closed or self.busy or not quiz_id:
            return False
        self.entering_quiz_id = quiz_id
        self._changed()
        return True

    def finish_enter(self, quiz=None):
        """Release on every worker outcome; a failed switch leaves all state intact."""
        target = self.entering_quiz_id
        self.entering_quiz_id = None
        if target is not None and quiz and quiz.get("quiz_id") == target:
            self.enter(quiz)
        else:
            self._changed()

    def _reset_state(self):
        self.feedback.set("选择一个选项后提交，即可查看解析。")
        self.answer_hint.set("尚未提交：请选择一个选项。当前没有可取消的提交。")
        self.answer.set("")

    def _changed(self):
        if self.on_changed is not None:
            self.on_changed()

    # -- delivery -------------------------------------------------------------
    def deliver(self, quiz, *, manual=False):
        """Single entry point for an automatic or manual quiz.  If nothing is
        current it becomes current; otherwise it is queued by quiz id."""
        if not quiz:
            return
        quiz_id = quiz["quiz_id"]
        if self.current is not None and self.current["quiz_id"] == quiz_id:
            self._changed()
            return
        if any(item["quiz_id"] == quiz_id for item in self.deferred):
            self._changed()
            return
        item = dict(quiz)
        if manual or quiz.get("manual_review"):
            item["manual_review"] = True
        if self.current is None:
            self._set_current(item, manual=manual or bool(item.get("manual_review")))
        else:
            self.deferred.append(item)
        self._changed()

    def enter(self, quiz):
        """按题进入: explicitly select a pending quiz (from the list).  A current
        unanswered quiz is parked back at the front of the queue so it is not
        lost; the entered quiz becomes current."""
        if self.closed or self.busy or not quiz:
            return
        quiz_id = quiz["quiz_id"]
        item = dict(quiz)
        if self.current is not None and self.current["quiz_id"] == quiz_id:
            self._changed()
            return
        self.deferred = [q for q in self.deferred if q["quiz_id"] != quiz_id]
        if self.current is not None and self.submit_state != "finished":
            self._save_draft()
            self.deferred.insert(0, self.current)
        self._set_current(item, manual=bool(item.get("manual_review")))
        self._changed()

    def _save_draft(self):
        if self.current is not None:
            self.drafts[self.current["quiz_id"]] = self.answer.get()

    def _set_current(self, quiz, *, manual):
        self.current = quiz
        self.manual = manual
        self.submit_state = "ready"
        self.grade = None
        self.submitted_answer = None
        self.feedback.set("选择一个选项后提交，即可查看解析。")
        self.answer_hint.set("尚未提交：请选择一个选项。当前没有可取消的提交。")
        self.answer.set(self.drafts.get(quiz["quiz_id"], ""))

    def clear(self):
        """Drop the current question and any deferred queue (teardown only)."""
        self.entering_quiz_id = None
        self.current = None
        self.manual = False
        self.submit_state = "ready"
        self.grade = None
        self.submitted_answer = None
        self.deferred = []
        self.drafts.clear()
        self._reset_state()

    # -- actions --------------------------------------------------------------
    def choose(self, letter):
        if self.closed or self.busy or self.submit_state == "finished" or self.current is None:
            return
        self.answer.set(letter)
        self._changed()

    def submit(self):
        """Validate and hand off to the application (worker).  Returns True when
        a submit job was accepted, False otherwise (no answer / already busy)."""
        if self.closed or self.current is None:
            return False
        if self.busy or self.submit_state == "finished":
            return False
        if not self.can_submit(self.quiz_id):
            return False
        answer = self.answer.get()
        if not answer:
            self.feedback.set("尚未选择答案：请先选择 A、B、C 或 D，再提交。")
            self._changed()
            return False
        previous = (self.submit_state, self.answer_hint.get(), self.feedback.get())
        self.submit_state = "submitting"
        self.answer_hint.set("当前不可取消：判分与记录写入完成后可关闭。")
        self.feedback.set("进行中：正在判分并保存学习记录，请勿重复提交。")
        self._changed()
        try:
            accepted = self.on_submit(self.current["quiz_id"], answer)
        except Exception:
            self.submit_state, hint, feedback = previous
            self.answer_hint.set(hint)
            self.feedback.set(feedback)
            self._changed()
            raise
        if accepted is False:
            self.submit_state, hint, feedback = previous
            self.answer_hint.set(hint)
            self.feedback.set(feedback)
            self._changed()
            return False
        return True

    def apply_grade(self, grade, submitted_answer=None):
        """Write a grade back for the current quiz.  A late grade whose quiz id
        no longer matches is ignored (the caller checks the id first)."""
        if self.closed or self.current is None:
            return
        self.grade = grade
        self.submitted_answer = submitted_answer
        if not grade.get("ok"):
            note = grade.get("note") or "提交失败，请重试。"
            terminal = any(word in note for word in ("已作答", "已过期", "不存在", "重复提交"))
            retryable = grade.get("retryable", True) and not terminal
            self.submit_state = "failed" if retryable else "finished"
            self.feedback.set("提交失败：" + note)
            self.answer_hint.set("可点击“重试提交”；沿用同一题号，已有记录不会重复计数。" if retryable
                                 else "该题不能再次提交，请关闭并继续其他练习。")
            self._changed()
            return
        self.submit_state = "finished"
        schedule = grade.get("schedule", {})
        days = schedule.get("interval_days", 0)
        next_time = schedule.get("next_review_at")
        if next_time is not None:
            review = datetime.fromtimestamp(next_time, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（北京时间）")
        else:
            review = "按已记录的复习间隔安排"
        answer = submitted_answer or grade.get("your_answer") or self.answer.get()
        self.feedback.set((("作答结果：回答正确" if grade["correct"] else "作答结果：回答错误") +
                           f"\n你的答案：{answer}\n正确答案：{grade['correct_answer']}"
                           f"\n\n解析：\n{grade.get('analysis') or '暂无解析。'}"
                           f"\n\n复习安排：间隔 {days:g} 天\n下次复习：{review}\n已记录作答，重复提交已阻止。"))
        self.answer_hint.set("成功：作答和复习安排已保存。可以继续下一题，不能重复提交。")
        self._changed()

    def dismiss(self):
        """Close the current question and advance to the next deferred quiz, or
        empty.  Called when the user asks to move on (never during a submit)."""
        if self.closed or self.current is None:
            return
        if self.busy:
            if self.entering_quiz_id is None:
                self.answer_hint.set("当前不可取消：正在保存记录，完成后可继续。")
            self._changed()
            return
        dismissed_id = self.current["quiz_id"]
        self.on_dismiss(dismissed_id)
        if self.deferred:
            next_quiz = self.deferred.pop(0)
            self._set_current(next_quiz, manual=bool(next_quiz.get("manual_review")))
        else:
            self.current = None
            self.manual = False
            self.submit_state = "ready"
            self.grade = None
            self.submitted_answer = None
            self.feedback.set("当前没有待答题。新题到达后会在练习中心显示。")
            self.answer_hint.set("尚未提交：请选择一个选项。当前没有可取消的提交。")
            self.answer.set("")
        self._changed()

    def close(self):
        self.closed = True
        self.clear()
