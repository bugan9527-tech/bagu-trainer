"""Reusable bank-management panel backed by the application task layer.

The panel shows the source list, current-bank summary and build progress.  The
actual build task (worker, cancel Event, result) is owned by the host-provided
``TaskController``, so hiding this page does not destroy a running build; a
late result after a page switch still lands in the controller and is reflected
when the page returns.  The three states — building / built / applied — come
from the controller, so the panel never fabricates its own progress.
"""
import threading

from tkinter import ttk

from . import config
from .bank_build.service import BankBuildService, load_sources
from .bank_storage import active_bank_dir
from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var, set_text
from .ui_theme import SPACE, PRIMARY, SECTION, MUTED


class BankPanel(ttk.Frame):
    def __init__(self, parent, theme, *, tasks, service=None, on_built=None):
        super().__init__(parent)
        self.theme = theme
        px = theme.px
        self.tasks = tasks
        self.on_built = on_built
        self.service = service or BankBuildService()
        self.sources = load_sources(self.service.manifest)
        self.closed = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        wrapped_label(self, theme, text="当前题库", style=SECTION).grid(row=0, column=0, sticky="ew")
        self.summary = window_var(self, "")
        wrapped_label(self, theme, textvariable=self.summary, style=MUTED).grid(row=0, column=0, sticky="e")
        self.summary.set(self._current_summary())

        self.view = ScrollableFrame(self, theme, minimum_height=px(360))
        self.view.grid(row=1, column=0, sticky="nsew")
        body = self.view.body
        body.columnconfigure(0, weight=1)
        wrapped_label(body, theme, text="选择来源（可多选）。使用清单中的固定 commit 版本；构建全部成功后才切换，旧题库和学习记录保留。",
                      style=MUTED).grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.md)))

        listing = ttk.Frame(body)
        listing.grid(row=1, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.source_list = ttk.Treeview(listing, columns=("commit", "license"), selectmode="extended", height=7)
        self.source_list.bind("<<TreeviewSelect>>", lambda _event: self._sync_controls())
        self.source_list.heading("#0", text="来源仓库")
        self.source_list.heading("commit", text="固定版本")
        self.source_list.heading("license", text="观察到的声明")
        self.source_list.column("#0", width=px(300), minwidth=px(180))
        self.source_list.column("commit", width=px(110), minwidth=px(110), stretch=False)
        self.source_list.column("license", width=px(200), minwidth=px(140))
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.source_list.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        self.source_list.configure(yscrollcommand=vertical.set)
        self.source_list.grid(row=0, column=0, sticky="nsew")
        for repository, source in self.sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license))

        row = ttk.Frame(body)
        row.grid(row=2, column=0, sticky="ew", pady=px(SPACE.sm))
        ttk.Label(row, text="总超时（秒）").pack(side="left")
        self.timeout = window_var(self, "900")
        self.timeout_entry = ttk.Entry(row, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=px(SPACE.sm))
        self.reload_button = ttk.Button(row, text="重新加载清单", command=self._reload)
        self.reload_button.pack(side="right", padx=px(SPACE.sm))

        self.status = window_var(self, "选择来源后开始。临时网络错误最多自动重试两次。")
        wrapped_label(body, theme, textvariable=self.status, style=MUTED).grid(row=3, column=0, sticky="ew", pady=px(SPACE.sm))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.grid(row=4, column=0, sticky="ew", pady=px((0, SPACE.md)))
        self.operation_hint = window_var(self, "尚未开始：请选择来源。当前没有可取消的任务。")
        wrapped_label(body, theme, textvariable=self.operation_hint, style=MUTED).grid(row=5, column=0, sticky="ew", pady=px((SPACE.sm, 0)))

        actions = FlowActions(self, theme)
        actions.grid(row=2, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.cancel_button = actions.add(ttk.Button(actions, text="取消构建", command=self._cancel))
        self.start_button = actions.add(ttk.Button(actions, text="下载并构建 / 重试", style=PRIMARY, command=self._start))
        self.cancel_button.state(["disabled"])

        self._sync_controls()
        self.poll = self.after(100, self._drain)

    def _current_summary(self):
        try:
            path = active_bank_dir() / "questions.json"
            import json
            bank = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(bank, list):
                return f"当前题库：{len(bank)} 个知识点"
        except (OSError, ValueError):
            pass
        return "当前题库：暂无（尚未构建）"

    def _sync_controls(self):
        if self.closed:
            return
        task = self.tasks.bank
        building = task.busy
        selected = bool(self.source_list.selection())
        self.start_button.state(["disabled"] if building or not selected else ["!disabled"])
        self.start_button.configure(text="下载构建中…" if building else ("重新构建" if task.state in {"failed", "cancelled"} else "下载并构建"))
        self.timeout_entry.state(["disabled"] if building else ["!disabled"])
        self.reload_button.state(["disabled"] if building else ["!disabled"])
        self.source_list.state(["disabled"] if building else ["!disabled"])
        self.cancel_button.configure(text="正在取消…" if building and task.cancel.is_set() else "取消构建")
        self.cancel_button.state(["!disabled"] if building and not task.cancel.is_set() else ["disabled"])
        if building:
            hint = "已请求取消；等待当前文件或网络读取结束。" if task.cancel.is_set() else "可以取消构建；取消将在下一检查点生效。"
        elif task.state == "built":
            hint = "构建完成：新题库已就绪，将在生成/提交安全边界应用。"
        elif task.state == "applied":
            hint = "已应用：新题库已生效，旧题库与学习记录保留。"
        elif task.state == "failed":
            hint = "失败：" + task.error + " 点击“重新构建”。"
        elif task.state == "cancelled":
            hint = "已取消：" + task.error + " 可重新构建。"
        elif not selected:
            hint = "开始不可用：请先选择至少一个来源。"
        else:
            hint = "可以开始构建。当前没有可取消的任务。"
        set_text(self.operation_hint, hint)

    def _start(self):
        if self.closed or self.tasks.bank.busy:
            return
        repositories = list(self.source_list.selection())
        if not repositories:
            self.status.set("请先从来源清单中选择至少一个来源。")
            return
        try:
            timeout = float(self.timeout.get())
            from .bank_build.control import BuildControl
            BuildControl(timeout=timeout)
        except ValueError:
            self.status.set("总超时必须是有效正数。")
            return
        self.status.set("进行中 · 正在准备下载…本次所选来源已固定。")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        self.tasks.start_build(self.service, repositories, timeout=timeout)
        self._sync_controls()

    def _cancel(self):
        if self.tasks.cancel_build():
            self.status.set("正在取消；等待当前文件或网络读取结束，任务结束前不能重复构建。")
            self._sync_controls()

    def _reload(self):
        if self.closed or self.tasks.bank.busy:
            return
        try:
            sources = load_sources(self.service.manifest)
        except Exception as error:
            self.status.set(f"读取清单失败（{type(error).__name__}），请修复来源清单后重新加载。")
            return
        selected = set(self.source_list.selection())
        self.source_list.delete(*self.source_list.get_children())
        self.sources = sources
        for repository, source in sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license))
        self.source_list.selection_set(list(selected.intersection(sources)))
        self._sync_controls()

    def refresh(self):
        """Reflect controller state on (re)entry; safe after a page switch."""
        self.summary.set(self._current_summary())
        task = self.tasks.bank
        if task.busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
            self.status.set(f"进行中 · {task.stage or '准备'}：{task.message}")
        elif task.state == "built":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            self.status.set(f"构建完成：{task.result.questions} 道题，将在安全边界应用。")
        elif task.state == "applied":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100)
            self.status.set("新题库已应用，旧题库与学习记录保留。")
        elif task.state == "failed":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status.set("失败 · " + task.error + " 点击“重新构建”。")
        elif task.state == "cancelled":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status.set("已取消 · " + task.error + " 可重新构建。")
        self._sync_controls()

    def _drain(self):
        if self.closed:
            return
        from .ui_state import cancel_timer
        cancel_timer(self, self.poll)
        self.poll = None
        changed = self.tasks.drain_bank()
        task = self.tasks.bank
        if changed:
            self.progress.stop()
            if task.state == "built":
                self.progress.configure(mode="determinate", value=100)
                self.status.set(f"构建完成：{task.result.questions} 道题，将在安全边界应用。")
                if self.on_built is not None:
                    self.on_built(task.result)
            elif task.state == "failed":
                self.progress.configure(mode="determinate", value=0)
                self.status.set("失败 · " + task.error + " 点击“重新构建”。")
            elif task.state == "cancelled":
                self.progress.configure(mode="determinate", value=0)
                self.status.set("已取消 · " + task.error + " 可重新构建。")
        elif task.busy:
            self.status.set(f"进行中 · {task.stage or '准备'}：{task.message}")
            if task.progress.get("total"):
                self.progress.stop()
                self.progress.configure(mode="determinate", value=min(100, 100 * task.progress["completed"] / task.progress["total"]))
            elif str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        self._sync_controls()
        if not self.closed:
            self.poll = self.after(100, self._drain)

    def _destroyed(self, event=None):
        # Internal helper so the host can also tear down explicitly.
        pass
