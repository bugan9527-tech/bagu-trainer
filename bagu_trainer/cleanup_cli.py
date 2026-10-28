"""System maintenance UI and portable handoff, independent of engine/model/Tk app."""
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import config, paths
from .app_cleanup import AppCleanup, MaintenanceMutex


def journal_path():
    if os.environ.get("BAGU_TEST_INSTANCE"):
        return config.USER_DATA_ROOT.parent / "bagu-test-uninstall.json"
    return Path(os.environ["LOCALAPPDATA"]) / "BaguTrainer.uninstall.json"


def preview():
    cleanup = AppCleanup.discover(journal_path())
    plan = cleanup.plan()
    from .secrets import api_key_source
    lines = ["将删除全部题库、历史版本、答题记录、题目快照、配置、日志及本机 API 凭据，无法在应用内撤销。",
        "安装版和便携版可能共用这些数据；其他副本再次启动将进入未配置状态。",
        "数据位置：\n" + "\n".join(plan["profiles"]),
        f"本次核对到 {len(plan['files'])} 个受管文件；凭据来源类型：{api_key_source()}。",
        "API 清理仅删除本机凭据，不代表吊销服务商后台的 Key。其他已运行进程继承的环境变量不受影响。",
        "未登记的旧自定义数据位置可在卸载工具中补充；监听项目、源码仓库、共用目录和其他副本的程序不会整体删除。"]
    return "\n\n".join(lines)


def launch_uninstall(*, wait_pid=None):
    owner_pid = wait_pid or os.getpid()
    uninstaller = paths.INSTALL_ROOT / "unins000.exe"
    if paths.FROZEN and uninstaller.is_file():
        subprocess.Popen([str(uninstaller)], cwd=paths.INSTALL_ROOT)
        return
    if paths.FROZEN:
        temporary = Path(tempfile.mkdtemp(prefix="BaguTrainer-cleanup-"))
        # Keep the retry tool and all its runtime dependencies outside the
        # directory being removed. Failure leaves this functional retry entry.
        helper = temporary / "tool"
        from .cleanup_manifest import safe_path
        manifest = paths.INSTALL_ROOT / "BUILD_MANIFEST.json"
        record = json.loads(manifest.read_text(encoding="utf-8"))
        try:
            for entry in [*record["files"], {"path": manifest.name}]:
                relative = Path(entry["path"])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise ValueError("程序清单越界")
                original = safe_path(paths.INSTALL_ROOT / relative)
                destination = helper / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
        except Exception:
            if temporary.parent.resolve() != Path(tempfile.gettempdir()).resolve():
                raise ValueError("辅助目录越界，未清理")
            safe_path(temporary)
            shutil.rmtree(temporary)
            raise
        command = [str(helper / "BaguCleanup.exe"), "--install", str(paths.INSTALL_ROOT),
            "--temporary-tool", str(temporary), "--wait-pid", str(owner_pid)]
    else:
        command = [sys.executable, "-B", "-m", "bagu_trainer.cleanup_cli", "--wait-pid", str(owner_pid)]
    subprocess.Popen(command, cwd=paths.RESOURCE_ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _finish_temporary(directory):
    """A separate system process removes only the verified temporary helper."""
    if not directory:
        return
    directory = Path(directory).absolute()
    expected = Path(tempfile.gettempdir()).resolve()
    if directory.parent.resolve() != expected or not directory.name.startswith("BaguTrainer-cleanup-"):
        raise ValueError("辅助临时目录不在系统临时目录内")
    literal = str(directory).replace("'", "''")
    script_path = Path(tempfile.gettempdir()) / (directory.name + "-retry.ps1")
    # Keep a standalone system retry script if the last helper files are locked.
    # It validates the exact absolute target on every run and never follows links.
    script = f"""$cleanupTarget = '{literal}'
Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
try {{
  $cleanupResolved = [IO.Path]::GetFullPath($cleanupTarget)
  if ($cleanupResolved -ne '{literal}') {{ throw 'Invalid cleanup target' }}
  if (Test-Path -LiteralPath $cleanupResolved) {{
    function Test-CleanupTree([string]$cleanupPath) {{
      $cleanupItem = Get-Item -LiteralPath $cleanupPath -Force
      if (($cleanupItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{ throw 'Reparse point preserved' }}
      if ($cleanupItem.PSIsContainer) {{
        foreach ($cleanupChild in Get-ChildItem -LiteralPath $cleanupPath -Force) {{ Test-CleanupTree $cleanupChild.FullName }}
      }}
    }}
    Test-CleanupTree $cleanupResolved
    Remove-Item -LiteralPath $cleanupResolved -Recurse -Force -ErrorAction Stop
  }}
  if (Test-Path -LiteralPath $cleanupResolved) {{ throw 'Cleanup residual remains' }}
  Remove-Item -LiteralPath ($PSCommandPath + '.residual.txt') -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction Stop
}} catch {{
  ('Cleanup incomplete: ' + $cleanupTarget + [Environment]::NewLine + 'Retry: ' + $PSCommandPath) | Set-Content -LiteralPath ($PSCommandPath + '.residual.txt') -Encoding UTF8
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show(('卸载辅助工具收尾未完成。残留：' + $cleanupTarget + [Environment]::NewLine + '解除占用后运行此脚本重试：' + $PSCommandPath), 'BaguTrainer 清理未完成') | Out-Null
}}
"""
    script_path.write_text(script, encoding="utf-8-sig")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", str(script_path)], creationflags=subprocess.CREATE_NO_WINDOW)


def main(argv=None):
    parser = argparse.ArgumentParser(description="BaguTrainer 完整卸载与残留重试工具")
    parser.add_argument("--install", type=Path)
    parser.add_argument("--maintenance-held", action="store_true")
    parser.add_argument("--confirmed", action="store_true", help="Explicit silent system-uninstaller confirmation")
    parser.add_argument("--wait-pid", type=int)
    parser.add_argument("--temporary-tool", type=Path)
    options = parser.parse_args(argv)
    if options.wait_pid:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x100000, False, options.wait_pid)
        if handle:
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            kernel.CloseHandle(handle)
    if options.confirmed:
        mutex = MaintenanceMutex()
        if not options.maintenance_held and not mutex.acquire():
            return 2
        try:
            from .desktop_instance import OwnerLock
            owner = OwnerLock()
            if not owner.acquire():
                return 2
            owner.close()
            journal = journal_path()
            if journal.exists():
                saved = json.loads(journal.read_text(encoding="utf-8"))
                cleanup = AppCleanup(saved["profiles"], journal, install=options.install, credential_services=saved["services"])
                plan = saved
            else:
                cleanup = AppCleanup.discover(journal, install=options.install)
                plan = cleanup.plan()
            result = cleanup.execute(plan)
            return 0 if result["ok"] else 1
        finally:
            mutex.close()
    from .tk_runtime import create_root
    from .ui_theme import apply_theme
    from .ui_layout import wrapped_label
    from tkinter import ttk, filedialog
    root = create_root()
    theme = apply_theme(root)
    root.title("AI 八股训练器 · 完整卸载")
    theme.window(root, (860, 700), (560, 500))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    import tkinter as tk
    status = tk.StringVar(master=root, value="正在生成清理清单…")
    wrapped_label(root, theme, textvariable=status).grid(row=0, column=0, sticky="ew", padx=16, pady=12)
    text = tk.Text(root, wrap="word", height=15)
    text.grid(row=1, column=0, sticky="nsew", padx=16)
    actions = ttk.Frame(root)
    actions.grid(row=2, column=0, sticky="ew", padx=16, pady=12)
    events, state = queue.Queue(), {"cleanup": None, "plan": None, "busy": False, "success": False}
    mutex = MaintenanceMutex()
    if not options.maintenance_held and not mutex.acquire():
        root.destroy()
        return 2
    def work(kind):
        try:
            if kind == "plan":
                journal = journal_path()
                if journal.exists():
                    saved = json.loads(journal.read_text(encoding="utf-8"))
                    cleanup = AppCleanup(saved["profiles"], journal, install=options.install,
                        credential_services=saved["services"])
                    plan = saved
                else:
                    cleanup = AppCleanup.discover(journal, install=options.install)
                    plan = cleanup.plan()
                events.put(("plan", (cleanup, plan)))
            else:
                result = state["cleanup"].execute(state["plan"], progress=lambda msg: events.put(("status", msg)))
                events.put(("result", result))
        except Exception as error:
            events.put(("error", f"清理未完成：{error}"))
    def start():
        if state["busy"] or state["plan"] is None:
            return
        # A separate owner file lease proves the desktop has really stopped.
        from .desktop_instance import OwnerLock
        owner = OwnerLock()
        if not owner.acquire():
            status.set("训练器仍在运行，请正常退出后重试。")
            return
        owner.close()  # The maintenance mutex protects this final lock deletion.
        state["busy"] = True
        confirm.state(["disabled"])
        threading.Thread(target=work, args=("execute",), daemon=True).start()
    def add_profile():
        if state["busy"] or state["cleanup"] is None:
            return
        value = filedialog.askdirectory(parent=root, title="补充旧版自定义数据目录（只清理可确认的应用文件）")
        if value:
            state["cleanup"].profiles.append(Path(value).absolute())
            state["plan"] = state["cleanup"].plan()
            render_plan()
    def render_plan():
        plan = state["plan"]
        text.delete("1.0", "end")
        text.insert("1.0", "将清除所有题库、作答与快照、配置、缓存、日志及本机 API Key，无法撤销。\n"
            "不会吊销服务商后台的 Key。共享数据会影响此用户的其他副本。\n\n" +
            "\n".join(plan["profiles"]) + f"\n\n受管文件：{len(plan['files'])} 个；本副本程序文件：{len(plan['program_files'])} 个。\n\n" +
            "\n".join(item["path"] for item in plan["files"]) + "\n\n保护对象：\n" + "\n".join(plan["protected"]))
        status.set("请核对清理范围；确认后开始。清理失败会列明残留并保留重试工具。")
        confirm.state(["!disabled"])
    confirm = ttk.Button(actions, text="确认完整卸载 / 重试", command=start)
    confirm.pack(side="left")
    confirm.state(["disabled"])
    ttk.Button(actions, text="补充旧数据位置", command=add_profile).pack(side="left", padx=8)
    def close():
        if state["busy"]:
            status.set("正在清理，请等待安全收尾。")
        else:
            root.destroy()
    ttk.Button(actions, text="关闭", command=close).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", close)
    def drain():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "plan":
                    state["cleanup"], state["plan"] = value
                    render_plan()
                elif kind == "status":
                    status.set(value)
                elif kind == "error":
                    state["busy"] = False
                    status.set(value)
                    confirm.state(["!disabled"])
                elif kind == "result":
                    state["busy"] = False
                    state["success"] = value["ok"]
                    status.set("数据与程序文件已核验；正在完成辅助工具收尾。" if value["ok"] else "清理未完成：请修复以下残留后点击重试。")
                    text.delete("1.0", "end")
                    text.insert("1.0", ("\n".join(value["failures"]) + "\n\n可重试清理工具（关闭本窗口后仍保留）：\n" + sys.executable
                        if value["failures"] else "全部受管对象已核验。其他软件或用户自有文件已保留。"))
                    confirm.state(["disabled"] if value["ok"] else ["!disabled"])
                    if value["ok"]:
                        root.after(1200, root.destroy)
        except queue.Empty:
            pass
        root.after(100, drain)
    threading.Thread(target=work, args=("plan",), daemon=True).start()
    root.after(100, drain)
    try:
        root.mainloop()
    finally:
        mutex.close()
    if state["success"] or not journal_path().exists():
        _finish_temporary(options.temporary_tool)
    return 0 if state["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
