"""Embedded practice page: pending list + current question + fixed submit footer.

The page is a pure view over a PracticeController.  Its body scrolls; submit /
next / dismiss actions stay fixed at the bottom.  Keyboard shortcuts A-D and
Ctrl+Enter are bound only while this page is the active workspace and focus is
on the reading surface (not the list).

Wide workspaces show the pending list beside the question (list + detail);
narrow workspaces stack the list above the question.
"""
from tkinter import ttk

from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var
from .ui_theme import SPACE, PRIMARY, SECTION, MUTED, CAPTION


class PracticePage(ttk.Frame):
    def __init__(self, parent, theme, controller, *, on_enter=None):
        super().__init__(parent)
        self.theme = theme
        self.controller = controller
        self.on_enter = on_enter or (lambda quiz_id: None)
        px = theme.px
        self.closed = False
        self.wide = False
        self.compact_list = False
        self.compact_expanded = False
        self._pending = []
        self._rendered_quiz_id = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.workspace = ttk.Frame(self)
        self.workspace.grid(row=0, column=0, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self.workspace.bind("<Configure>", self._resize, add="+")
        self.compact_toggle = ttk.Button(self.workspace, text="查看待答列表", command=self._toggle_compact)

        # -- pending list column ---------------------------------------------
        self.list_column = ttk.Frame(self.workspace)
        self.list_column.columnconfigure(0, weight=1)
        self.list_column.rowconfigure(3, weight=1)
        self.summary = window_var(self, "待答题 0 · 待复习 0")
        wrapped_label(self.list_column, theme, textvariable=self.summary, style=SECTION).grid(
            row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.list_help = wrapped_label(self.list_column, theme, text="选择待答列表中的任意一题进入作答；查看列表不会把题目标记为已交付。",
                                       style=MUTED)
        self.list_help.grid(row=1, column=0, sticky="ew", pady=px((0, SPACE.md)))
        self.list_label = wrapped_label(self.list_column, theme, text="待答列表", style=CAPTION)
        self.list_label.grid(row=2, column=0, sticky="ew")
        listing = ttk.Frame(self.list_column)
        listing.grid(row=3, column=0, sticky="nsew", pady=px(SPACE.xs))
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.pending_list = ttk.Treeview(listing, columns=("kp",), show="tree headings",
                                         selectmode="browse", height=4)
        self.pending_list.heading("#0", text="题目")
        self.pending_list.heading("kp", text="知识点")
        self.pending_list.column("#0", width=px(260), minwidth=px(140))
        self.pending_list.column("kp", width=px(120), minwidth=px(80), stretch=False)
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.pending_list.yview)
        self.pending_list.configure(yscrollcommand=vertical.set)
        self.pending_list.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        self.pending_list.bind("<<TreeviewSelect>>", lambda _event: self._sync_controls())
        self.pending_list.bind("<Double-1>", lambda _event: self._enter_selected())
        self.pending_list.bind("<Return>", lambda _event: self._enter_selected())
        self.enter_button = ttk.Button(listing, text="进入本题", command=self._enter_selected)
        self.enter_button.grid(row=1, column=0, sticky="w", pady=px((SPACE.sm, 0)))
        self._pending_ids = []

        # -- reading surface (question + options + feedback) ------------------
        self.view = ScrollableFrame(self.workspace, theme)
        body = self.view.body
        body.columnconfigure(0, weight=1)
        self.question_heading = wrapped_label(body, theme, text="当前题目", style=SECTION)
        self.question_heading.grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.project = window_var(self, "")
        wrapped_label(body, theme, textvariable=self.project, style=MUTED).grid(
            row=1, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.question_label = wrapped_label(body, theme, text="")
        self.question_label.grid(row=2, column=0, sticky="ew", pady=px((0, SPACE.sm)))

        self.option_buttons = []
        for index, letter in enumerate("ABCD"):
            button = ttk.Radiobutton(body, text="", value=letter, variable=controller.answer,
                                     style="Quiz.TRadiobutton", command=lambda l=letter: controller.choose(l))
            button.grid(row=3 + index, column=0, sticky="ew", pady=px(SPACE.xs))
            self.option_buttons.append(button)

        def wrap_options(event):
            if event.width < px(80):
                return
            width = max(1, event.width - px(56))
            if self.theme.style.lookup("Quiz.TRadiobutton", "wraplength") != width:
                self.theme.style.configure("Quiz.TRadiobutton", wraplength=width)
        body.bind("<Configure>", wrap_options, add="+")

        wrapped_label(body, theme, text="作答结果与解析", style=SECTION).grid(
            row=7, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        self.feedback_label = wrapped_label(body, theme, textvariable=controller.feedback)
        self.feedback_label.grid(row=8, column=0, sticky="ew", pady=px((0, SPACE.lg)))

        # -- fixed footer -----------------------------------------------------
        footer = self.footer = ttk.Frame(self)
        footer.grid(row=1, column=0, sticky="ew", pady=px((SPACE.md, 0)))
        footer.columnconfigure(0, weight=1)
        self.keyboard_hint = wrapped_label(footer, theme, text="A–D 选择 · Ctrl+Enter 提交 · 下一题继续", style=MUTED)
        self.keyboard_hint.grid(row=0, column=0, sticky="ew")
        actions = FlowActions(footer, theme)
        actions.grid(row=1, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.submit_button = actions.add(ttk.Button(actions, text="提交答案", style=PRIMARY, command=self._submit))
        self.dismiss_button = actions.add(ttk.Button(actions, text="下一题 / 关闭", command=self._dismiss))
        wrapped_label(footer, theme, textvariable=controller.answer_hint, style=MUTED).grid(
            row=2, column=0, sticky="ew")

        self._layout()
        self._bind_keys()
        controller.on_changed = self._on_controller_changed
        self.refresh()

    # -- responsive split -----------------------------------------------------
    def _resize(self, event):
        if event.widget is not self.workspace:
            return
        wide = event.width >= self.theme.px(900) and bool(self._pending)
        # Use the stable window height. Basing this on the child's requested
        # height makes hiding help text resize the child back across the same
        # threshold, causing an endless Configure/layout feedback loop.
        compact = not wide and self.winfo_toplevel().winfo_height() < self.theme.px(700)
        if wide != self.wide or compact != self.compact_list:
            self.wide, self.compact_list = wide, compact
            self._layout()

    def _layout(self):
        split = self.wide
        self.pending_list.configure(height=1 if self.compact_list else 4)
        if self.compact_list:
            self.list_help.grid_remove()
            self.list_label.grid_remove()
        else:
            self.list_help.grid()
            self.list_label.grid()
        self.workspace.columnconfigure(0, weight=0 if split else 1, uniform="practice" if split else "")
        self.workspace.columnconfigure(1, weight=1 if split else 0, uniform="practice" if split else "")
        self.compact_toggle.grid_remove()
        if self.compact_list and not split:
            self.keyboard_hint.grid_remove()
            self.workspace.rowconfigure(0, weight=0)
            self.workspace.rowconfigure(1, weight=1)
            self.compact_toggle.configure(text="返回当前题目" if self.compact_expanded else f"查看待答列表（{len(self._pending)}）")
            self.compact_toggle.grid(row=0, column=0, sticky="w", pady=(0, self.theme.px(SPACE.xs)))
            if self.compact_expanded:
                self.view.grid_remove()
                self.list_column.grid(row=1, column=0, sticky="nsew", padx=0)
            else:
                self.list_column.grid_remove()
                self.view.grid(row=1, column=0, sticky="nsew")
            return
        self.keyboard_hint.grid()
        if split:
            self.workspace.rowconfigure(0, weight=1)
            self.workspace.rowconfigure(1, weight=0)
            self.list_column.grid(row=0, column=0, sticky="nsew", padx=(0, self.theme.px(SPACE.lg)))
            self.view.grid(row=0, column=1, sticky="nsew")
        else:
            self.workspace.rowconfigure(0, weight=0)
            self.workspace.rowconfigure(1, weight=1)
            self.list_column.grid(row=0, column=0, sticky="ew", padx=0)
            self.view.grid(row=1, column=0, sticky="nsew")

    def _toggle_compact(self):
        self.compact_expanded = not self.compact_expanded
        self._layout()

    # -- keyboard -------------------------------------------------------------
    def _bind_keys(self):
        # Tk events propagate through the toplevel bindtag, not through parent
        # Frames. Bind there and guard the actual visible practice focus.
        top = self.winfo_toplevel()
        bindings = []
        for letter in "ABCD":
            def choose(event, selected=letter):
                if event.state & (0x0004 | 0x0008 | 0x20000):
                    return None
                if not self._on_reading_surface(event):
                    return None
                if self.controller.submit_state not in ("submitting", "finished"):
                    self.controller.choose(selected)
                    self.view.reveal(self.option_buttons["ABCD".index(selected)])
                return "break"
            for sequence in ("<KeyPress-" + letter.lower() + ">", "<KeyPress-" + letter + ">"):
                bindings.append((sequence, top.bind(sequence, choose, add="+")))

        def keyboard_submit(event):
            if event.state & (0x0008 | 0x20000):
                return None
            if not self._on_reading_surface(event):
                return None
            self._submit()
            return "break"
        bindings.append(("<Control-Return>", top.bind("<Control-Return>", keyboard_submit, add="+")))
        def cleanup(event):
            if event.widget is self:
                for sequence, binding in bindings:
                    top.unbind(sequence, binding)
                bindings.clear()
        self.bind("<Destroy>", cleanup, add="+")

    def _on_reading_surface(self, event):
        if not self.winfo_ismapped():
            return False
        widget = self.focus_get()
        if widget is self or widget is self.view.canvas or widget is self.view:
            return True
        return widget in self.option_buttons or widget is self.submit_button

    # -- pending list ---------------------------------------------------------
    def set_pending(self, items, due_reviews):
        """Render the read-only pending list.  Items are public quiz dicts."""
        self._pending = list(items)
        self._pending_ids = [item["quiz_id"] for item in items]
        selection = set(self.pending_list.selection())
        self.pending_list.delete(*self.pending_list.get_children())
        for item in items:
            kp = item.get("knowledge_point") or "未记录知识点"
            question = (item.get("question") or "").replace("\n", " ")
            manual = "（重练）" if item.get("manual_review") else ""
            self.pending_list.insert("", "end", iid=item["quiz_id"],
                                     text=(question[:60] + ("…" if len(question) > 60 else "")) + manual,
                                     values=(kp,))
        if selection:
            for iid in selection:
                if self.pending_list.exists(iid):
                    self.pending_list.selection_add(iid)
        self.summary.set(f"待答题 {len(items)} · 待复习 {due_reviews}")
        self._resize_from_pending()
        if self.compact_list:
            self._layout()
        self._sync_controls()

    def _resize_from_pending(self):
        wide = self.workspace.winfo_width() >= self.theme.px(900) and bool(self._pending)
        if wide != self.wide:
            self.wide = wide
            self._layout()

    def _enter_selected(self):
        if self.controller.closed or self.controller.busy:
            return
        selection = self.pending_list.selection()
        if not selection:
            return
        # The ready -> delivered transition runs on the worker; the app routes
        # the delivered quiz back through the controller's enter().
        self.on_enter(selection[0])

    def _quiz_by_id(self, quiz_id):
        for item in self._pending:
            if item["quiz_id"] == quiz_id:
                return item
        return None

    # -- current question -----------------------------------------------------
    def refresh(self):
        if self.closed:
            return
        controller = self.controller
        quiz = controller.current
        self._rendered_quiz_id = controller.quiz_id
        if quiz is None:
            self.question_heading.configure(text="当前题目")
            self.question_label.configure(text="当前没有待答题。")
            self.project.set("")
            for button in self.option_buttons:
                button.configure(text="")
                button.state(["disabled"])
        else:
            self.question_heading.configure(text="当前题目" + ("（历史重练）" if controller.manual else ""))
            self.question_label.configure(text=quiz.get("question") or "")
            self.project.set(quiz.get("project_root") or "当前练习未提供项目路径")
            options = quiz.get("options") or {}
            for button, letter in zip(self.option_buttons, "ABCD"):
                button.configure(text=f"{letter}. {options.get(letter, '')}")
        self._sync_controls()
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
        if has_current:
            self.footer.grid()
        else:
            self.footer.grid_remove()
        self.submit_button.configure(text=("正在切题…" if switching else "正在提交…" if submitting else
                                           ("重试提交" if state == "failed" else
                                            ("已记录作答" if finished else "提交答案"))))
        self.submit_button.state(["disabled"] if controller.busy or finished or not has_current else ["!disabled"])
        self.dismiss_button.configure(text="提交中…" if submitting else "下一题 / 关闭")
        self.dismiss_button.state(["disabled"] if controller.busy else ["!disabled"])
        for button in self.option_buttons:
            button.state(["disabled"] if controller.busy or finished or not has_current else ["!disabled"])
        selected = bool(self.pending_list.selection())
        self.enter_button.state(["!disabled"] if selected and not controller.busy else ["disabled"])

    def _submit(self):
        self.controller.submit()

    def _dismiss(self):
        self.controller.dismiss()

    def _on_controller_changed(self):
        if self._rendered_quiz_id != self.controller.quiz_id:
            self.compact_expanded = False
            if self.compact_list:
                self._layout()
        self.refresh()

    def focus_target(self):
        if self.controller.current is not None and self.controller.submit_state != "submitting":
            return self.option_buttons[0]
        return self.pending_list

    def close(self):
        self.closed = True
