"""Reusable model configuration panel (embedded Frame).

A single verified-form implementation powers the first-use wizard, the desktop
"模型配置" page and the standalone --configure entry.  The panel owns only its
form, test/save workers and their revision gating; the host owns page mounting
and application-level lifecycle (navigation, exit).  Hiding the panel (page
switch) must not cancel a running test or discard the draft: the panel is
created once and pack_forget'd, never destroyed, on ordinary page changes.

Rules preserved from the legacy settings window:
- A test result is bound to the exact parameters (plus a revision counter) it
  was tested with; editing any field invalidates it.
- Saving always requires a current successful test; the effective (resolved)
  Key is compared again inside the worker before persisting.
"""
import queue
import threading

from tkinter import ttk

from . import onboarding
from .ui_layout import ScrollableFrame, MessageArea, FlowActions, wrapped_label
from .ui_state import cancel_timer, set_text, clear_variable_traces, window_var
from .ui_theme import SPACE, PRIMARY, SECTION, MUTED

PRESET_MODELS = ["deepseek-chat", "gpt-4o-mini", "deepseek-reasoner", "qwen-plus", "moonshot-v1-8k"]
PRESET_URLS = {
    "DeepSeek": "https://api.deepseek.com/v1",
    "OpenAI": "https://api.openai.com/v1",
    "Moonshot": "https://api.moonshot.cn/v1",
    "通义千问": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


class ModelPanel(ttk.Frame):
    """Embedded model form; callbacks report verified/saved state to the host."""

    def __init__(self, parent, theme, *, events=None, on_saved=None, opener=None,
                 compact=False, interactive=True):
        super().__init__(parent)
        self.theme = theme
        compact = compact or theme.compact
        px = theme.px
        self.events = events if events is not None else queue.Queue()
        self.on_saved = on_saved
        self.opener = opener
        self.interactive = interactive
        self.closed = self.busy = self.closing = False
        self.task = None
        self.cancel = threading.Event()
        self.verified = None
        self.test_signature = None
        self.saved_signature = None
        self.revision = 0
        self.test_failed = False
        self.restoring = False
        self.poll = None
        self.worker = None
        self.variable_traces = []

        from . import config
        cfg = config.load_config()
        llm = cfg.get("llm")
        llm = llm if isinstance(llm, dict) else {}
        self.base_url = window_var(self, llm.get("base_url") or "https://api.deepseek.com/v1")
        self.model_name = window_var(self, llm.get("model") or "deepseek-chat")
        self.api_key = window_var(self, "")
        self.timeout = window_var(self, str(llm.get("timeout_sec", 60)))
        self.status = window_var(self, "请先测试连接，成功后保存。已有 Key 可留空沿用。")
        self.save_state = window_var(self, "等待测试连接")
        self.result_heading = window_var(self, "连接验证与保存 · 等待测试")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        intro_text = "填写服务商提供的基础地址和模型名称。点击“测试连接”会发送一次小请求，\n可能产生服务商费用；测试通过后点击“保存”。"
        self.intro = wrapped_label(self, theme, text=intro_text, style=MUTED)
        self.intro.grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm if compact else SPACE.lg)))

        self.form_view = ScrollableFrame(self, theme)
        self.form_view.grid(row=1, column=0, sticky="nsew")
        frm = self.form_view.body
        frm.columnconfigure(0, weight=0)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="快捷选择").grid(row=0, column=0, sticky="nw", padx=px((0, SPACE.lg)), pady=px(SPACE.sm))
        self.provider_var = window_var(self)
        self.provider = ttk.Combobox(frm, textvariable=self.provider_var, state="readonly", width=16,
                                     values=list(PRESET_URLS))
        self.provider.grid(row=0, column=1, sticky="ew", pady=px(SPACE.sm))
        self.provider.bind("<<ComboboxSelected>>", self._provider_changed)
        self.entries = []
        fields = (("接口地址", self.base_url, "使用服务商的完整基础地址，通常包含 /v1。"),
                  ("模型名称", self.model_name, "填写服务商支持的模型标识，也可从列表选择。"),
                  ("API Key", self.api_key, "留空可沿用已有 Key；仅保存到 Windows 凭据管理器。"),
                  ("超时（秒）", self.timeout, "等待模型响应的最长时间；连接较慢时可适当增加。"))
        for index, (label, variable, help_text) in enumerate(fields):
            row = 1 + index * 2
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="nw", padx=px((0, SPACE.lg)), pady=px(SPACE.sm))
            if variable is self.model_name:
                entry = ttk.Combobox(frm, textvariable=variable, values=PRESET_MODELS, width=16)
            else:
                entry = ttk.Entry(frm, textvariable=variable, width=16, show="*" if variable is self.api_key else "")
            entry.grid(row=row, column=1, sticky="ew", pady=px((SPACE.sm, SPACE.xs)))
            wrapped_label(frm, theme, text=help_text, style=MUTED).grid(
                row=row + 1, column=1, sticky="ew", pady=px((0, SPACE.md)))
            self.entries.append(entry)
            self.variable_traces.append((variable, variable.trace_add("write", self._changed)))
        self.key_hint = window_var(self, "Key 仅保存到 Windows 凭据管理器，不写入日志或明文配置。")
        # Imported/provider diagnostics can be arbitrarily long. Keep them
        # bounded so Tk's child-window coordinate limit cannot unmap the test
        # action below the form when a large message is scrolled to its end.
        self.key_hint_area = MessageArea(frm, self.key_hint, theme, lines=2, muted=True)
        self.key_hint_area.grid(row=100, column=0, columnspan=2, sticky="ew", pady=px(SPACE.md))

        if compact or not self.interactive:
            # The wizard already has a fixed step header and action footer.
            # Keep fee/help text next to its test action in the scrolling body.
            self.intro.grid_remove()
            wrapped_label(frm, theme, text=intro_text, style=MUTED).grid(
                row=99, column=0, columnspan=2, sticky="ew", pady=px(SPACE.sm))
        if not self.interactive:
            # Form-only mode: the host (e.g. the first-use wizard) supplies its
            # own test/save buttons and lifecycle; no worker, poll or buttons.
            self.test_button = self.save_button = self.cancel_button = None
            self.progress = self.status_area = None
            self._restore()
            return

        result = ttk.Frame(frm if compact else self)
        result.grid(row=102 if compact else 2, column=0, columnspan=2 if compact else 1,
                    sticky="ew", pady=px((SPACE.md, 0)))
        result.columnconfigure(0, weight=1)
        wrapped_label(result, theme, textvariable=self.result_heading, style=SECTION).grid(row=0, column=0, sticky="ew")
        self.status_area = MessageArea(result, self.status, theme, lines=1 if compact else 3)
        self.status_area.grid(row=1, column=0, sticky="ew", pady=px(SPACE.sm))
        self.progress = ttk.Progressbar(result, mode="indeterminate")
        self.progress.grid(row=2, column=0, sticky="ew")
        self.operation_hint = window_var(self, "保存不可用：请先测试当前配置。尚无进行中的任务。")
        if compact:
            operation_area = MessageArea(result, self.operation_hint, theme, lines=1, muted=True)
            operation_area.configure(padding=px(SPACE.xs))
            operation_area.grid(row=3, column=0, sticky="ew", pady=px((0, SPACE.xs)))
        else:
            wrapped_label(result, theme, textvariable=self.operation_hint, style=MUTED).grid(
                row=3, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        btns = FlowActions(self if compact else result, theme)
        btns.grid(row=2 if compact else 4, column=0, sticky="ew")
        self.test_button = btns.add(ttk.Button(btns, text="测试连接", command=self._test))
        self.save_button = btns.add(ttk.Button(btns, text="保存", style=PRIMARY, command=self._save))
        self.cancel_button = btns.add(ttk.Button(btns, text="取消测试", command=self._cancel_test))

        self.bind("<Destroy>", self._destroyed, add="+")
        self._restore()
        self._sync_controls()
        self.poll = self.after(40, self._drain)

    # -- snapshot / signature -------------------------------------------------
    def reveal(self, widget):
        """Delegate to the embedded scrollable form view."""
        self.form_view.reveal(widget)

    def _signature(self):
        return (self.revision, self.base_url.get(), self.model_name.get(),
                self.api_key.get(), self.timeout.get())

    @staticmethod
    def _settings(signature):
        try:
            timeout = float(signature[4])
        except ValueError:
            raise onboarding.SetupError("timeout", "连接超时请填写有效秒数，再重新测试。") from None
        return onboarding.effective_settings(signature[1], signature[2], signature[3], timeout)

    def _restore(self):
        """Load the persisted public configuration; never resolve the Key into Tk."""
        from . import config
        self.restoring = True
        cfg = config.load_config()
        llm = cfg.get("llm")
        llm = llm if isinstance(llm, dict) else {}
        self.base_url.set(llm.get("base_url") or "https://api.deepseek.com/v1")
        self.model_name.set(llm.get("model") or "deepseek-chat")
        self.timeout.set(str(llm.get("timeout_sec", 60)))
        self.api_key.set("")
        self.restoring = False
        self.verified = None
        self.test_signature = None
        self.saved_signature = self._signature()
        has_key = False
        try:
            from . import secrets
            has_key = bool(secrets.resolve_api_key(llm))
        except (ValueError, TypeError, AttributeError):
            has_key = False
        self.key_hint.set("已检测到现有 Key；留空可沿用，保存时会写入 Windows 凭据管理器。" if has_key else
                          "Key 仅保存到 Windows 凭据管理器，不写入日志或明文配置。")

    # -- event handling -------------------------------------------------------
    def _provider_changed(self, _event=None):
        if not self.busy and self.provider_var.get() in PRESET_URLS:
            self.base_url.set(PRESET_URLS[self.provider_var.get()])

    def _changed(self, *_):
        if self.closed or self.restoring:
            return
        self.revision += 1
        self.verified = self.test_signature = None
        self.save_state.set("有未保存的修改")
        if not self.closed:
            if not self.busy:
                self.status.set("配置已修改，请重新测试连接后保存。")
            if self.interactive:
                self._sync_controls()

    def _sync_controls(self):
        if self.closed or not self.interactive:
            return
        if self.closing:
            phase, hint = "正在安全关闭", "保存中不可取消，完成写入后关闭。" if self.task == "save" else "已请求取消；不会保存迟到的测试结果。"
        elif self.busy and self.task == "save":
            phase, hint = "进行中 · 保存配置", "正在写入配置与凭据，当前不可取消；保存完成后可关闭。"
        elif self.busy:
            phase = "正在取消测试" if self.cancel.is_set() else "进行中 · 测试连接"
            hint = "等待当前网络读取结束或超时；结束前不能再次测试。" if self.cancel.is_set() else "可以取消测试；取消将在当前网络读取结束或超时后生效。"
        elif self.verified is not None:
            phase, hint = "成功 · 连接已验证", "当前配置可以保存；修改任一字段后须重新测试。"
        else:
            phase, hint = self.save_state.get(), "保存不可用：请先测试当前配置。尚无进行中的任务。"
        set_text(self.result_heading, "连接验证与保存 · " + phase)
        set_text(self.operation_hint, hint)
        locked = self.busy or self.closing
        if locked:
            self.progress.grid()
        else:
            self.progress.grid_remove()
        self.test_button.configure(text="测试中…" if self.busy and self.task == "test" else ("重新测试" if self.test_failed else "测试连接"))
        self.test_button.state(["disabled"] if locked else ["!disabled"])
        verified = self.verified is not None and self.test_signature == self._signature()
        self.save_button.configure(text="保存中…" if self.busy and self.task == "save" else "保存")
        self.save_button.state(["!disabled"] if not locked and verified else ["disabled"])
        self.cancel_button.configure(text="正在取消…" if self.busy and self.cancel.is_set() else "取消测试")
        self.cancel_button.state(["!disabled"] if self.busy and self.task == "test" and not self.cancel.is_set() and not self.closing else ["disabled"])
        self.provider.configure(state="disabled" if locked else "readonly")
        for entry in self.entries:
            entry.state(["disabled"] if locked else ["!disabled"])

    # -- actions --------------------------------------------------------------
    def _test(self):
        if self.busy or self.closed or self.closing:
            return
        signature = self._signature()
        self.verified = self.test_signature = None
        self.cancel = threading.Event()
        cancel = self.cancel
        settings_for = type(self)._settings
        opener = self.opener

        def test():
            return signature, onboarding.test_connection(settings_for(signature), cancel=cancel, opener=opener)
        self._job("test", test, "正在后台测试连接…")

    def _save(self):
        if self.busy or self.closed or self.closing:
            return
        signature, verified = self._signature(), self.verified
        if verified is None or signature != self.test_signature:
            self.status.set("请先测试当前配置，成功后再保存。")
            return
        settings_for = type(self)._settings

        def save():
            if settings_for(signature) != verified.settings:
                raise onboarding.SetupError("changed", "生效配置或 Key 已变化，请重新测试连接后保存。")
            onboarding.save_verified_model(verified)
        self._job("save", save, "正在保存已验证配置及 Windows 凭据…")

    def _cancel_test(self):
        if self.closed or self.closing or not self.busy or self.task != "test" or self.cancel.is_set():
            return
        self.cancel.set()
        self.verified = self.test_signature = None
        self.status.set("正在取消测试；当前网络读取结束或超时后可重新测试，取消结果不会用于保存。")
        self._sync_controls()

    def _job(self, tag, action, message):
        if self.busy or self.closed or self.closing:
            return
        self.busy, self.task = True, tag
        self.status.set(message)
        self.progress.start(15)
        self._sync_controls()
        events = self.events

        def work():
            try:
                events.put((tag, action(), None))
            except onboarding.SetupError as error:
                events.put((tag, None, str(error)))
            except Exception as error:
                events.put((tag, None, f"操作失败（{type(error).__name__}），请检查配置或凭据服务后重试。"))
        self.worker = threading.Thread(target=work, daemon=True, name="bagu-model-" + tag)
        self.worker.start()

    def _drain(self):
        if self.closed:
            return
        cancel_timer(self, self.poll)
        self.poll = None
        try:
            tag, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            if self.closing:
                self._close()
                return
            if error:
                self.verified = self.test_signature = None
                self.test_failed = True
                self.save_state.set("已取消" if self.cancel.is_set() and tag == "test" else "失败 · 请重新测试")
                self.status.set(error + " 点击“重新测试”后重试。")
            elif tag == "test":
                signature, verified = result
                if signature == self._signature() and not self.cancel.is_set():
                    self.verified, self.test_signature = verified, signature
                    self.test_failed = False
                    self.status.set("连接测试成功，请点击保存。修改任一项后须重新测试。")
                else:
                    self.test_failed = True
                    self.save_state.set("已取消或配置已变化")
                    self.status.set("输入已变化或测试已取消，请重新测试当前配置。")
            else:
                self.verified = self.test_signature = None
                self.api_key.set("")
                self.status.set("已保存并更新模型验证状态，即时生效；Key 输入框已清空。")
                self.save_state.set("已保存 · 即时生效")
                self.saved_signature = self._signature()
                if self.on_saved is not None:
                    self.on_saved()
            self._sync_controls()
        if not self.closed:
            self.poll = self.after(40, self._drain)

    def _destroyed(self, event):
        if event.widget is self:
            self.closed = True
            clear_variable_traces(self.variable_traces)
            self.cancel.set()
            self.verified = self.test_signature = None
            if self.poll is not None:
                cancel_timer(self, self.poll)
                self.poll = None

    def close(self):
        """Request close; a running save completes first, a test is cancelled."""
        if self.closed:
            return
        self.closing = True
        self.cancel.set()
        self.verified = self.test_signature = None
        if self.busy and self.task == "save":
            self.status.set("正在完成保存，随后关闭…")
            self._sync_controls()
            return
        self.api_key.set("")
        self.destroy()

    def _close(self):
        # Called from _drain after a pending save finished during closing.
        if self.busy:
            return
        self.api_key.set("")
        self.destroy()
