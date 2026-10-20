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
    from .tk_runtime import prepare_tk
    from .version import __version__
    prepare_tk()
    import tkinter as tk
    import keyring
    import ssl
    import sqlite3
    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        tcl_version = str(root.tk.call("info", "patchlevel"))
    finally:
        root.destroy()
    backend = keyring.get_keyring()
    report = {"version": __version__, "frozen": paths.FROZEN, "architecture": struct.calcsize("P") * 8,
              "resources": str(paths.RESOURCE_ROOT), "installation": str(paths.INSTALL_ROOT),
              "user_data": str(config.USER_DATA_ROOT), "config": str(config.CONFIG_PATH),
              "database": str(config.db_path()), "tcl_version": tcl_version,
              "keyring_backend": type(backend).__module__ + "." + type(backend).__name__,
              "sources": len(load_sources()), "sqlite": sqlite3.sqlite_version,
              "tls": ssl.OPENSSL_VERSION}
    atomic_json(destination, report)
    return report


def run(argv=None):
    logger = configure_logs()
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
