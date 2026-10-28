"""Create a Unicode .lnk using Windows Shell COM; no PowerShell dependency."""
import ctypes
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import paths


def _guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


def shortcut_directory():
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, buffer) != 0:
        raise OSError("无法定位当前用户的登录自启动目录")
    return Path(buffer.value)


@contextmanager
def _shell_link():
    if os.name != "nt":
        raise OSError("登录自启动仅支持 Windows")
    # IShellLinkW / IPersistFile vtables, with pointer-sized calls on x64.
    ole = ctypes.OleDLL("ole32")
    hr = ole.CoInitializeEx(None, 2)
    if hr not in (0, 1):
        raise OSError("COM 初始化失败")
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    def call(pointer, index, types, *args):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        result = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])(pointer, *args)
        if result < 0:
            raise OSError("Windows 快捷方式操作失败")
    try:
        clsid, iid = _guid("00021401-0000-0000-C000-000000000046"), _guid("000214F9-0000-0000-C000-000000000046")
        result = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
        if result < 0:
            raise OSError("无法创建 Windows 快捷方式")
        persist_iid = _guid("0000010B-0000-0000-C000-000000000046")
        call(link, 0, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(persist_iid), ctypes.byref(persist))
        yield link, persist, call
    finally:
        for pointer in (persist, link):
            if pointer:
                call(pointer, 2, [])
        ole.CoUninitialize()


def install(directory=None, *, executable=None):
    destination = Path(directory) if directory else shortcut_directory()
    if not destination.is_dir():
        raise OSError("自启动目录不存在")
    target = Path(executable).resolve() if executable else Path(paths.command()[0])
    arguments = "--background" if paths.FROZEN or executable else '-B "' + str(paths.RESOURCE_ROOT / "scripts" / "start_desktop.py") + '" --background'
    if not paths.FROZEN and not executable:
        target = target.with_name("pythonw.exe")
    if not target.is_file():
        raise OSError("找不到自启动可执行文件")
    temporary = destination / (".bagu-" + uuid.uuid4().hex + ".lnk")
    try:
        with _shell_link() as (link, persist, call):
            call(link, 20, [ctypes.c_wchar_p], str(target))
            call(link, 11, [ctypes.c_wchar_p], arguments)
            call(link, 9, [ctypes.c_wchar_p], str(paths.INSTALL_ROOT))
            call(link, 7, [ctypes.c_wchar_p], "AI 八股训练器 · 登录时后台启动")
            call(link, 15, [ctypes.c_int], 7)
            call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(temporary), 1)
        os.replace(temporary, destination / "BaguTrainer.lnk")
    finally:
        temporary.unlink(missing_ok=True)
    return destination / "BaguTrainer.lnk"


def read_shortcut(path):
    """Read .lnk metadata with Unicode COM; WScript.Shell can lose non-ACP paths."""
    with _shell_link() as (link, persist, call):
        call(persist, 5, [ctypes.c_wchar_p, ctypes.c_uint], str(Path(path).resolve(strict=True)), 0)
        target, arguments, cwd = (ctypes.create_unicode_buffer(32768) for _ in range(3))
        call(link, 3, [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], target, 32768, None, 4)
        call(link, 10, [ctypes.c_wchar_p, ctypes.c_int], arguments, 32768)
        call(link, 8, [ctypes.c_wchar_p, ctypes.c_int], cwd, 32768)
        return {"target": target.value, "arguments": arguments.value, "cwd": cwd.value}


def uninstall(directory=None):
    destination = Path(directory) if directory else shortcut_directory()
    (destination / "BaguTrainer.lnk").unlink(missing_ok=True)
