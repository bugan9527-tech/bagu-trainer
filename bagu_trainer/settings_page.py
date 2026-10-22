"""Read-only + login-startup application settings page."""
from tkinter import ttk

from . import config, login_startup
from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var
from .ui_theme import SPACE, PRIMARY, SECTION, MUTED
from .version import __version__


class SettingsPage(ttk.Frame):
    def __init__(self, parent, theme, *, on_status=None):
        super().__init__(parent)
        self.theme = theme
        px = theme.px
        self.on_status = on_status
        self.closed = False
        self.busy = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        view = ScrollableFrame(self, theme)
        view.grid(row=0, column=0, sticky="nsew")
        body = view.body
        body.columnconfigure(0, weight=1)

        # Version
        wrapped_label(body, theme, text="关于", style=SECTION).grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        wrapped_label(body, theme, text=f"AI 八股训练器 · 版本 {__version__}", style=MUTED).grid(
            row=1, column=0, sticky="ew", pady=px((0, SPACE.lg)))

        # Data directory
        wrapped_label(body, theme, text="数据目录", style=SECTION).grid(row=2, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        wrapped_label(body, theme, text="配置、题库和学习记录保存在用户数据目录，卸载程序不会删除：", style=MUTED).grid(
            row=3, column=0, sticky="ew")
        self.data_dir = window_var(self, str(config.USER_DATA_ROOT))
        wrapped_label(body, theme, textvariable=self.data_dir, style=MUTED).grid(
            row=4, column=0, sticky="ew", pady=px((SPACE.xs, SPACE.sm)))
        wrapped_label(body, theme, text="· config.json —— 应用配置（模型 Key 不写入明文，仅存 Windows 凭据管理器）\n"
                      "· data/trainer.db —— 学习记录\n· data/questions.json —— 当前题库", style=MUTED).grid(
            row=5, column=0, sticky="ew", pady=px((0, SPACE.lg)))

        # Login startup
        wrapped_label(body, theme, text="登录自启动", style=SECTION).grid(row=6, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        wrapped_label(body, theme, text="开启后，登录 Windows 会在后台最小化启动训练器，继续监听代码改动。",
                      style=MUTED).grid(row=7, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        actions = FlowActions(body, theme)
        actions.grid(row=8, column=0, sticky="ew")
        self.startup_button = actions.add(ttk.Button(actions, text="启用登录自启动", style=PRIMARY, command=lambda: self._set_startup(True)))
        self.uninstall_button = actions.add(ttk.Button(actions, text="取消登录自启动", command=lambda: self._set_startup(False)))
        self.startup_state = window_var(self, self._startup_status())
        wrapped_label(body, theme, textvariable=self.startup_state, style=MUTED).grid(row=9, column=0, sticky="ew", pady=px((SPACE.sm, 0)))

    def _startup_status(self):
        try:
            target = login_startup.shortcut_directory() / "BaguTrainer.lnk"
            return "已启用：登录时自动启动。" if target.exists() else "未启用：登录时不会自动启动。"
        except Exception:
            return "无法读取自启动状态。"

    def _set_startup(self, enable):
        if self.busy:
            return
        self.busy = True
        self.startup_button.state(["disabled"])
        self.uninstall_button.state(["disabled"])
        try:
            if enable:
                login_startup.install()
            else:
                login_startup.uninstall()
            self.startup_state.set("已启用：登录时自动启动。" if enable else "已取消：登录时不会自动启动。")
        except Exception as error:
            self.startup_state.set(f"操作失败（{type(error).__name__}）：请检查系统权限后重试。")
        self.busy = False
        self.startup_button.state(["!disabled"])
        self.uninstall_button.state(["!disabled"])

    def refresh(self):
        self.data_dir.set(str(config.USER_DATA_ROOT))
        self.startup_state.set(self._startup_status())
