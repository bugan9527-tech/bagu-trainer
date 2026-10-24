"""Application locations, configuration shortcuts and login-startup settings."""
import os
from tkinter import ttk

from . import config, login_startup
from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var
from .ui_theme import SPACE, PRIMARY, SECTION, MUTED
from .version import __version__
from . import paths


class SettingsPage(ttk.Frame):
    def __init__(self, parent, theme, *, on_status=None, on_navigate=None, on_uninstall=None):
        super().__init__(parent)
        self.theme = theme
        px = theme.px
        self.on_status = on_status
        self.closed = False
        self.busy = False
        self.on_uninstall = on_uninstall

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        view = ScrollableFrame(self, theme)
        self.view = view
        view.grid(row=0, column=0, sticky="nsew")
        body = view.body
        body.columnconfigure(0, weight=1)

        # Version
        wrapped_label(body, theme, text="关于", style=SECTION).grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        wrapped_label(body, theme, text=f"AI 八股训练器 · 版本 {__version__}", style=MUTED).grid(
            row=1, column=0, sticky="ew", pady=px((0, SPACE.lg)))

        # Program resources and personal data are deliberately separate.
        wrapped_label(body, theme, text="程序目录", style=SECTION).grid(row=2, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.install_dir = window_var(self, str(paths.INSTALL_ROOT))
        wrapped_label(body, theme, textvariable=self.install_dir, style=MUTED).grid(row=3, column=0, sticky="ew")
        wrapped_label(body, theme, text="存放程序及运行资源；源码运行时显示源码目录。升级更新此处的程序文件，个人配置和学习记录另存于下方数据目录。",
                      style=MUTED).grid(row=4, column=0, sticky="ew", pady=px((SPACE.xs, SPACE.lg)))
        wrapped_label(body, theme, text="个人数据目录", style=SECTION).grid(row=5, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        wrapped_label(body, theme, text="配置、下载的题库、学习记录、窗口状态和日志保存在此；升级和修复安装保留，主动完整卸载会清除。安装版与便携版默认共用当前 Windows 用户的数据。", style=MUTED).grid(
            row=6, column=0, sticky="ew")
        self.data_dir = window_var(self, str(config.USER_DATA_ROOT))
        wrapped_label(body, theme, textvariable=self.data_dir, style=MUTED).grid(
            row=7, column=0, sticky="ew", pady=px((SPACE.xs, SPACE.sm)))
        wrapped_label(body, theme, text="· config.json —— 应用配置；新保存的 Key 仅存 Windows 凭据管理器\n"
                      "· data/ —— 题库及学习记录（默认学习库为 data/trainer.db，自定义库路径继续保留）\n"
                      "凭据来源可在模型配置页查看；此页不迁移或清除数据，也不会测试模型连接。", style=MUTED).grid(
            row=8, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        directory_actions = FlowActions(body, theme)
        directory_actions.grid(row=9, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.open_data_button = directory_actions.add(ttk.Button(directory_actions, text="打开数据目录", command=self._open_data_directory))
        if on_navigate is not None:
            self.model_button = directory_actions.add(ttk.Button(directory_actions, text="修改模型配置", command=lambda: on_navigate("model")))
            self.projects_button = directory_actions.add(ttk.Button(directory_actions, text="修改监听项目", command=lambda: on_navigate("projects")))
        self.directory_status = window_var(self)
        wrapped_label(body, theme, textvariable=self.directory_status, style=MUTED).grid(row=10, column=0, sticky="ew")

        # Login startup
        wrapped_label(body, theme, text="登录自启动", style=SECTION).grid(row=11, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        wrapped_label(body, theme, text="开启后，登录 Windows 会在后台最小化启动训练器，继续监听代码改动。",
                      style=MUTED).grid(row=12, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        actions = FlowActions(body, theme)
        actions.grid(row=13, column=0, sticky="ew")
        self.startup_button = actions.add(ttk.Button(actions, text="启用登录自启动", style=PRIMARY, command=lambda: self._set_startup(True)))
        self.uninstall_button = actions.add(ttk.Button(actions, text="取消登录自启动", command=lambda: self._set_startup(False)))
        self.startup_state = window_var(self, self._startup_status())
        wrapped_label(body, theme, textvariable=self.startup_state, style=MUTED).grid(row=14, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        wrapped_label(body, theme, text="完整卸载", style=SECTION).grid(row=15, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        self.cleanup_status = window_var(self, "清除本应用管理的全部题库、记录、配置和本机 API 凭据。先查看清理范围，再确认退出并交接卸载工具。")
        wrapped_label(body, theme, textvariable=self.cleanup_status).grid(row=16, column=0, sticky="ew")
        self.cleanup_preview = ttk.Button(body, text="卸载应用并清除全部数据…", command=self._preview_cleanup)
        self.cleanup_preview.grid(row=17, column=0, sticky="w", pady=px(SPACE.sm))
        self.cleanup_confirm = ttk.Button(body, text="确认退出并进入完整卸载", command=self._confirm_cleanup)
        self.cleanup_confirm.grid(row=18, column=0, sticky="w")
        self.cleanup_confirm.grid_remove()
        import queue
        self.cleanup_events = queue.Queue()
        self.cleanup_worker = None
        self.cleanup_poll = None

    def _preview_cleanup(self):
        if self.cleanup_worker is not None and self.cleanup_worker.is_alive():
            return
        import threading
        from .cleanup_cli import preview
        events = self.cleanup_events
        def work():
            try:
                events.put((preview(), True))
            except Exception as error:
                events.put((f"清理范围检查失败：{error}。请修复后重试。", False))
        self.cleanup_status.set("正在后台核对数据位置和归属…")
        self.cleanup_preview.state(["disabled"])
        self.cleanup_worker = threading.Thread(target=work, daemon=True, name="bagu-uninstall-plan")
        self.cleanup_worker.start()
        self.cleanup_poll = self.after(100, self._poll_cleanup)

    def _poll_cleanup(self):
        import queue
        try:
            message, ready = self.cleanup_events.get_nowait()
        except queue.Empty:
            self.cleanup_poll = self.after(100, self._poll_cleanup)
            return
        self.cleanup_poll = None
        self.cleanup_status.set(message)
        self.cleanup_preview.state(["!disabled"])
        if ready:
            self.cleanup_confirm.grid()

    def _confirm_cleanup(self):
        if self.on_uninstall is not None:
            self.on_uninstall()

    def _open_data_directory(self):
        try:
            os.startfile(str(config.USER_DATA_ROOT))
            self.directory_status.set("已打开个人数据目录。")
        except OSError:
            self.directory_status.set("无法打开个人数据目录，请检查目录是否存在及访问权限。")

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
        self.install_dir.set(str(paths.INSTALL_ROOT))
        self.data_dir.set(str(config.USER_DATA_ROOT))
        self.startup_state.set(self._startup_status())
