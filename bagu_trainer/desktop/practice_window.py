"""One nonmodal answer window; all answer state belongs to PracticeController."""
import tkinter as tk
from tkinter import ttk

from bagu_trainer.desktop.ui_layout import ScrollableFrame
from bagu_trainer.desktop.ui_layout import FlowActions
from bagu_trainer.desktop.ui_layout import wrapped_label
from bagu_trainer.desktop.ui_state import window_var
from bagu_trainer.desktop.ui_state import cancel_timer
from bagu_trainer.desktop.ui_theme import SPACE
from bagu_trainer.desktop.ui_theme import PRIMARY
from bagu_trainer.desktop.ui_theme import SECTION
from bagu_trainer.desktop.ui_theme import MUTED
from bagu_trainer.desktop.ui_copy import ReadOnlyText


class PracticeWindow(tk.Toplevel):
    def __init__(self, parent, theme, controller, *, on_destroy=None):
        super().__init__(parent)
        self.withdraw()
        self.theme, self.controller = theme, controller
        self.closed = False
        self._on_destroy = on_destroy
        self.focus_timer = None
        self._rendered_quiz_id = None
        self.title("练习答题 · AI 八股训练器")
        theme.window(self, (900, 700), (480, 400))
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        px = theme.px
        content = ttk.Frame(self, padding=px(SPACE.lg))
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        self.notice = window_var(self)
        wrapped_label(content, theme, textvariable=self.notice, style=MUTED).grid(row=0, column=0, sticky="ew")
        self.view = ScrollableFrame(content, theme)
        body = self.view.body
        body.columnconfigure(0, weight=1)
        self.question_heading = wrapped_label(body, theme, text="当前题目", style=SECTION)
        self.question_heading.grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.project = window_var(self, "")
        wrapped_label(body, theme, textvariable=self.project, style=MUTED).grid(
            row=1, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.question_label = ReadOnlyText(body, theme)
        self.question_label.grid(row=2, column=0, sticky="ew", pady=px((0, SPACE.sm)))

        self.option_buttons = []
        self.option_texts = []
        for index, letter in enumerate("ABCD"):
            option = ttk.Frame(body)
            option.columnconfigure(1, weight=1)
            option.grid(row=3 + index, column=0, sticky="ew", pady=px(SPACE.xs))
            button = ttk.Radiobutton(option, text=letter + ".", value=letter, variable=controller.answer,
                                     style="Quiz.TRadiobutton", command=lambda l=letter: controller.choose(l))
            button.grid(row=0, column=0, sticky="nw", padx=px((0, SPACE.sm)))
            text = ReadOnlyText(option, theme)
            text.grid(row=0, column=1, sticky="ew")
            self.option_buttons.append(button)
            self.option_texts.append(text)

        wrapped_label(body, theme, text="作答结果与解析", style=SECTION).grid(
            row=7, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        self.feedback_label = ReadOnlyText(body, theme, textvariable=controller.feedback)
        self.feedback_label.grid(row=8, column=0, sticky="ew", pady=px((0, SPACE.lg)))

        # -- fixed footer -----------------------------------------------------
        footer = self.footer = ttk.Frame(self, padding=px((SPACE.lg, 0, SPACE.lg, SPACE.md)))
        footer.grid(row=1, column=0, sticky="ew", pady=px((SPACE.md, 0)))
        footer.columnconfigure(0, weight=1)
        self.keyboard_hint = wrapped_label(footer, theme, text="A–D 选择 · Ctrl+Enter 提交 · Alt+N 下一题 · 正文 Ctrl+A 全选、Ctrl+C 复制；选中文本时不切换选项", style=MUTED)
        self.keyboard_hint.grid(row=0, column=0, sticky="ew")
        actions = FlowActions(footer, theme)
        actions.grid(row=1, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.submit_button = actions.add(ttk.Button(actions, text="提交答案", style=PRIMARY, command=self._submit))
        self.dismiss_button = actions.add(ttk.Button(actions, text="结束练习", command=self._dismiss))
        wrapped_label(footer, theme, textvariable=controller.answer_hint, style=MUTED).grid(
            row=2, column=0, sticky="ew")

        self.view.grid(row=1, column=0, sticky="nsew")
        self._bind_keys()
        self.bind("<Destroy>", self._destroyed, add="+")
        self.refresh()

    def show(self, notice=None):
        if self.closed:
            return
        if notice is not None:
            self.notice.set(notice)
        self.refresh()
        self.deiconify()
        self.lift()
        cancel_timer(self, self.focus_timer)
        self.focus_timer = self.after_idle(self._focus_reader)

    def _focus_reader(self):
        self.focus_timer = None
        if not self.closed and self.state() != "withdrawn":
            self.view.canvas.focus_set()

    def hide(self):
        if self.closed:
            return
        self.controller.save_draft()
        cancel_timer(self, self.focus_timer)
        self.focus_timer = None
        self.withdraw()

    def _destroyed(self, event):
        if event.widget is self:
            self.closed = True
            cancel_timer(self, self.focus_timer)
            self.focus_timer = None
            if self._on_destroy:
                self._on_destroy(self)
            self._on_destroy = None

    def _on_reading_surface(self, event=None):
        if self.closed or not self.winfo_ismapped():
            return False
        widget = self.focus_get()
        return widget in (self, self.view, self.view.canvas, self.question_label,
                          self.feedback_label, self.submit_button, self.dismiss_button,
                          *self.option_buttons, *self.option_texts)

    def _bind_keys(self):
        # A toplevel bindtag only sees its own descendants. No root/bind_all
        # shortcuts, so other pages, forms and native text copy remain intact.
        self._key_bindings = []
        def bind(sequence, callback):
            self._key_bindings.append((sequence, self.bind(sequence, callback, add="+")))
        def selected_text():
            focus = self.focus_get()
            return isinstance(focus, ReadOnlyText) and bool(focus.tag_ranges("sel"))
        for letter in "ABCD":
            def choose(event, letter=letter):
                if event.state & (0x0004 | 0x0008 | 0x20000) or not self._on_reading_surface() or selected_text():
                    return None
                self.controller.choose(letter)
                self.view.reveal(self.option_buttons["ABCD".index(letter)])
                return "break"
            bind("<KeyPress-" + letter.lower() + ">", choose)
            bind("<KeyPress-" + letter + ">", choose)
        def submit(event):
            if event.state & (0x0008 | 0x20000) or not self._on_reading_surface():
                return None
            self._submit()
            return "break"
        def next_quiz(event):
            if event.state & (0x0004 | 0x0001) or not self._on_reading_surface() or selected_text():
                return None
            self._dismiss()
            return "break"
        bind("<Control-Return>", submit)
        bind("<Alt-n>", next_quiz)
        def cleanup(event):
            if event.widget is self:
                for sequence, binding in self._key_bindings:
                    self.unbind(sequence, binding)
                self._key_bindings.clear()
        self.bind("<Destroy>", cleanup, add="+")

    def refresh(self):
        if self.closed:
            return
        controller = self.controller
        quiz = controller.current
        changed_quiz = self._rendered_quiz_id != controller.quiz_id
        self._rendered_quiz_id = controller.quiz_id
        if quiz is None:
            self.question_heading.configure(text="当前题目")
            self.question_label.set_text("当前没有待答题。")
            self.project.set("")
            for button, text in zip(self.option_buttons, self.option_texts):
                text.set_text("")
                button.state(["disabled"])
        else:
            self.question_heading.configure(text=(quiz.get("knowledge_point") or "当前题目") + ("（历史重练）" if controller.manual else ""))
            self.question_label.set_text(quiz.get("question") or "")
            self.project.set(quiz.get("project_root") or "当前练习未提供项目路径")
            options = quiz.get("options") or {}
            for text, letter in zip(self.option_texts, "ABCD"):
                text.set_text(options.get(letter, ""))
        self._sync_controls()
        if changed_quiz:
            self.view.canvas.yview_moveto(0)
        return "break"

    def _sync_controls(self):
        if self.closed:
            return
        controller = self.controller
        state = controller.submit_state
        submitting = state == "submitting"
        switching = controller.entering_quiz_id is not None
        finished = state == "finished"
        has_current = controller.current is not None
        self.submit_button.configure(text=("正在切题…" if switching else "正在提交…" if submitting else
                                           ("重试提交" if state == "failed" else
                                            ("已记录作答" if finished else "提交答案"))))
        self.submit_button.state(["disabled"] if controller.busy or finished or not has_current else ["!disabled"])
        self.dismiss_button.configure(text="提交中…" if submitting else "正在切题…" if switching else
                                      "下一题" if controller.has_next else "结束练习")
        self.dismiss_button.state(["disabled"] if controller.busy else ["!disabled"])
        for button in self.option_buttons:
            button.state(["disabled"] if controller.busy or finished or not has_current else ["!disabled"])

    def _submit(self):
        self.controller.submit()

    def _dismiss(self):
        if self.controller.current is None and not self.controller.busy:
            self.hide()
        else:
            self.controller.dismiss()
