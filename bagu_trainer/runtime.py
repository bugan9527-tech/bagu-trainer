"""Windowed entry, bounded local logs and package diagnostics."""
import json
import logging
import logging.handlers
import os
import struct
import sys


def configure_logs():
    from . import config, secrets
    directory = config.USER_DATA_ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bagu-trainer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(directory / "desktop.log", maxBytes=2 * 1024 * 1024,
                                                       backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    class Stream:
        encoding = "utf-8"
        def __init__(self, original):
            self.original, self.buffer = original, ""
        def write(self, value):
            safe = secrets.redact_text(str(value), [os.environ.get(secrets.ENV_KEY, ""), *secrets.known_keys()])
            if self.original is not None:
                self.original.write(safe)
            self.buffer += safe
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if line.strip():
                    logger.info(line)
            return len(value)
        def flush(self):
            if self.original is not None:
                self.original.flush()
            if self.buffer:
                logger.info(self.buffer)
                self.buffer = ""
        def isatty(self):
            return False
    sys.stdout, sys.stderr = Stream(sys.stdout), Stream(sys.stderr)
    return logger


def package_report(destination):
    from . import config, paths
    from .bank_build.service import load_sources
    from .bank_storage import atomic_json
    from .tk_runtime import prepare_tk, create_root
    from .version import __version__
    prepare_tk()
    import tkinter as tk
    import keyring
    import ssl
    import sqlite3
    root = create_root()
    try:
        from tkinter import ttk
        from .ui_theme import apply_theme, COLORS, PRIMARY, SECONDARY, DANGER
        from importlib.metadata import version
        theme = apply_theme(root)
        root.withdraw()
        widgets = [ttk.Button(root, text="主要操作 Primary", style=PRIMARY),
                   ttk.Button(root, text="次要操作", style=SECONDARY),
                   ttk.Button(root, text="危险操作", style=DANGER), ttk.Entry(root),
                   ttk.Combobox(root, values=["中文模型 Model"]),
                   ttk.Radiobutton(root, text="中文选项 Option"),
                   ttk.Treeview(root), ttk.Progressbar(root), ttk.Scrollbar(root)]
        for widget in widgets:
            widget.pack()
        root.update_idletasks()
        tcl_version = str(root.tk.call("info", "patchlevel"))
        ui_theme = theme.style.theme_use()
        ui_font = theme.fonts["body"].actual("family")
        ui_diagnostics = {
            "widgets": [{"class": widget.winfo_class(), "width": widget.winfo_reqwidth(),
                         "height": widget.winfo_reqheight()} for widget in widgets],
            "fonts": {role: value.actual() for role, value in theme.fonts.items()},
            "mixed_text_width": theme.fonts["body"].measure("中文 English 123，学习工具"),
            "image_count": len(root.tk.call("image", "names")),
            "tk_library": str(root.tk.call("set", "tk_library")),
            "tcl_library": str(root.tk.call("info", "library")),
            "visual_acceptance": "runtime diagnostics only; inspect captured windows separately",
            "tk_init_retried": bool(getattr(root, "_bagu_tk_recovered", False)),
        }
        from .ui_icons import make_icon
        from .ui_shell import AppShell
        ui_diagnostics["navigation_icons"] = {}
        for name, _ in AppShell.PAGES:
            icon = make_icon(theme, name, COLORS.primary)
            ui_diagnostics["navigation_icons"][name] = {"width": icon.width(), "height": icon.height()}
    finally:
        root.destroy()
    backend = keyring.get_keyring()
    report = {"version": __version__, "frozen": paths.FROZEN, "architecture": struct.calcsize("P") * 8,
              "resources": str(paths.RESOURCE_ROOT), "installation": str(paths.INSTALL_ROOT),
              "user_data": str(config.USER_DATA_ROOT), "config": str(config.CONFIG_PATH),
              "database": str(config.db_path()), "tcl_version": tcl_version,
              "ui_theme": ui_theme, "ui_font": ui_font,
              "ttkbootstrap": version("ttkbootstrap"), "pillow": version("pillow"),
              "ui_diagnostics": ui_diagnostics,
              "keyring_backend": type(backend).__module__ + "." + type(backend).__name__,
              "sources": len(load_sources()), "sqlite": sqlite3.sqlite_version,
              "tls": ssl.OPENSSL_VERSION}
    atomic_json(destination, report)
    return report


def run(argv=None):
    if os.path.basename(sys.executable).lower() == "bagucleanup.exe":
        from .cleanup_cli import main
        return main(argv)
    from .cleanup_cli import journal_path
    # A failed/interrupted full uninstall owns the profile until recovery;
    # do not recreate logs or config before handing off to its retry tool.
    logger = logging.getLogger("bagu-trainer") if journal_path().is_file() else configure_logs()
    try:
        from .desktop import main
        return main(argv)
    except Exception as error:
        logger.error("启动失败（%s）；请检查用户目录中的配置和依赖。", type(error).__name__)
        if os.name == "nt":
            import ctypes
            from .config import USER_DATA_ROOT
            ctypes.windll.user32.MessageBoxW(None, f"训练器未能启动（{type(error).__name__}）。\n"
                f"用户数据和日志位于：\n{USER_DATA_ROOT}", "AI 八股训练器", 0x10)
        return 1
