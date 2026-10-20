"""Tk adapter for the in-process bank initialization service."""
import queue
import threading

from .bank_build.control import BuildCancelled, BuildError
from .bank_build.service import BankBuildService, load_sources


class BankSetupWindow:
    def __init__(self, parent, *, service=None, on_success=None, embedded=False):
        import tkinter as tk
        from tkinter import ttk
        self.service = service or BankBuildService()
        self.sources = load_sources(self.service.manifest)
        self.on_success = on_success
        self.busy = False
        self.closed = False
        self.close_when_idle = False
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.result = None
        self.root = ttk.Frame(parent) if embedded else tk.Toplevel(parent)
        if not embedded:
            self.root.title("选择来源 · 下载并构建题库")
            self.root.geometry("880x580")
            self.root.minsize(760, 540)
        panel = ttk.Frame(self.root, padding=0 if embedded else 18)
        panel.pack(fill="both", expand=True)
        if not embedded:
            ttk.Label(panel, text="选择首次下载的题库来源", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(panel, text="按 Ctrl 可多选。使用清单中的固定 commit 版本；请阅读所选来源的声明。\n"
                  "第三方完整题库仅下载到本机，构建全部成功后才切换，旧题库和学习记录保留。",
                  wraplength=800).pack(anchor="w", pady=10)
        self.source_list = ttk.Treeview(panel, columns=("commit", "license"), selectmode="extended", height=8 if embedded else 11)
        self.source_list.heading("#0", text="来源仓库")
        self.source_list.heading("commit", text="固定版本")
        self.source_list.heading("license", text="观察到的声明")
        self.source_list.column("#0", width=370)
        self.source_list.column("commit", width=110, stretch=False)
        self.source_list.column("license", width=240)
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.source_list.yview)
        self.source_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.source_list.pack(fill="both", expand=True)
        for repository, source in self.sources.items():
            self.source_list.insert("", "end", iid=repository, text=repository,
                                    values=(source.commit[:12], source.license))
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="总超时（秒）").pack(side="left")
        self.timeout = tk.StringVar(self.root, "900")
        self.timeout_entry = ttk.Entry(row, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=6)
        self.evidence_button = ttk.Button(row, text="查看所选来源声明", command=self._evidence)
        self.evidence_button.pack(side="right")
        self.status = tk.StringVar(self.root, "选择来源后开始。临时网络错误最多自动重试两次。")
        ttk.Label(panel, textvariable=self.status, wraplength=800, justify="left").pack(anchor="w", pady=8)
        self.progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 12))
        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="下载并构建 / 重试", command=self._start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="取消构建", command=self._cancel)
        self.cancel_button.pack(side="right", padx=10)
        self.cancel_button.state(["disabled"])
        if not embedded:
            ttk.Button(actions, text="关闭", command=self._close).pack(side="left")
            self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._drain)

    def _evidence(self):
        import webbrowser
        selected = self.source_list.selection()
        if selected:
            source = self.sources[selected[0]]
            # Links are from the bundled metadata, not the downloaded ZIP.
            if source.evidence_url.startswith("https://github.com/"):
                webbrowser.open(source.evidence_url)

    def _start(self):
        if self.busy:
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
        self.status.set("正在准备下载…")

        def work():
            try:
                result = self.service.build(repositories, cancel=self.cancel, timeout=timeout,
                    progress=lambda event: self.events.put(("progress", event)))
                self.events.put(("result", result))
            except BuildError as error:
                self.events.put(("error", (error.stage, str(error), isinstance(error, BuildCancelled))))
            except Exception as error:
                self.events.put(("error", ("prepare", f"初始化失败（{type(error).__name__}），可重试。", False)))
        threading.Thread(target=work, daemon=True, name="bagu-bank-build").start()

    def _set_busy(self, busy):
        self.start_button.state(["disabled"] if busy else ["!disabled"])
        self.timeout_entry.state(["disabled"] if busy else ["!disabled"])
        self.cancel_button.state(["!disabled"] if busy else ["disabled"])

    def _cancel(self):
        if self.busy:
            self.cancel.set()
            self.status.set("正在取消；等待当前文件或网络读取结束…")
            self.cancel_button.state(["disabled"])

    def _drain(self):
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "progress":
                    if not self.cancel.is_set():
                        labels = {"prepare": "准备", "download": "下载", "retry": "重试", "unpack": "解压",
                                  "extract": "提取", "import": "导入", "tag": "标签", "concepts": "概念",
                                  "validate": "校验", "activate": "切换", "done": "完成"}
                        self.status.set(f"{labels.get(value.stage, value.stage)}：{value.message}")
                    if value.total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=min(100, 100 * value.completed / value.total))
                    elif str(self.progress.cget("mode")) != "indeterminate":
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(15)
                elif name in {"result", "error"}:
                    self.busy = False
                    self._set_busy(False)
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=100 if name == "result" else 0)
                    if name == "result":
                        self.result = value
                        self.status.set(f"完成：{value.questions} 道题。关闭本窗口后可继续启动。旧题库和学习记录已保留。")
                        if self.on_success:
                            self.on_success(value)
                    else:
                        stage, message, cancelled = value
                        self.status.set(f"{stage}：{message}" + ("" if cancelled else " 可直接点击重试。"))
                    if self.close_when_idle:
                        self._close()
                        return
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self._drain)

    def _close(self):
        if self.busy:
            self.close_when_idle = True
            self._cancel()
            return
        self.closed = True
        self.root.destroy()
