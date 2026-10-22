# -*- coding: utf-8 -*-
"""AI 接口配置窗口（独立 Tk/Toplevel），复用公共 ModelPanel。

打开方式:
  1. 桌面主窗口点击「配置出题模型」
  2. 手动: python scripts/config_window.py

填写 base_url / api_key / model / timeout，「测试连接」验证 key 后「保存」，
写入用户数据目录 config.json 并即时生效（出题器每次读最新配置，无需重启）。
"""
from bagu_trainer.tk_runtime import prepare_tk, create_root
prepare_tk()

import tkinter as tk
from tkinter import ttk

from bagu_trainer.ui_theme import apply_theme, SPACE, TITLE, MUTED
from bagu_trainer.ui_layout import wrapped_label
from bagu_trainer.model_panel import ModelPanel


class ModelSettingsWindow:
    """Own Tk on the caller thread; the panel's workers communicate via a queue."""

    def __init__(self, parent=None):
        self.root = tk.Toplevel(parent) if parent is not None else create_root()
        self.theme = apply_theme(self.root)
        px = self.theme.px
        self.root.title("AI 八股训练器 · AI 接口配置")
        self.theme.window(self.root, (760, 760), (560, 540))

        panel = ttk.Frame(self.root, padding=px(SPACE.xl))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        wrapped_label(panel, self.theme, text="模型连接", style=TITLE).grid(row=0, column=0, sticky="ew")
        wrapped_label(panel, self.theme, text="先测试当前配置，再保存生效。新题需经过 AI 生成与关联审核，共用所填超时；失败可重试，不展示离线选择题。",
                      style=MUTED).grid(row=1, column=0, sticky="ew", pady=px((SPACE.sm, SPACE.lg)))

        self.panel_widget = ModelPanel(panel, self.theme)
        self.panel_widget.grid(row=2, column=0, sticky="nsew")

        # Expose the legacy attribute names so existing callers/tests keep working.
        self.status = self.panel_widget.status
        self.save_state = self.panel_widget.save_state
        self.result_heading = self.panel_widget.result_heading
        self.operation_hint = self.panel_widget.operation_hint
        self.test_button = self.panel_widget.test_button
        self.save_button = self.panel_widget.save_button
        self.cancel_button = self.panel_widget.cancel_button
        self.progress = self.panel_widget.progress
        self.entries = self.panel_widget.entries
        self.variable_traces = self.panel_widget.variable_traces
        self.form_view = self.panel_widget.form_view
        self.status_area = self.panel_widget.status_area
        self.provider = self.panel_widget.provider
        self.provider_var = self.panel_widget.provider_var
        self.base_url = self.panel_widget.base_url
        self.model_name = self.panel_widget.model_name
        self.api_key = self.panel_widget.api_key
        self.timeout = self.panel_widget.timeout
        self.worker = None

        # Legacy alias variables used by tests (url_var/model_var/key_var/timeout_var).
        self.url_var = self.panel_widget.base_url
        self.model_var = self.panel_widget.model_name
        self.key_var = self.panel_widget.api_key
        self.timeout_var = self.panel_widget.timeout

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._closing_via_panel = False
        self.panel_widget.bind("<Destroy>", self._panel_destroyed, add="+")

    def _panel_destroyed(self, event):
        # Only tear down the root when the panel was closed by our own flow.
        # An external root.destroy() tears the root down itself, and calling
        # destroy() again inside that recursion raises a Tcl error.
        if event.widget is not self.panel_widget or not self._closing_via_panel:
            return
        self._closing_via_panel = False
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except Exception:
            pass

    @property
    def closing(self):
        return self.panel_widget.closing

    @closing.setter
    def closing(self, value):
        self.panel_widget.closing = value

    @property
    def closed(self):
        return self.panel_widget.closed

    @closed.setter
    def closed(self, value):
        self.panel_widget.closed = value

    def _signature(self):
        return self.panel_widget._signature()

    @staticmethod
    def _settings(signature):
        return ModelPanel._settings(signature)

    def _test(self):
        self.panel_widget._test()
        self.worker = self.panel_widget.worker

    def _save(self):
        self.panel_widget._save()
        self.worker = self.panel_widget.worker

    def _cancel_test(self):
        self.panel_widget._cancel_test()

    @property
    def busy(self):
        return self.panel_widget.busy

    @property
    def verified(self):
        return self.panel_widget.verified

    @verified.setter
    def verified(self, value):
        self.panel_widget.verified = value

    @property
    def test_signature(self):
        return self.panel_widget.test_signature

    @test_signature.setter
    def test_signature(self, value):
        self.panel_widget.test_signature = value

    @property
    def cancel(self):
        return self.panel_widget.cancel

    @property
    def task(self):
        return self.panel_widget.task

    @property
    def test_failed(self):
        return self.panel_widget.test_failed

    @test_failed.setter
    def test_failed(self, value):
        self.panel_widget.test_failed = value

    @property
    def revision(self):
        return self.panel_widget.revision

    @revision.setter
    def revision(self, value):
        self.panel_widget.revision = value

    @property
    def events(self):
        return self.panel_widget.events

    def _close(self):
        # Delegate entirely to the panel's close-wait logic: a running save
        # completes first (closing stays True, closed stays False), otherwise
        # the panel is destroyed immediately.  When the panel finishes tearing
        # down we destroy the owning root (Toplevel or standalone Tk).
        self._closing_via_panel = True
        self.panel_widget.close()

    def _destroyed(self, event):
        # Kept for parity; the panel handles its own teardown.
        pass


def build_window(parent=None):
    # Preserve the historical entry point and its Tk/Toplevel return value.
    return ModelSettingsWindow(parent).root


if __name__ == "__main__":
    build_window().mainloop()
