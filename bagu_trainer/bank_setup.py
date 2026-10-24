"""Tk adapter for the in-process bank initialization service."""
import queue
import threading
import time

from .bank_build.control import BuildCancelled, BuildError
from .bank_build.service import BankBuildService, load_sources
from .ui_theme import apply_theme, SPACE, PRIMARY, TITLE, MUTED
from .ui_state import cancel_timer, set_text, window_var


STAGES = {"prepare": "准备", "cache": "缓存校验", "download": "下载", "retry": "重试", "unpack": "解压校验",
          "extract": "提取", "import": "导入", "tag": "标签", "concepts": "概念", "validate": "校验",
          "activate": "激活", "done": "完成"}
STATES = {"waiting": "等待", "downloading": "下载中", "completed": "完成", "failed": "失败", "cancelled": "取消"}


class BankSetupWindow:
    def __init__(self, parent, *, service=None, on_success=None, embedded=False):
        import tkinter as tk
        from tkinter import ttk
        from .ui_layout import wrapped_label, MessageArea, FlowActions
        self.theme = apply_theme(parent)
        px = self.theme.px
        self.service = service or BankBuildService()
        self.sources = load_sources(self.service.manifest)
        self.on_success = on_success
        self.busy = False
        self.closed = False
        self.close_when_idle = False
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.result = None
        self.stage = "idle"
        self.failed = False
        self.poll = None
        self.source_statuses = {}
        self.last_batch = None
        self.budget_text = window_var(parent, "总时限自动按所选来源数 × 900 秒设置；包含下载、重试和构建。")
        self.root = ttk.Frame(parent) if embedded else tk.Toplevel(parent)
        if not embedded:
            self.root.title("选择来源 · 下载并构建题库")
            self.theme.window(self.root, (920, 640), (820, 580))
        panel = ttk.Frame(self.root, padding=0 if embedded else px(SPACE.xl))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        if not embedded:
            ttk.Label(panel, text="选择首次下载的题库来源", style=TITLE).grid(row=0, column=0, sticky="w")
        wrapped_label(panel, self.theme, text="按 Ctrl 可多选。使用清单中的固定 commit 版本；请阅读所选来源的声明。\n"
                  "第三方完整题库仅下载到本机，构建全部成功后才切换，旧题库和学习记录保留。",
                  style=MUTED).grid(row=1, column=0, sticky="ew", pady=px(SPACE.md))
        listing = ttk.Frame(panel)
        listing.grid(row=2, column=0, sticky="nsew")
        self.source_list = ttk.Treeview(listing, columns=("commit", "license", "state", "stage", "retries", "reason"), selectmode="extended", height=3 if embedded else 11)
        self.source_list.bind("<<TreeviewSelect>>", lambda _event: self._sync_controls())
        self.source_list.heading("#0", text="来源仓库")
        self.source_list.heading("commit", text="固定版本")
        self.source_list.heading("license", text="观察到的声明")
        self.source_list.column("commit", width=px(110), minwidth=px(110), stretch=False)
        for name, title, width in (("state", "状态", 80), ("stage", "阶段", 100), ("retries", "重试次数", 80),
                                   ("reason", "原因 / 处理建议", 420)):
            self.source_list.heading(name, text=title)
            self.source_list.column(name, width=px(width), minwidth=px(width), stretch=False)
        self.source_list.column("#0", width=px(230), minwidth=px(180))
        self.source_list.column("license", width=px(140), minwidth=px(120))
        self.source_list.configure(displaycolumns=("state", "stage", "retries", "commit", "license", "reason"))
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.source_list.yview)
        self.source_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.source_list.xview)
        horizontal.pack(side="bottom", fill="x")
        self.source_list.configure(xscrollcommand=horizontal.set)
        self.source_list.pack(fill="both", expand=True)
        for repository, source in self.sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license, "等待", "准备", 0, ""))
        row = ttk.Frame(panel)
        row.grid(row=3, column=0, sticky="ew", pady=px(SPACE.sm))
        ttk.Label(row, text="任务总时限（秒）").pack(side="left")
        self.timeout = window_var(self.root, "自动")
        self.timeout_entry = ttk.Entry(row, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=px(SPACE.sm))
        ttk.Label(row, text="单次网络超时（秒）").pack(side="left")
        self.request_timeout = window_var(self.root, "5")
        self.request_timeout_entry = ttk.Entry(row, textvariable=self.request_timeout, width=6)
        self.request_timeout_entry.pack(side="left", padx=px(SPACE.sm))
        self.reload_button = ttk.Button(row, text="重新加载清单", command=self.reload_sources)
        self.evidence_button = ttk.Button(row, text="查看所选来源声明", command=self._evidence)
        self.evidence_button.pack(side="right")
        self.reload_button.pack(side="right", padx=px(SPACE.sm))
        self.force_download = tk.BooleanVar(master=self.root, value=False)
        self.force_download_check = ttk.Checkbutton(panel, text="强制重新下载（忽略有效缓存并请求网络）",
                                                    variable=self.force_download)
        self.force_download_check.grid(row=8, column=0, sticky="w", pady=px(SPACE.sm))
        wrapped_label(panel, self.theme, textvariable=self.budget_text, style=MUTED).grid(row=9, column=0, sticky="ew")
        self.status = window_var(self.root, "构建优先复用已校验缓存；来源缺失或损坏时下载。")
        MessageArea(panel, self.status, self.theme, lines=2).grid(row=4, column=0, sticky="ew", pady=px(SPACE.sm))
        self.progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=px((0, SPACE.md)))
        actions = FlowActions(panel, self.theme)
        actions.grid(row=6, column=0, sticky="ew")
        if not embedded:
            actions.add(ttk.Button(actions, text="关闭", command=self._close))
        self.cancel_button = actions.add(ttk.Button(actions, text="取消构建", command=self._cancel))
        self.start_button = actions.add(ttk.Button(actions, text="构建 / 重新构建", style=PRIMARY, command=self._start))
        self.retry_failed_button = actions.add(ttk.Button(actions, text="仅重试失败来源", command=lambda: self._start(retry_only=True)))
        self.details_button = actions.add(ttk.Button(actions, text="查看失败详情", command=self._show_failures))
        self.cancel_button.state(["disabled"])
        self.operation_hint = window_var(self.root, "尚未开始：请选择来源。当前没有可取消的任务。")
        wrapped_label(panel, self.theme, textvariable=self.operation_hint, style=MUTED).grid(
            row=7, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        if not embedded:
            self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self._sync_controls()
        self.poll = self.root.after(100, self._drain)

    def _evidence(self):
        import webbrowser
        selected = self.source_list.selection()
        if selected:
            source = self.sources[selected[0]]
            # Links are from the bundled metadata, not the downloaded ZIP.
            if source.evidence_url.startswith("https://github.com/"):
                webbrowser.open(source.evidence_url)

    def _show_failures(self):
        import tkinter as tk
        from tkinter import ttk
        window = tk.Toplevel(self.root)
        window.title("来源准备结果 · 错误详情")
        self.theme.window(window, (820, 480), (620, 320))
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        for source in self.source_statuses.values():
            if source.state in {"failed", "cancelled"}:
                text.insert("end", f"{source.repository}\ncommit: {source.commit}\n"
                            f"{STATES[source.state]} · {STAGES.get(source.stage, source.stage)} · 重试 {max(0, source.attempt - 1)} 次\n"
                            f"{source.reason}\n\n")
        text.configure(state="disabled")
        text.focus_set()

    def _start(self, *, retry_only=False):
        if self.closed or self.close_when_idle or self.busy:
            return
        repositories = list(self.source_list.selection())
        retry_failed = None
        versions = None
        force_download = self.force_download.get()
        if retry_only:
            if not self.last_batch:
                return
            repositories, versions, force_download = self.last_batch
            retry_failed = [s.repository for s in self.source_statuses.values() if s.state == "failed"]
            if not retry_failed:
                return
        if not repositories:
            self.status.set("请先从来源清单中选择至少一个来源。")
            return
        try:
            timeout = 900 * len(repositories) if self.timeout.get().strip() in {"", "自动"} else float(self.timeout.get())
            request_timeout = float(self.request_timeout.get())
            from .bank_build.control import BuildControl
            BuildControl(timeout=timeout)  # Validate before creating the worker.
            BuildControl(timeout=request_timeout)
        except ValueError:
            self.status.set("任务总时限须为“自动”或有效正数；单次网络超时须为有效正数。")
            return
        versions = versions or {repo: self.sources[repo].commit for repo in repositories}
        self.last_batch = (list(repositories), dict(versions), force_download)
        self.source_statuses = {}
        self.deadline = time.monotonic() + timeout
        self.task_timeout = timeout
        self.task_request_timeout = request_timeout
        self._update_budget()
        self.cancel = threading.Event()
        self.busy = True
        self.result = None
        self._set_busy(True)
        self.stage = "prepare"
        self.status.set("进行中 · 正在准备来源…本次所选来源已固定。")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        self._sync_controls()
        service, events, cancel = self.service, self.events, self.cancel

        def work():
            try:
                result = service.build(repositories, versions=versions, retry_failed=retry_failed,
                    cancel=cancel, timeout=timeout, request_timeout=request_timeout, force_download=force_download,
                    progress=lambda event: events.put(("progress", event)))
                events.put(("result", result))
            except BuildError as error:
                events.put(("error", error))
            except Exception as error:
                events.put(("error", BuildError("prepare", f"初始化失败：{error}。请检查来源清单及数据目录权限后重试。")))
        self.worker = threading.Thread(target=work, daemon=True, name="bagu-bank-build")
        self.worker.start()

    def _set_busy(self, busy):
        self._sync_controls()

    def _sync_controls(self):
        if self.closed or not hasattr(self, "operation_hint"):
            return
        locked = self.busy or self.close_when_idle
        selected = bool(self.source_list.selection())
        has_failures = any(s.state == "failed" for s in self.source_statuses.values())
        unfinished = any(s.state == "cancelled" for s in self.source_statuses.values())
        self.retry_failed_button.state(["!disabled"] if has_failures and not unfinished and not locked else ["disabled"])
        self.details_button.state(["!disabled"] if any(s.state in {"failed", "cancelled"} for s in self.source_statuses.values()) else ["disabled"])
        self.start_button.state(["disabled"] if locked or not selected else ["!disabled"])
        self.start_button.configure(text="构建中…" if self.busy else ("重试构建" if self.failed else "构建 / 重新构建"))
        self.force_download_check.state(["disabled"] if locked else ["!disabled"])
        self.timeout_entry.state(["disabled"] if locked else ["!disabled"])
        self.request_timeout_entry.state(["disabled"] if locked else ["!disabled"])
        self.reload_button.state(["disabled"] if locked else ["!disabled"])
        self.evidence_button.state(["!disabled"] if selected else ["disabled"])
        self.source_list.state(["disabled"] if locked else ["!disabled"])
        self.cancel_button.configure(text="正在取消…" if self.busy and self.cancel.is_set() else "取消构建")
        self.cancel_button.state(["!disabled"] if self.busy and not self.cancel.is_set() and not self.close_when_idle else ["disabled"])
        if self.busy:
            hint = "已请求取消；等待当前文件或网络读取结束。" if self.cancel.is_set() else "可以取消构建；取消将在下一检查点生效，切换完成后以最终结果为准。"
        elif self.result is not None:
            hint = "成功：新题库已就绪，无需取消；可继续下一步。"
        elif unfinished:
            hint = "任务提前停止：请重新构建以处理未完成项；已完成来源优先复用缓存。"
        elif has_failures:
            hint = "来源准备失败：可查看失败详情或仅重试失败来源，全部就绪后才激活。"
        elif not self.sources:
            hint = "空状态：来源清单为空，请修复清单后重新加载。"
        elif not selected:
            hint = "开始不可用：请先选择至少一个来源。当前没有可取消的任务。"
        else:
            hint = "可以开始或重试构建。当前没有可取消的任务。"
        set_text(self.operation_hint, hint)

    def reload_sources(self):
        if self.closed or self.busy or self.close_when_idle:
            return
        try:
            sources = load_sources(self.service.manifest)
        except Exception as error:
            self.status.set(f"读取清单失败（{type(error).__name__}），请修复来源清单后重新加载。")
            return
        selected = set(self.source_list.selection())
        self.source_list.delete(*self.source_list.get_children())
        self.sources = sources
        self.last_batch = None
        self.source_statuses = {}
        for repository, source in sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license, "等待", "准备", 0, ""))
        self.source_list.selection_set(list(selected.intersection(sources)))
        self._sync_controls()

    def _cancel(self):
        if not self.closed and self.busy and not self.cancel.is_set():
            self.cancel.set()
            self.status.set("正在取消；等待当前文件或网络读取结束，任务结束前不能重复构建。")
            self._sync_controls()

    def _drain(self):
        if self.closed:
            return
        cancel_timer(self.root, self.poll)
        self.poll = None
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "progress":
                    self.stage = value.stage
                    if value.source is not None:
                        self._render_source(value.source)
                    if not self.cancel.is_set():
                        prefix = (f"第 {value.source_index}/{value.source_total} 个 · {value.source.repository} · "
                                  f"重试 {max(0, value.attempt - 1)} 次 · " if value.source else
                                  f"共 {value.source_total} 个来源 · " if value.source_total else "")
                        self.status.set(f"进行中 · {prefix}{STAGES.get(value.stage, value.stage)}：{value.message}")
                    if value.total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=min(100, 100 * value.completed / value.total))
                    elif str(self.progress.cget("mode")) != "indeterminate":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(15)
                elif name in {"result", "error"}:
                    self.busy = False
                    self.failed = name == "error"
                    self._set_busy(False)
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=100 if name == "result" else 0)
                    if name == "result":
                        self.result = value
                        self.status.set(f"成功 · 完成：{value.questions} 道题。题库已切换，旧题库和学习记录已保留。")
                        if self.on_success and not self.close_when_idle:
                            self.on_success(value)
                    else:
                        for source in getattr(value, "sources", ()):
                            self._render_source(source)
                        self.status.set(("已取消" if isinstance(value, BuildCancelled) else "失败") + f" · {value.stage}：{value}")
                    if self.closed:
                        return
                    self._sync_controls()
                    if self.close_when_idle:
                        self._close()
                        return
        except queue.Empty:
            pass
        if not self.closed:
            if self.busy:
                self._update_budget()
            self.poll = self.root.after(100, self._drain)

    def _render_source(self, source):
        self.source_statuses[source.repository] = source
        if self.source_list.exists(source.repository):
            self.source_list.item(source.repository, values=(source.commit[:12], self.sources[source.repository].license,
                STATES[source.state], STAGES.get(source.stage, source.stage), max(0, source.attempt - 1), source.reason))

    def _update_budget(self):
        remaining = max(0, self.deadline - time.monotonic())
        warning = " · 总时限即将耗尽；到期将停止整个任务，完成缓存保留" if remaining < 30 else ""
        self.budget_text.set(f"单次网络等待上限 {self.task_request_timeout:g} 秒；整个任务总时限 {self.task_timeout:g} 秒，"
                             f"剩余 {remaining:.0f} 秒（包含重试和构建）{warning}")

    def _destroyed(self, event):
        if event.widget is self.root:
            self.closed = True
            self.cancel.set()
            cancel_timer(self.root, self.poll)
            self.poll = None

    def _close(self):
        if self.closed:
            return
        if self.busy:
            self.close_when_idle = True
            self._cancel()
            return
        self.root.destroy()
