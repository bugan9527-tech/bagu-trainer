"""Read-only pending queue and explicit entry to the application's answer window."""
from tkinter import ttk
from .ui_layout import wrapped_label
from .ui_state import window_var
from .ui_theme import SPACE, SECTION, MUTED, CAPTION


class PracticePage(ttk.Frame):
    def __init__(self, parent, theme, controller, *, on_enter=None):
        super().__init__(parent)
        self.theme, self.controller = theme, controller
        self.on_enter = on_enter or (lambda quiz_id: None)
        self.closed = False
        self._pending = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        px = theme.px
        # -- pending list column ---------------------------------------------
        self.list_column = ttk.Frame(self)
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
        self.pending_list.bind("<Double-1>", self._double_click)
        self.pending_list.bind("<Return>", lambda _event: self._enter_selected())
        self.enter_button = ttk.Button(listing, text="进入本题", command=self._enter_selected)
        self.enter_button.grid(row=1, column=0, sticky="w", pady=px((SPACE.sm, 0)))
        self.list_column.grid(row=0, column=0, sticky="nsew")

        self.refresh()

    def _double_click(self, event):
        tree = self.pending_list
        if tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row = tree.identify_row(event.y)
        if row:
            tree.selection_set(row)
            tree.focus(row)
            self._enter_selected()
        return "break"

    def _enter_selected(self):
        if self.closed or self.controller.closed:
            return
        selected = self.pending_list.selection()
        if selected:
            self.on_enter(selected[0])
        return "break"

    def set_pending(self, items, due_reviews):
        tree = self.pending_list
        self._pending = list(items)
        self._pending_ids = [item["quiz_id"] for item in items]
        selected, focus, scroll = tree.selection(), tree.focus(), tree.yview()
        for iid in tree.get_children():
            if iid not in self._pending_ids:
                tree.delete(iid)
        for index, item in enumerate(items):
            qid = item["quiz_id"]
            question = (item.get("question") or "").replace("\n", " ")
            text = question[:60] + ("…" if len(question) > 60 else "") + ("（重练）" if item.get("manual_review") else "")
            values = (item.get("knowledge_point") or "未记录知识点",)
            if not tree.exists(qid):
                tree.insert("", index, iid=qid, text=text, values=values)
            else:
                if tree.item(qid, "text") != text or tuple(tree.item(qid, "values")) != values:
                    tree.item(qid, text=text, values=values)
                if tree.index(qid) != index:
                    tree.move(qid, "", index)
        tree.selection_set([iid for iid in selected if tree.exists(iid)])
        if focus and tree.exists(focus):
            tree.focus(focus)
        if scroll:
            tree.yview_moveto(scroll[0])
        self.summary.set(f"待答题 {len(items)} · 待复习 {due_reviews}")
        self.refresh()

    def refresh(self):
        if not self.closed:
            self.enter_button.state(["!disabled"] if self.pending_list.selection() and not self.controller.busy else ["disabled"])

    _sync_controls = refresh

    def focus_target(self):
        return self.pending_list

    def close(self):
        self.closed = True
