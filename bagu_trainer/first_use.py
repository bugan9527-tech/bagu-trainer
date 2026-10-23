"""Chinese first-use wizard. Tk is confined to the main thread."""
import queue
import threading

from . import config, onboarding
from .startup import set_projects
from .ui_state import cancel_timer, set_text, clear_variable_traces, window_var
from .ui_theme import apply_theme, SPACE, PRIMARY, DANGER, TITLE, MUTED, SECTION


class FirstUseWizard:
    STEPS = ("bank", "model", "projects", "finish")
    TITLES = {"bank": "选择来源并构建题库", "model": "配置并测试模型连接",
              "projects": "选择监听项目", "finish": "准备完成，开始练习"}

    def __init__(self, coordinator, *, bank_service=None, model_opener=None,
                 root=None, start_step=None, on_return=None, content_host=None):
        from .tk_runtime import prepare_tk, create_root
        prepare_tk()
        import tkinter as tk
        from tkinter import ttk
        from .ui_layout import ScrollableFrame, MessageArea, FlowActions, wrapped_label
        self.tk, self.ttk = tk, ttk
        self.coordinator = coordinator
        self.events = coordinator.events
        self.bank_service, self.model_opener = bank_service, model_opener
        self.start_step, self.on_return = start_step or "bank", on_return
        self.embedded = root is not None
        self.bank_changed = False
        self.engine = None
        self.bank_setup = None
        self.busy = False
        self.task = None
        self.poll = None
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
        self.root = root if root is not None else create_root()
        self.theme = apply_theme(self.root)
        px = self.theme.px
        self.root.title("AI 八股训练器 · 设置" if self.embedded else "AI 八股训练器 · 首次使用向导")
        if content_host is None:
            self.theme.window(self.root, (960, 900), (700, 660))
        compact = self.theme.compact
        gap = SPACE.sm if compact else SPACE.lg
        panel = ttk.Frame(content_host if content_host is not None else self.root,
                          padding=px(SPACE.lg if compact else SPACE.xl))
        self.panel = panel
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self.heading = window_var(self.panel, "正在恢复已完成步骤…")
        wrapped_label(panel, self.theme, textvariable=self.heading, style=SECTION if compact else TITLE).grid(row=0, column=0, sticky="ew")
        self.steps_text = window_var(self.panel, "正在检查首次使用进度")
        stepper = ttk.Frame(panel)
        stepper.grid(row=1, column=0, sticky="ew", pady=px((SPACE.md, SPACE.sm)))
        self.step_labels = []
        for index, label in enumerate(("题库", "模型连接", "监听项目", "完成")):
            stepper.columnconfigure(index, weight=1, uniform="step")
            item = wrapped_label(stepper, self.theme, text=f"{index + 1}  {label}", style="Step.TLabel")
            item.grid(row=0, column=index, sticky="ew", padx=px((0, SPACE.xs)))
            self.step_labels.append(item)
        progress_row = ttk.Frame(panel)
        progress_row.grid(row=2, column=0, sticky="ew", pady=px((0, gap)))
        progress_row.columnconfigure(1, weight=1)
        ttk.Label(progress_row, textvariable=self.steps_text, style=MUTED).grid(row=0, column=0, sticky="w", padx=px((0, SPACE.lg)))
        self.step_progress = ttk.Progressbar(progress_row, mode="determinate", maximum=4)
        self.step_progress.grid(row=0, column=1, sticky="ew")
        self.content = ttk.Frame(panel)
        self.content.grid(row=3, column=0, sticky="nsew")
        self.pages = {name: ttk.Frame(self.content) for name in self.STEPS}
        self.bank_hint = window_var(self.panel)
        self.bank_view = ScrollableFrame(self.pages["bank"], self.theme, minimum_height=px(480))
        self.bank_view.pack(fill="both", expand=True)
        bank_page = self.bank_view.body
        bank_page.rowconfigure(2, weight=1)
        wrapped_label(bank_page, self.theme, textvariable=self.bank_hint).grid(row=0, column=0, sticky="ew")
        ttk.Button(bank_page, text="重新加载来源清单", command=self._ensure_bank_panel).grid(
            row=1, column=0, sticky="w", pady=px(SPACE.sm))
        self.bank_host = ttk.Frame(bank_page)
        self.bank_host.grid(row=2, column=0, sticky="nsew")
        self._make_model_page()
        self._make_projects_page()
        wrapped_label(self.pages["finish"], self.theme, text="题库、模型连接和监听项目已准备完成。\n\n"
                  "进入主窗口后会先建立监听基线，再根据新的代码改动出题。\n"
                  "原有题库版本和学习记录继续保留。").pack(fill="x", pady=px(SPACE.xl))
        self.status = window_var(self.panel, "正在检查本地进度，不会自动发送模型请求。")
        self.status_area = MessageArea(panel, self.status, self.theme, lines=1 if compact else 2)
        self.status_area.grid(row=4, column=0, sticky="ew", pady=px(SPACE.sm))
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        auxiliary = FlowActions(panel, self.theme)
        auxiliary.grid(row=6, column=0, sticky="ew")
        if not self.embedded:
            auxiliary.add(ttk.Button(auxiliary, text="退出，稍后继续", command=self._close))
        self.migration_button = ttk.Button(auxiliary, text="迁移旧数据…", command=self._migrate)
        if not self.embedded:
            auxiliary.add(self.migration_button)
        if self.embedded or compact:
            stepper.grid_remove()
            progress_row.grid_remove()
        self.cancel_button = auxiliary.add(ttk.Button(auxiliary, text="取消测试", command=self._cancel_test))
        footer = ttk.Frame(panel)
        footer.grid(row=7, column=0, sticky="ew", pady=px((SPACE.xs, 0)))
        footer.columnconfigure(1, weight=1)
        self.back_button = ttk.Button(footer, text="上一步", command=self._back)
        self.back_button.grid(row=0, column=0, sticky="w")
        self.start_button = ttk.Button(footer, text="重试恢复", style=PRIMARY, command=self._next)
        self.start_button.grid(row=0, column=2, sticky="e")
        self.operation_hint = window_var(self.panel, "正在恢复步骤；恢复完成前不能继续。")
        wrapped_label(footer, self.theme, textvariable=self.operation_hint, style=MUTED).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=px((SPACE.sm, 0)))
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.panel.bind("<Destroy>", self._destroyed, add="+")
        self.poll = self.root.after(80, self._drain)
        self._restore()

    def _make_model_page(self):
        from .model_panel import ModelPanel
        from .ui_layout import wrapped_label
        px = self.theme.px
        self.model_view = ModelPanel(self.pages["model"], self.theme, interactive=False,
                                     opener=self.model_opener)
        self.model_view.pack(fill="both", expand=True)
        # Reuse the panel's form variables/entries; the wizard keeps its own
        # test/save flow and step gating on top.
        self.base_url = self.model_view.base_url
        self.model_name = self.model_view.model_name
        self.api_key = self.model_view.api_key
        self.timeout = self.model_view.timeout
        self.key_hint = self.model_view.key_hint
        self.model_entries = self.model_view.entries
        self.variable_traces = self.model_view.variable_traces
        # The wizard's own test button (the panel's form is interactive=False).
        # Place it inside the scrollable form body so keyboard Tab order stays
        # field-to-field without jumping outside the form.
        self.test_button = self.ttk.Button(self.model_view.form_view.body, text="测试连接", command=self._test_model)
        self.test_button.grid(row=101, column=0, columnspan=2, sticky="w", pady=px(SPACE.sm))
        # Track edits so the wizard's own verified/signature state stays valid
        # (the panel tracks its own revision separately in form-only mode).
        self.model_change_traces = [
            (variable, variable.trace_add("write", self._model_changed))
            for variable in (self.base_url, self.model_name, self.api_key, self.timeout)]
        self.variable_traces.extend(self.model_change_traces)

    def _make_projects_page(self):
        from .ui_layout import FlowActions, wrapped_label
        px = self.theme.px
        page = self.pages["projects"]
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        wrapped_label(page, self.theme, text="选择希望监听代码改动的项目文件夹，可添加多个。失效目录可先移除，再选择新位置。",
                      style=MUTED).grid(row=0, column=0, sticky="ew", pady=px(SPACE.sm))
        listing = self.ttk.Frame(page)
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        listing.grid(row=1, column=0, sticky="nsew")
        self.project_list = self.tk.Listbox(listing, height=5, width=1, exportselection=False)
        self.theme.listbox(self.project_list)
        self.project_list.bind("<<ListboxSelect>>", lambda _event: self._sync_controls())
        self.project_list.grid(row=0, column=0, sticky="nsew")
        vertical = self.ttk.Scrollbar(listing, command=self.project_list.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = self.ttk.Scrollbar(listing, orient="horizontal", command=self.project_list.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.project_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        actions = FlowActions(page, self.theme)
        actions.grid(row=2, column=0, sticky="ew", pady=px(SPACE.sm))
        self.project_buttons = []
        for label, callback in (("添加项目文件夹", self._add_project), ("移除选中目录", self._remove_project)):
            button = self.ttk.Button(actions, text=label, command=callback,
                                     style=DANGER if callback == self._remove_project else PRIMARY)
            actions.add(button)
            self.project_buttons.append(button)

    def run(self):
        self.root.mainloop()
        return self.engine

    def _signature(self):
        return (self.base_url.get(), self.model_name.get(), self.api_key.get(), self.timeout.get())

    def _model_changed(self, *_):
        if self.closed or self.restoring:
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
        if self.closed:
            return
        locked = self.busy or self._build_busy() or self.closing
        if locked:
            self.progress.grid()
        else:
            self.progress.grid_remove()
        next_ok = not locked
        if self.step == "bank":
            next_ok = next_ok and self.bank_ready
        elif self.step == "model":
            next_ok = next_ok and (self.verified is not None or self.saved_signature == self._signature())
        elif self.step == "projects":
            next_ok = next_ok and bool(self.projects)
        self.start_button.state(["!disabled"] if next_ok else ["disabled"])
        self.back_button.state(["disabled"] if locked or (not self.embedded and self.step in {"loading", "bank"}) else ["!disabled"])
        if self.embedded:
            self.back_button.configure(text="返回监听首页")
        self.cancel_button.configure(text="取消迁移" if self.busy and self.task == "migration" else "取消测试")
        self.cancel_button.state(["!disabled"] if self.busy and self.task in {"test", "migration"} and not self.cancel.is_set() and not self.closing else ["disabled"])
        self.test_button.state(["disabled"] if locked else ["!disabled"])
        self.migration_button.state(["disabled"] if locked else ["!disabled"])
        for widget in self.model_entries + self.project_buttons:
            widget.state(["disabled"] if locked else ["!disabled"])
        self.project_list.configure(state="disabled" if locked else "normal")
        if not locked and not self.project_list.curselection():
            self.project_buttons[1].state(["disabled"])
        if self.closing:
            hint = "正在安全结束任务；保存操作需完成写入后关闭。"
        elif self._build_busy():
            hint = "进行中：下载构建题库；可在题库区域取消，结束前不能继续。"
        elif self.busy:
            if self.task in {"test", "migration"}:
                hint = "已请求取消；请等待当前步骤或网络读取结束。" if self.cancel.is_set() else "进行中：可以取消；取消将在下一检查点或网络读取结束后生效。"
            else:
                hint = "进行中：正在恢复、保存或加载；当前不可取消，重复操作已阻止。"
        elif self.step == "loading":
            hint = "尚未恢复完成：可点击“重试恢复”，修复提示中的问题后重试。"
        elif self.step == "bank" and not self.bank_ready:
            hint = ("请完成题库构建，也可返回监听首页。" if self.embedded else
                    "下一步不可用：请先完成题库构建。返回不可用：这是第一步。")
        elif self.step == "model" and not next_ok:
            hint = "保存不可用：请先测试当前模型配置；测试成功且配置未变更后才可保存。"
        elif self.step == "projects" and not self.projects:
            hint = "空状态：尚未选择监听项目；请添加项目文件夹后继续。"
        elif self.step == "finish":
            hint = "成功：所有步骤就绪，可以进入主窗口。"
        else:
            hint = "当前步骤可以继续。尚无进行中的任务。"
        set_text(self.operation_hint, hint)

    def _show(self, step):
        for page in self.pages.values():
            page.pack_forget()
        self.step = step
        self.pages[step].pack(fill="both", expand=True)
        self.heading.set(self.TITLES[step])
        index = self.STEPS.index(step)
        if self.theme.compact and not self.embedded:
            self.heading.set(f"{index + 1}/4 · {self.TITLES[step]}")
        self.steps_text.set(f"第 {index + 1} / 4 步")
        self.step_progress.configure(value=index + 1)
        for position, (label, title) in enumerate(zip(self.step_labels, ("题库", "模型连接", "监听项目", "完成"))):
            label.configure(text=f"{position + 1}  {title}" + (" · 当前" if position == index else ""),
                            style="Current.Step.TLabel" if position == index else "Step.TLabel")
        self.start_button.configure(text={"bank": "使用就绪题库，下一步", "model": "保存并继续",
                                          "projects": "保存项目并继续", "finish": "进入主窗口"}[step])
        if self.embedded:
            self.start_button.configure(text="保存并返回监听" if step == "model" else "返回监听首页")
        if step == "bank":
            self._ensure_bank_panel()
        self._sync_controls()

    def _ensure_bank_panel(self):
        if self.closed or self.closing:
            return
        if self.bank_setup is not None:
            self.bank_setup.reload_sources()
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
        self.bank_changed = True
        if not self.closing:
            self._restore()

    def _restore(self):
        coordinator, embedded = self.coordinator, self.embedded
        def restore():
            if not embedded:
                onboarding.begin()
            report = coordinator.checker()
            return report, onboarding.snapshot(report)
        self._job("restore", restore, "正在恢复已完成步骤…")

    def _migrate(self):
        from tkinter import filedialog
        if self.closed or self.closing or self.busy or self._build_busy():
            return
        source = filedialog.askdirectory(parent=self.root, title="选择旧版 bagu-trainer 项目目录（包含 config.json 或 data）")
        if not source:
            return
        self.cancel = threading.Event()
        cancel, events, coordinator = self.cancel, self.events, self.coordinator
        def migrate():
            from .migration import migrate_legacy
            migrate_legacy(source, cancel=cancel,
                           progress=lambda text: events.put(("migration_progress", text)))
            report = coordinator.checker()
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
        self._show(self.start_step if self.embedded else value.step)
        self.status.set(report.error or {"bank": "完成题库构建后，向导会自动进入下一步。",
            "model": "请主动测试连接，通过后保存。", "projects": "模型已测试并保存，请选择监听项目。",
            "finish": "所有步骤已完成，点击进入主窗口。"}[value.step])
        if self.embedded:
            self.status.set("题库已更新，返回后立即使用新题库。" if self.bank_changed else
                            "完成修改后返回监听首页；原有题库和学习记录会保留。")

    def _test_model(self):
        if self.closed or self.closing or self.busy or self._build_busy():
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
        cancel, opener = self.cancel, self.model_opener
        def test():
            settings = onboarding.effective_settings(*signature[:3], timeout)
            return signature, onboarding.test_connection(settings, cancel=cancel, opener=opener)
        self._job("test", test, "正在后台测试连接，窗口可继续响应；可点击取消测试。")

    def _next(self):
        if self.closed or self.closing or self.busy or self._build_busy():
            return
        if self.step == "loading":
            self._restore()
        elif self.step == "bank":
            if self.bank_ready:
                if self.embedded:
                    self._close()
                else:
                    self._show("model")
                    self.status.set("请测试模型连接并保存，已验证且未修改的配置可直接继续。")
        elif self.step == "model":
            if self.saved_signature == self._signature():
                if self.embedded:
                    self._close()
                else:
                    self._show("projects")
                    self.status.set("选择监听目录后保存并继续。")
            elif self.verified is not None and self.test_signature == self._signature():
                verified = self.verified
                coordinator = self.coordinator
                def save():
                    onboarding.save_verified_model(verified)
                    report = coordinator.checker()
                    return report, onboarding.snapshot(report)
                self._job("save_model", save, "正在保存 Key 到凭据管理器并保存配置…")
            else:
                self.status.set("请先测试当前填写的模型连接，成功后再保存。")
        elif self.step == "projects":
            self._save_projects()
        elif self.step == "finish":
            self._retry()

    def _back(self):
        if self.embedded:
            self._close()
            return
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
        coordinator = self.coordinator
        def save():
            set_projects(paths)
            report = coordinator.checker()
            return report, onboarding.snapshot(report)
        self._job("projects", save, "正在验证并保存监听目录…")

    def _retry(self):
        coordinator = self.coordinator
        def launch():
            engine = coordinator.prepare()
            report = coordinator.report
            return engine, report, onboarding.snapshot(report)
        self._job("launch", launch, "正在重新检查准备情况并加载学习引擎…")

    def _job(self, tag, action, status):
        if self.closed or self.busy or self._build_busy() or self.closing:
            return
        self.busy = True
        self.task = tag
        self.status.set(status)
        self.progress.start(15)
        self._sync_controls()
        events = self.events
        def work():
            try:
                events.put(("wizard_result", (tag, action())))
            except onboarding.SetupError as error:
                events.put(("wizard_error", (error.code, str(error))))
            except Exception as error:
                from .migration import MigrationError
                message = str(error) if isinstance(error, MigrationError) else f"操作失败（{type(error).__name__}），请检查配置文件、目录权限或依赖后重试。"
                events.put(("wizard_error", ("operation", message)))
        self.worker = threading.Thread(target=work, daemon=True, name="bagu-wizard-" + tag)
        self.worker.start()

    def _cancel_test(self):
        if not self.closed and self.busy and self.task in {"test", "migration"} and not self.cancel.is_set():
            self.cancel.set()
            self.status.set("正在取消；当前步骤或网络读取结束后即可重试。")
            self._sync_controls()

    def _drain(self):
        if self.closed:
            return
        cancel_timer(self.root, self.poll)
        self.poll = None
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
                        self.status.set(value[1] + " 请修正后重试当前步骤。")
                    else:
                        tag, result = value
                        if tag == "test":
                            signature, verified = result
                            if signature == self._signature() and not self.cancel.is_set():
                                self.verified = verified
                                self.test_signature = signature
                                self.test_button.configure(text="重新测试")
                                self.status.set("成功 · 连接测试成功。请点击“保存并继续”，将 Key 保存到 Windows 凭据管理器。")
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
                            if self.embedded and tag == "save_model":
                                self._close()
                                return
                    self._sync_controls()
        except queue.Empty:
            pass
        if not self.closed:
            if self.closing and not self.busy and not self._build_busy():
                self._close()
                return
            self._sync_controls()
            self.coordinator.window_state = self.root.state()
            self.poll = self.root.after(80, self._drain)

    def _destroyed(self, event):
        if event.widget is self.panel:
            self.closed = True
            clear_variable_traces(self.variable_traces)
            self.cancel.set()
            self.verified = self.test_signature = self.saved_signature = None
            cancel_timer(self.root, self.poll)
            self.poll = None

    def _close(self):
        if self.closed:
            return
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
        self.verified = None
        self.test_signature = None
        self.saved_signature = None
        self.api_key.set("")
        if self.embedded:
            self.panel.destroy()
            if self.on_return:
                self.on_return(self.bank_changed)
        else:
            self.root.destroy()
