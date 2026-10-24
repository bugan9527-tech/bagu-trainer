"""Answer history presentation. Workers receive plain values and queue only."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var
from .ui_theme import SPACE, SECTION, MUTED, PRIMARY
from .ui_copy import ReadOnlyText, copy_text, question_text, solution_text


def _query(events, page_id, token, kind, function, arguments):
    try:
        result = function(**arguments)
    except Exception as error:
        result = {"ok": False, "note": f"查询失败（{type(error).__name__}），请重试。"}
    events.put(("history", (page_id, token, kind, result)))


class HistoryPage:
    def invalidate_content(self):
        """Invalidate late queries and discard all cached snapshot text on Tk."""
        self.token += 1
        self.dirty = True
        self._copy_snapshot = None
        self.tree.delete(*self.tree.get_children())
        if self.detail_frame is not None:
            self.detail_frame.destroy()
            self.detail_frame = None
        self.detail_id = None
        self.message.set("题库内容已变化，请刷新记录。")

    def __init__(self, app, content_host=None):
        self.app, self.root, self.theme = app, app.root, app.theme
        self.page_id, self.token = uuid.uuid4().hex, 0
        self.closed = self.retry_busy = False
        self.visible = True
        self.saved_focus = None
        self.wide = False
        self.dirty = self.restore_list = False
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bagu-history")
        self.future = None
        self.futures = []
        self.page = self.total = 0
        self.project_paths = [None]
        self.detail_id = None
        self._copy_snapshot = None
        self.selection, self.scroll = (), 0
        self.frame = ttk.Frame(content_host if content_host is not None else self.root,
                               padding=self.theme.px(SPACE.lg))
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        wrapped_label(self.frame, self.theme, text="回顾当次作答与题目快照", style=MUTED).grid(row=0, column=0, sticky="ew")
        self.message = window_var(self.root, "正在读取答题记录…")
        wrapped_label(self.frame, self.theme, textvariable=self.message, style=MUTED).grid(row=1, column=0, sticky="ew", pady=8)
        self.workspace = ttk.Frame(self.frame)
        self.workspace.grid(row=2, column=0, sticky="nsew")
        self.workspace.rowconfigure(0, weight=1)
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.bind("<Configure>", self._resize, add="+")
        self.list_frame = ttk.Frame(self.workspace)
        self.list_frame.grid(row=0, column=0, sticky="nsew")
        self.list_frame.columnconfigure(0, weight=1)
        self.list_frame.rowconfigure(1, weight=1)
        filters = ttk.Frame(self.list_frame)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="答题结果").grid(row=0, column=0, sticky="w")
        self.result = ttk.Combobox(filters, values=("全部", "答错", "答对"), state="readonly", width=10)
        self.result.current(0)
        self.result.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(filters, text="项目").grid(row=1, column=0, sticky="w", pady=8)
        self.project = ttk.Combobox(filters, values=("全部项目",), state="readonly", width=12)
        self.project.current(0)
        self.project.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(filters, text="关键词").grid(row=2, column=0, sticky="w")
        self.keyword = window_var(self.root)
        self.search = ttk.Entry(filters, textvariable=self.keyword)
        self.search.grid(row=2, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(filters, text="搜索 / 刷新", command=self.refresh).grid(row=3, column=1, sticky="e", pady=(8, 0))
        self.result.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.project.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.search.bind("<Return>", lambda _: self.refresh())
        table = ttk.Frame(self.list_frame)
        table.grid(row=1, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("time", "kp", "project", "result"), show="headings", selectmode="browse", height=8)
        for key, title, width in (("time", "作答时间（本地）", 160), ("kp", "知识点", 170),
                                   ("project", "项目名称", 120), ("result", "答题结果", 80)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=self.theme.px(width), minwidth=self.theme.px(70), stretch=key == "kp")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<Return>", lambda _: self.open_selected())
        self.tree.bind("<Double-1>", lambda _: self.open_selected())
        nav = FlowActions(self.list_frame, self.theme)
        nav.grid(row=2, column=0, sticky="ew", pady=8)
        self.previous = nav.add(ttk.Button(nav, text="上一页", command=lambda: self.turn(-1)))
        self.next = nav.add(ttk.Button(nav, text="下一页", command=lambda: self.turn(1)))
        self.open_button = nav.add(ttk.Button(nav, text="查看详情 (Enter)", style=PRIMARY, command=self.open_selected))
        self.detail_frame = None
        footer = FlowActions(self.frame, self.theme)
        footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.back_button = footer.add(ttk.Button(footer, text="返回记录列表", command=self.back))
        self.home_button = footer.add(ttk.Button(footer, text="返回工作台", command=app._return_from_history))
        self.back_button.state(["disabled"])
        self.refresh()
        self.search.focus_set()

    def query(self, kind, function, **arguments):
        self.token += 1
        if self.future:
            self.future.cancel()
        self.future = self.executor.submit(_query, self.app.events, self.page_id, self.token, kind, function, arguments)
        self.futures = [future for future in self.futures if not future.done()]
        self.futures.append(self.future)

    def refresh(self):
        if self.closed or self.retry_busy:
            return
        self.page = 0
        self.load_list()

    def load_list(self, restore=False):
        self.restore_list = restore
        if restore:
            self.selection, self.scroll = self.tree.selection(), self.tree.yview()[0]
        self.dirty = False
        if self.detail_frame:
            self.detail_frame.destroy()
            self.detail_frame = None
        self.detail_id = None
        self.back_button.state(["disabled"])
        self._layout()
        self.message.set("正在查询…监听继续运行，阅读时暂缓自动弹题。")
        self.open_button.state(["disabled"])
        self.previous.state(["disabled"])
        self.next.state(["disabled"])
        selected = max(0, self.project.current())
        if getattr(getattr(self.app, "tasks", None), "bank", None) and self.app.tasks.bank.cleaning:
            self.message.set("正在维护学习数据，完成后可刷新。")
            return
        self.query("list", getattr(self.app, "history_service", self.app.monitor.engine).get_answer_history, page=self.page,
                   result=("all", "wrong", "correct")[max(0, self.result.current())],
                   project=self.project_paths[selected], keyword=self.keyword.get())

    def turn(self, delta):
        if 0 <= self.page + delta <= max(0, (self.total - 1) // 20):
            self.page += delta
            self.load_list()

    def open_selected(self):
        if self.open_button.instate(["disabled"]) or not self.tree.selection():
            return
        self.selection, self.scroll = self.tree.selection(), self.tree.yview()[0]
        self.detail_id = int(self.selection[0])
        self._new_detail()
        self._layout()
        self.back_button.state(["!disabled"])
        self.message.set("正在读取完整题目快照…")
        if getattr(getattr(self.app, "tasks", None), "bank", None) and self.app.tasks.bank.cleaning:
            return
        self.query("detail", getattr(self.app, "history_service", self.app.monitor.engine).get_answer_detail, attempt_id=self.detail_id)

    def receive(self, page_id, token, kind, data):
        if self.closed or page_id != self.page_id or token != self.token:
            return
        if not data.get("ok"):
            self.message.set(data.get("note") or "查询失败，请重试。")
            if kind == "detail":
                self.render_detail_error()
            return
        if kind == "list":
            current = self.project_paths[max(0, self.project.current())]
            self.project_paths = [None, *data["projects"]]
            self.project.configure(values=["全部项目", *[p or "未记录项目" for p in data["projects"]]])
            self.project.current(self.project_paths.index(current) if current in self.project_paths else 0)
            self.tree.delete(*self.tree.get_children())
            for item in data["items"]:
                self.tree.insert("", "end", iid=str(item["id"]), values=(item["time_text"], item["knowledge_point"] or "旧记录未提供知识点", item["project_name"], item["result_text"]))
            self.total, self.page = data["total"], data["page"]
            if self.total:
                self.message.set(f"共 {self.total} 条 · 第 {self.page + 1} / {(self.total + 19) // 20} 页 · 每页 20 条。监听持续运行，自动弹题暂缓。")
                self.open_button.state(["!disabled"])
                first = self.tree.get_children()[0]
                self.tree.selection_set(first)
                self.tree.focus(first)
                if self.restore_list and self.selection and self.tree.exists(self.selection[0]):
                    self.tree.selection_set(self.selection)
                    self.tree.focus(self.selection[0])
                    self.tree.yview_moveto(self.scroll)
            else:
                filtered = self.keyword.get().strip() or self.result.current() > 0 or self.project.current() > 0
                self.message.set("没有匹配的答题记录，请调整筛选或关键词。" if filtered else "暂无答题记录。完成一次答题后，即可在这里回顾。")
            self.previous.state(["!disabled"] if self.page > 0 else ["disabled"])
            self.next.state(["!disabled"] if (self.page + 1) * 20 < self.total else ["disabled"])
        else:
            self.render_detail(data)

    def render_detail_error(self):
        self._new_detail()
        ttk.Button(self.detail_frame, text="重试查询", command=lambda: self.query(
            "detail", self.app.monitor.engine.get_answer_detail, attempt_id=self.detail_id)).pack(anchor="w")

    def _new_detail(self):
        self._copy_snapshot = None
        if self.detail_frame:
            self.detail_frame.destroy()
        self.detail_frame = ttk.Frame(self.workspace)
        self._layout()

    def render_detail(self, data):
        self._new_detail()
        self.detail_frame.rowconfigure(0, weight=1)
        self.detail_frame.columnconfigure(0, weight=1)
        self.detail_view = ScrollableFrame(self.detail_frame, self.theme)
        self.detail_view.grid(row=0, column=0, sticky="nsew")
        body, snapshot = self.detail_view.body, data["snapshot"]
        self.detail_texts = {}
        missing = "旧记录未提供"
        def show(title, text):
            wrapped_label(body, self.theme, text=title, style=SECTION).pack(fill="x", pady=(12, 4))
            content = ReadOnlyText(body, self.theme, text=str(text) if text not in (None, "") else missing)
            content.pack(fill="x")
            self.detail_texts[title] = content
        self.message.set(f"{data['time_text']} · {'答对' if data['correct'] else '答错'} · 记录 #{data['id']}（历史快照）")
        show("知识点与项目", f"{snapshot.get('kp_name') or data.get('kp_id') or missing}\n{data.get('project_root') or missing}")
        show("题干", snapshot.get("question"))
        options = snapshot.get("options")
        options = options if isinstance(options, dict) else {}
        self._copy_snapshot = {"id": data["id"], "question": snapshot.get("question"), "options": dict(options),
                               "answer": snapshot.get("answer"), "analysis": snapshot.get("analysis")}
        for letter in "ABCD":
            show(f"选项 {letter}", options.get(letter))
        show("当次选择", data.get("answer"))
        show("正确答案", snapshot.get("answer"))
        show("解析", snapshot.get("analysis"))
        # Only already persisted source metadata; no source files or bank lookup.
        import json
        sources = {k: v for k, v in snapshot.items() if "source" in k and v}
        if data.get("source_summary"):
            sources["source_summary"] = data["source_summary"]
        show("已有来源信息", json.dumps(sources, ensure_ascii=False, indent=2) if sources else missing)
        self.base_retry_reason = data.get("retry_reason")
        reason = self.base_retry_reason
        self.retry_reason = window_var(self.detail_frame, reason or "使用该快照重新练习，不调用 AI。提交后才显示答案与解析。")
        wrapped_label(self.detail_frame, self.theme, textvariable=self.retry_reason, style=MUTED).grid(row=1, column=0, sticky="ew", pady=8)
        actions = FlowActions(self.detail_frame, self.theme)
        actions.grid(row=2, column=0, sticky="ew")
        self.retry_button = actions.add(ttk.Button(actions, text="重新练习本题", style=PRIMARY, command=self.retry))
        self.copy_question_button = actions.add(ttk.Button(actions, text="复制题目（含选项）", command=self._copy_question))
        self.copy_solution_button = actions.add(ttk.Button(actions, text="复制答案与解析", command=self._copy_solution))
        if not (snapshot.get("answer") or snapshot.get("analysis")):
            self.copy_solution_button.state(["disabled"])
        if reason:
            self.retry_button.state(["disabled"])
        if self.visible:
            self.detail_view.canvas.focus_set()

    def _copy_detail(self):
        snapshot = self._copy_snapshot
        return snapshot if not self.closed and self.visible and snapshot and snapshot["id"] == self.detail_id else None

    def _copy_question(self):
        snapshot = self._copy_detail()
        if snapshot:
            copy_text(self.frame, question_text(snapshot["question"], snapshot["options"]))

    def _copy_solution(self):
        snapshot = self._copy_detail()
        if snapshot and (snapshot["answer"] or snapshot["analysis"]):
            copy_text(self.frame, solution_text(snapshot["answer"], snapshot["analysis"]))

    def retry(self):
        if self.closed or self.retry_busy or self.retry_button.instate(["disabled"]):
            return
        self.retry_busy = True
        self.retry_button.state(["disabled"])
        self.home_button.state(["disabled"])
        self.back_button.state(["disabled"])
        self.retry_reason.set("正在准备快照重练…重复操作已阻止。")
        self.app.pending_actions.add("repractice")
        self.app.jobs.put(("repractice", (self.page_id, self.detail_id)))

    def retry_finished(self, result):
        self.retry_busy = False
        self.home_button.state(["!disabled"])
        self.back_button.state(["!disabled"])
        self.retry_button.state(["disabled"] if result.get("ok") else ["!disabled"])
        self.retry_reason.set("重练已打开，提交后查看结果。" if result.get("ok") else result.get("note") or "重练失败，请重试。")

    def back(self):
        if self.retry_busy:
            return
        self.token += 1
        self.detail_id = None
        if self.detail_frame:
            self.detail_frame.destroy()
            self.detail_frame = None
        self._layout()
        self.back_button.state(["disabled"])
        self.message.set(f"共 {self.total} 条 · 第 {self.page + 1} 页。监听持续运行，自动弹题暂缓。")
        if self.selection and self.tree.exists(self.selection[0]):
            self.tree.selection_set(self.selection)
            self.tree.focus(self.selection[0])
            self.tree.yview_moveto(self.scroll)
        self.tree.focus_set()
        if self.dirty:
            self.load_list(restore=True)

    def _resize(self, event):
        if event.widget is self.workspace:
            wide = event.width >= self.theme.px(900)
            if wide != self.wide:
                self.wide = wide
                self._layout()

    def _layout(self):
        split = self.wide and self.detail_id is not None
        self.workspace.columnconfigure(0, weight=1, uniform="history" if split else "")
        self.workspace.columnconfigure(1, weight=1 if split else 0, uniform="history" if split else "")
        if self.detail_id is None or self.wide:
            self.list_frame.grid()
        else:
            self.list_frame.grid_remove()
        if self.detail_frame is not None:
            self.detail_frame.grid(row=0, column=1 if split else 0, sticky="nsew",
                                   padx=(self.theme.px(SPACE.lg) if split else 0, 0))

    def enter(self):
        self.visible = True
        if self.dirty and self.detail_id is None:
            self.load_list(restore=True)
        if (self.detail_id is not None and hasattr(self, "retry_button")
                and self.retry_button.winfo_exists() and not self.retry_busy):
            reason = self.base_retry_reason
            self.retry_button.state(["disabled"] if reason else ["!disabled"])
            self.retry_reason.set(reason or "使用该快照重新练习，不调用 AI。提交后才显示答案与解析。")

    def leave(self):
        focus = self.root.focus_get() or self.root.focus_lastfor()
        self.saved_focus = focus if focus and str(focus).startswith(str(self.frame) + ".") else None
        self.visible = False
        self.frame.pack_forget()

    def focus_target(self):
        if self.saved_focus is not None and self.saved_focus.winfo_exists():
            return self.saved_focus
        if (self.detail_id is not None and hasattr(self, "detail_view")
                and self.detail_view.winfo_exists() and self.detail_frame):
            return self.detail_view.canvas
        return self.search

    def close(self):
        self.closed = True
        self.token += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.frame.destroy()
