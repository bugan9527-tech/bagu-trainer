"""Read-only installation resources and version-independent per-user storage."""
import os
import sys
from pathlib import Path


FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parents[2]
INSTALL_ROOT = Path(sys.executable).resolve().parent if FROZEN else RESOURCE_ROOT


def default_user_root():
    override = os.environ.get("BAGU_USER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("BAGU_USER_DATA_DIR 必须是绝对路径")
        return path
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buffer) != 0:
                raise OSError("无法定位当前用户的 LOCALAPPDATA")
            local = buffer.value
        return Path(local) / "BaguTrainer"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "BaguTrainer"


USER_ROOT = default_user_root()


def command(*arguments):
    if FROZEN:
        return [sys.executable, *arguments]
    return [sys.executable, "-B", str(RESOURCE_ROOT / "scripts" / "start_desktop.py"), *arguments]


def canonical_project_root(path):
    """Canonical Windows-aware project scope key; empty/invalid input stays empty."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(raw))
    except (OSError, ValueError):
        return ""
