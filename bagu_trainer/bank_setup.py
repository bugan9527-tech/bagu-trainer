"""Tk adapter for the in-process bank initialization service."""
import queue
import threading

from .bank_build.control import BuildCancelled, BuildError
from .bank_build.service import BankBuildService, load_sources
from .ui_theme import apply_theme, SPACE, PRIMARY, TITLE, MUTED
from .ui_state import cancel_timer, set_text, window_var


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
        self.source_list = ttk.Treeview(listing, columns=("commit", "license"), selectmode="extended", height=3 if embedded else 11)
        self.source_list.bind("<<TreeviewSelect>>", lambda _event: self._sync_controls())
        self.source_list.heading("#0", text="来源仓库")
        self.source_list.heading("commit", text="固定版本")
        self.source_list.heading("license", text="观察到的声明")
        self.source_list.column("#0", width=px(370), minwidth=px(200))
        self.source_list.column("commit", width=px(110), minwidth=px(110), stretch=False)
        self.source_list.column("license", width=px(240), minwidth=px(180))
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.source_list.yview)
        self.source_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.source_list.xview)
        horizontal.pack(side="bottom", fill="x")
        self.source_list.configure(xscrollcommand=horizontal.set)
        self.source_list.pack(fill="both", expand=True)
        for repository, source in self.sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license))
        row = ttk.Frame(panel)
        row.grid(row=3, column=0, sticky="ew", pady=px(SPACE.sm))
        ttk.Label(row, text="总超时（秒）").pack(side="left")
        self.timeout = window_var(self.root, "900")
        self.timeout_entry = ttk.Entry(row, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=px(SPACE.sm))
        self.reload_button = ttk.Button(row, text="重新加载清单", command=self.reload_sources)
        self.evidence_button = ttk.Button(row, text="查看所选来源声明", command=self._evidence)
        self.evidence_button.pack(side="right")
        self.reload_button.pack(side="right", padx=px(SPACE.sm))
        self.status = window_var(self.root, "选择来源后开始。临时网络错误最多自动重试两次。")
        MessageArea(panel, self.status, self.theme, lines=2).grid(row=4, column=0, sticky="ew", pady=px(SPACE.sm))
        self.progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=px((0, SPACE.md)))
        actions = FlowActions(panel, self.theme)
        actions.grid(row=6, column=0, sticky="ew")
        if not embedded:
            actions.add(ttk.Button(actions, text="关闭", command=self._close))
        self.cancel_button = actions.add(ttk.Button(actions, text="取消构建", command=self._cancel))
        self.start_button = actions.add(ttk.Button(actions, text="下载并构建 / 重试", style=PRIMARY, command=self._start))
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

    def _start(self):
        if self.closed or self.close_when_idle or self.busy:
            return
        repositories = list(self.source_list.selection())
        if not repositories:
            self.status.set("请先从来源清单中选择至少一个来源。")
            return
        try:
            timeout = float(self.timeout.get())
            from .bank_build.control import BuildControl
            BuildControl(timeout=timeout)  # Validate before creating the worker.
        except ValueError:
            self.status.set("总超时必须是有效正数。")
            return
        self.cancel = threading.Event()
        self.busy = True
        self.result = None
        self._set_busy(True)
        self.stage = "prepare"
        self.status.set("进行中 · 正在准备下载…本次所选来源已固定。")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        self._sync_controls()
        service, events, cancel = self.service, self.events, self.cancel

        def work():
            try:
                result = service.build(repositories, cancel=cancel, timeout=timeout,
                    progress=lambda event: events.put(("progress", event)))
                events.put(("result", result))
            except BuildError as error:
                events.put(("error", (error.stage, str(error), isinstance(error, BuildCancelled))))
            except Exception as error:
                events.put(("error", ("prepare", f"初始化失败（{type(error).__name__}），可重试。", False)))
        self.worker = threading.Thread(target=work, daemon=True, name="bagu-bank-build")
        self.worker.start()

    def _set_busy(self, busy):
        self._sync_controls()

    def _sync_controls(self):
        if self.closed or not hasattr(self, "operation_hint"):
            return
        locked = self.busy or self.close_when_idle
        selected = bool(self.source_list.selection())
        self.start_button.state(["disabled"] if locked or not selected else ["!disabled"])
        self.start_button.configure(text="下载构建中…" if self.busy else ("重试构建" if self.failed else "下载并构建 / 重试"))
        self.timeout_entry.state(["disabled"] if locked else ["!disabled"])
        self.reload_button.state(["disabled"] if locked else ["!disabled"])
        self.evidence_button.state(["!disabled"] if selected else ["disabled"])
        self.source_list.state(["disabled"] if locked else ["!disabled"])
        self.cancel_button.configure(text="正在取消…" if self.busy and self.cancel.is_set() else "取消构建")
        self.cancel_button.state(["!disabled"] if self.busy and not self.cancel.is_set() and not self.close_when_idle else ["disabled"])
        if self.busy:
            hint = "已请求取消；等待当前文件或网络读取结束。" if self.cancel.is_set() else "可以取消构建；取消将在下一检查点生效，切换完成后以最终结果为准。"
        elif self.result is not None:
            hint = "成功：新题库已就绪，无需取消；可继续下一步。"
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
        for repository, source in sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license))
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
                    if not self.cancel.is_set():
                        labels = {"prepare": "准备", "download": "下载", "retry": "重试", "unpack": "解压",
                                  "extract": "提取", "import": "导入", "tag": "标签", "concepts": "概念",
                                  "validate": "校验", "activate": "切换", "done": "完成"}
                        self.status.set(f"进行中 · {labels.get(value.stage, value.stage)}：{value.message}")
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
                        stage, message, cancelled = value
                        self.status.set(("已取消" if cancelled else "失败") + f" · {stage}：{message}" + (" 可重新构建。" if cancelled else " 点击“重试构建”。"))
                    if self.closed:
                        return
                    self._sync_controls()
                    if self.close_when_idle:
                        self._close()
                        return
        except queue.Empty:
            pass
        if not self.closed:
            self.poll = self.root.after(100, self._drain)

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
