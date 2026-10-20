"""Chinese first-use wizard. Tk is confined to the main thread."""
import queue
import threading

from . import config, onboarding
from .startup import set_projects


class FirstUseWizard:
    STEPS = ("bank", "model", "projects", "finish")
    TITLES = {"bank": "选择来源并构建题库", "model": "配置并测试模型连接",
              "projects": "选择监听项目", "finish": "准备完成，开始练习"}

    def __init__(self, coordinator, *, bank_service=None, model_opener=None):
        from .tk_runtime import prepare_tk
        prepare_tk()
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.coordinator = coordinator
        self.events = coordinator.events
        self.bank_service, self.model_opener = bank_service, model_opener
        self.engine = None
        self.bank_setup = None
        self.busy = False
        self.closed = False
        self.closing = False
        self.cancel = threading.Event()
        self.verified = None
        self.test_signature = None
        self.saved_signature = None
        self.restoring = False
        self.bank_ready = False
        self.projects = []
        self.step = "loading"
        self.root = tk.Tk()
        self.root.title("AI 八股训练器 · 首次使用向导")
        self.root.geometry("900x740")
        self.root.minsize(820, 680)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        panel = ttk.Frame(self.root, padding=22)
        panel.pack(fill="both", expand=True)
        self.heading = tk.StringVar(self.root, "正在恢复已完成步骤…")
        ttk.Label(panel, textvariable=self.heading, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        self.steps_text = tk.StringVar(self.root, "1 题库  →  2 模型连接  →  3 监听项目  →  4 完成")
        ttk.Label(panel, textvariable=self.steps_text).pack(anchor="w", pady=(10, 12))
        self.content = ttk.Frame(panel)
        self.content.pack(fill="both", expand=True)
        self.pages = {name: ttk.Frame(self.content) for name in self.STEPS}
        self.bank_hint = tk.StringVar(self.root)
        ttk.Label(self.pages["bank"], textvariable=self.bank_hint, wraplength=820).pack(anchor="w")
        ttk.Button(self.pages["bank"], text="重新加载来源清单", command=self._ensure_bank_panel).pack(anchor="w", pady=4)
        self.bank_host = ttk.Frame(self.pages["bank"])
        self.bank_host.pack(fill="both", expand=True)
        self._make_model_page()
        self._make_projects_page()
        ttk.Label(self.pages["finish"], text="题库、模型连接和监听项目已准备完成。\n\n"
                  "进入主窗口后会先建立监听基线，再根据新的代码改动出题。\n"
                  "原有题库版本和学习记录继续保留。", wraplength=800).pack(anchor="w", pady=20)
        self.status = tk.StringVar(self.root, "正在检查本地进度，不会自动发送模型请求。")
        ttk.Label(panel, textvariable=self.status, wraplength=820, justify="left").pack(anchor="w", fill="x", pady=10)
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))
        footer = ttk.Frame(panel)
        footer.pack(fill="x")
        ttk.Button(footer, text="退出，稍后继续", command=self._close).pack(side="left")
        self.migration_button = ttk.Button(footer, text="迁移旧数据…", command=self._migrate)
        self.migration_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(footer, text="取消测试", command=self._cancel_test)
        self.cancel_button.pack(side="left", padx=8)
        self.back_button = ttk.Button(footer, text="上一步", command=self._back)
        self.back_button.pack(side="right", padx=8)
        self.start_button = ttk.Button(footer, text="重试恢复", command=self._next)
        self.start_button.pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._drain)
        self._restore()

    def _make_model_page(self):
        tk, ttk = self.tk, self.ttk
        page = self.pages["model"]
        ttk.Label(page, text="填写服务商提供的基础地址和模型名称。点击“测试连接”会发送一次小请求，\n"
                  "可能产生服务商费用；测试通过后点击“保存并继续”。", wraplength=800).pack(anchor="w", pady=12)
        form = ttk.Frame(page)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.base_url = tk.StringVar(self.root)
        self.model_name = tk.StringVar(self.root)
        self.api_key = tk.StringVar(self.root)
        self.timeout = tk.StringVar(self.root, "15")
        self.model_entries = []
        for row, (label, variable) in enumerate((("模型基础地址", self.base_url), ("模型名称", self.model_name),
                                                ("API Key", self.api_key), ("连接超时（秒）", self.timeout))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=10)
            entry = ttk.Entry(form, textvariable=variable, show="*" if variable is self.api_key else "")
            entry.grid(row=row, column=1, sticky="ew", pady=10)
            self.model_entries.append(entry)
            variable.trace_add("write", self._model_changed)
        self.key_hint = tk.StringVar(self.root, "Key 仅保存到 Windows 凭据管理器，不写入日志或明文配置。")
        ttk.Label(page, textvariable=self.key_hint, wraplength=800).pack(anchor="w", pady=12)
        self.test_button = ttk.Button(page, text="测试连接", command=self._test_model)
        self.test_button.pack(anchor="w", pady=8)

    def _make_projects_page(self):
        page = self.pages["projects"]
        self.ttk.Label(page, text="选择希望监听代码改动的项目文件夹，可添加多个。\n"
                       "失效目录可先移除，再选择新位置。", wraplength=800).pack(anchor="w", pady=16)
        self.project_list = self.tk.Listbox(page, height=10, exportselection=False)
        self.project_list.pack(fill="both", expand=True, pady=10)
        actions = self.ttk.Frame(page)
        actions.pack(fill="x", pady=10)
        self.project_buttons = []
        for label, callback in (("添加项目文件夹", self._add_project), ("移除选中目录", self._remove_project)):
            button = self.ttk.Button(actions, text=label, command=callback)
            button.pack(side="left", padx=(0, 10))
            self.project_buttons.append(button)

    def run(self):
        self.root.mainloop()
        return self.engine

    def _signature(self):
        return (self.base_url.get(), self.model_name.get(), self.api_key.get(), self.timeout.get())

    def _model_changed(self, *_):
        if self.restoring:
            return
        self.verified = None
        self.test_signature = None
        if hasattr(self, "start_button"):
            self._sync_controls()
            if self.step == "model" and not self.busy:
                self.status.set("配置已修改，请重新测试连接后保存。")

    def _build_busy(self):
        return self.bank_setup is not None and self.bank_setup.busy

    def _sync_controls(self):
        locked = self.busy or self._build_busy() or self.closing
        next_ok = not locked
        if self.step == "bank":
            next_ok = next_ok and self.bank_ready
        elif self.step == "model":
            next_ok = next_ok and (self.verified is not None or self.saved_signature == self._signature())
        self.start_button.state(["!disabled"] if next_ok else ["disabled"])
        self.back_button.state(["disabled"] if locked or self.step in {"loading", "bank"} else ["!disabled"])
        self.cancel_button.state(["!disabled"] if self.busy and self.task == "test" else ["disabled"])
        self.test_button.state(["disabled"] if locked else ["!disabled"])
        self.migration_button.state(["disabled"] if locked else ["!disabled"])
        for widget in self.model_entries + self.project_buttons:
            widget.state(["disabled"] if locked else ["!disabled"])
        self.project_list.configure(state="disabled" if locked else "normal")

    def _show(self, step):
        for page in self.pages.values():
            page.pack_forget()
        self.step = step
        self.pages[step].pack(fill="both", expand=True)
        self.heading.set(self.TITLES[step])
        self.steps_text.set("  →  ".join(("● " if name == step else "") + f"{i} {label}"
            for i, (name, label) in enumerate(zip(self.STEPS, ("题库", "模型连接", "监听项目", "完成")), 1)))
        self.start_button.configure(text={"bank": "使用就绪题库，下一步", "model": "保存并继续",
                                          "projects": "保存项目并继续", "finish": "进入主窗口"}[step])
        if step == "bank":
            self._ensure_bank_panel()
        self._sync_controls()

    def _ensure_bank_panel(self):
        if self.bank_setup is not None:
            return
        try:
            from .bank_setup import BankSetupWindow
            self.bank_setup = BankSetupWindow(self.bank_host, service=self.bank_service,
                                              on_success=self._bank_ready, embedded=True)
            self.bank_setup.root.pack(fill="both", expand=True)
        except Exception:
            self.bank_hint.set("无法读取来源清单，请修复 interview-banks/sources.json 或文件权限，再点击重新加载。")

    def _bank_ready(self, _result):
        self.bank_ready = True
        if not self.closing:
            self._restore()

    def _restore(self):
        def restore():
            onboarding.begin()
            report = self.coordinator.checker()
            return report, onboarding.snapshot(report)
        self._job("restore", restore, "正在恢复已完成步骤…")

    def _migrate(self):
        from tkinter import filedialog
        if self.busy or self._build_busy():
            return
        source = filedialog.askdirectory(parent=self.root, title="选择旧版 bagu-trainer 项目目录（包含 config.json 或 data）")
        if not source:
            return
        self.cancel = threading.Event()
        def migrate():
            from .migration import migrate_legacy
            migrate_legacy(source, cancel=self.cancel,
                           progress=lambda text: self.events.put(("migration_progress", text)))
            report = self.coordinator.checker()
            return report, onboarding.snapshot(report)
        self._job("migration", migrate, "正在复制并校验旧数据；原目录会保留…")

    def _apply_snapshot(self, report, value):
        self.coordinator.report = report
        self.bank_ready = value.bank_ready
        self.bank_hint.set("已有可用题库，可直接下一步；也可重新选择来源构建。" if value.bank_ready else "请选择来源并完成题库构建。")
        self.restoring = True
        self.base_url.set(value.base_url)
        self.model_name.set(value.model)
        self.timeout.set(str(value.timeout_sec))
        self.api_key.set("")
        self.restoring = False
        self.verified = None
        self.test_signature = None
        self.saved_signature = self._signature() if value.model_ready else None
        self.key_hint.set("已检测到现有 Key；留空可沿用，保存时会写入 Windows 凭据管理器。" if value.has_key else
                          "Key 仅保存到 Windows 凭据管理器，不写入日志或明文配置。")
        self.projects = list(value.projects)
        self._render_projects()
        self._show(value.step)
        self.status.set(report.error or {"bank": "完成题库构建后，向导会自动进入下一步。",
            "model": "请主动测试连接，通过后保存。", "projects": "模型已测试并保存，请选择监听项目。",
            "finish": "所有步骤已完成，点击进入主窗口。"}[value.step])

    def _test_model(self):
        if self.busy or self._build_busy():
            return
        signature = self._signature()
        self.verified = None
        self.test_signature = None
        try:
            timeout = float(signature[3])
        except ValueError:
            self.status.set("连接超时请填写有效秒数，再重新测试。")
            return
        self.cancel = threading.Event()
        def test():
            settings = onboarding.effective_settings(*signature[:3], timeout)
            return signature, onboarding.test_connection(settings, cancel=self.cancel, opener=self.model_opener)
        self._job("test", test, "正在后台测试连接，窗口可继续响应；可点击取消测试。")

    def _next(self):
        if self.busy or self._build_busy():
            return
        if self.step == "loading":
            self._restore()
        elif self.step == "bank":
            if self.bank_ready:
                self._show("model")
                self.status.set("请测试模型连接并保存，已验证且未修改的配置可直接继续。")
        elif self.step == "model":
            if self.saved_signature == self._signature():
                self._show("projects")
                self.status.set("选择监听目录后保存并继续。")
            elif self.verified is not None and self.test_signature == self._signature():
                verified = self.verified
                def save():
                    onboarding.save_verified_model(verified)
                    report = self.coordinator.checker()
                    return report, onboarding.snapshot(report)
                self._job("save_model", save, "正在保存 Key 到凭据管理器并保存配置…")
            else:
                self.status.set("请先测试当前填写的模型连接，成功后再保存。")
        elif self.step == "projects":
            self._save_projects()
        elif self.step == "finish":
            self._retry()

    def _back(self):
        if not self.busy and not self._build_busy() and self.step in self.STEPS[1:]:
            self._show(self.STEPS[self.STEPS.index(self.step) - 1])
            self.status.set("已完成的配置会保留，可修改后重新验证。")

    def _add_project(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self.root, title="选择要监听的代码项目")
        if path and path not in self.projects:
            self.projects.append(path)
            self._render_projects()

    def _remove_project(self):
        for index in reversed(self.project_list.curselection()):
            del self.projects[index]
        self._render_projects()

    def _render_projects(self):
        self.project_list.configure(state="normal")
        self.project_list.delete(0, "end")
        for project in self.projects:
            self.project_list.insert("end", project)

    def _save_projects(self):
        paths = list(self.projects)
        def save():
            set_projects(paths)
            report = self.coordinator.checker()
            return report, onboarding.snapshot(report)
        self._job("projects", save, "正在验证并保存监听目录…")

    def _retry(self):
        def launch():
            engine = self.coordinator.prepare()
            report = self.coordinator.report
            return engine, report, onboarding.snapshot(report)
        self._job("launch", launch, "正在重新检查准备情况并加载学习引擎…")

    def _job(self, tag, action, status):
        if self.busy or self._build_busy() or self.closing:
            return
        self.busy = True
        self.task = tag
        self.status.set(status)
        self.progress.start(15)
        self._sync_controls()
        def work():
            try:
                self.events.put(("wizard_result", (tag, action())))
            except onboarding.SetupError as error:
                self.events.put(("wizard_error", (error.code, str(error))))
            except Exception as error:
                from .migration import MigrationError
                message = str(error) if isinstance(error, MigrationError) else f"操作失败（{type(error).__name__}），请检查配置文件、目录权限或依赖后重试。"
                self.events.put(("wizard_error", ("operation", message)))
        threading.Thread(target=work, daemon=True, name="bagu-wizard-" + tag).start()

    def _cancel_test(self):
        if self.busy and self.task == "test":
            self.cancel.set()
            self.status.set("正在取消测试；当前网络读取结束或超时后即可重试。")

    def _drain(self):
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "show":
                    self.root.deiconify()
                    self.root.lift()
                elif name == "migration_progress":
                    self.status.set(value)
                elif name in {"wizard_result", "wizard_error"}:
                    self.busy = False
                    self.progress.stop()
                    if self.closing:
                        self._close()
                        return
                    if name == "wizard_error":
                        self.status.set(value[1])
                    else:
                        tag, result = value
                        if tag == "test":
                            signature, verified = result
                            if signature == self._signature() and not self.cancel.is_set():
                                self.verified = verified
                                self.test_signature = signature
                                self.status.set("连接测试成功。请点击“保存并继续”，将 Key 保存到 Windows 凭据管理器。")
                            else:
                                self.status.set("测试已取消或输入已变化，请重新测试当前配置。")
                        elif tag == "launch":
                            engine, report, restored = result
                            if engine is not None:
                                self.engine = engine
                                self._close()
                                return
                            self._apply_snapshot(report, restored)
                        else:
                            self._apply_snapshot(*result)
                    self._sync_controls()
        except queue.Empty:
            pass
        if not self.closed:
            if self.closing and not self.busy and not self._build_busy():
                self._close()
                return
            self._sync_controls()
            self.coordinator.window_state = self.root.state()
            self.root.after(80, self._drain)

    def _close(self):
        if self.busy or self._build_busy():
            self.closing = True
            self._cancel_test()
            if self.busy and self.task == "migration":
                self.cancel.set()
            if self._build_busy():
                self.bank_setup._cancel()
            self.status.set("正在结束当前操作；已完成的步骤会保留，稍后可继续。")
            self._sync_controls()
            return
        self.closed = True
        self.verified = None
        self.test_signature = None
        self.saved_signature = None
        self.api_key.set("")
        self.root.destroy()
