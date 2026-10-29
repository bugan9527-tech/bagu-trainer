"""Capture isolated real Tk windows for manual display review on Windows.

No network, credentials, monitoring or learning writes. --tk-scale changes only
Tk's point conversion; it never changes or certifies Windows display scaling.
The JSON records captures/errors/geometry, not a visual acceptance verdict.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tk-scale", type=float, choices=(1, 1.25, 1.5, 2))
    parser.add_argument("--fallback-font", action="store_true", help="simulate missing preferred YaHei fonts")
    parser.add_argument("--normal", action="store_true", help="capture short everyday content instead of stress fixtures")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "ui-display" / "native")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bagu-display-") as user:
        os.environ["BAGU_USER_DATA_DIR"] = user
        os.environ.pop("BAGU_LLM_API_KEY", None)
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        from bagu_trainer.desktop.tk_runtime import prepare_tk
        prepare_tk()
        import tkinter as tk
        import ttkbootstrap  # load the real Tk subclass before installing the fixture
        from PIL import ImageGrab
        from bagu_trainer.desktop import ui_theme
        from bagu_trainer.application.monitor import DesktopMonitor
        from bagu_trainer.desktop import DesktopApp
        real_tk = tk.Tk
        if args.tk_scale is not None:
            def scaled_root():
                root = real_tk()
                root.tk.call("tk", "scaling", args.tk_scale * 96 / 72)
                return root
            tk.Tk = scaled_root
        if args.fallback_font:
            ui_theme.FONT_FAMILIES = ("Unavailable Chinese Font", "Noto Sans SC", "Noto Sans CJK SC", "SimHei", "SimSun")
        report = {"mode": "tk-simulation" if args.tk_scale else "native-windows",
                  "requested_tk_scale": args.tk_scale,
                  "fallback_simulation": args.fallback_font,
                  "content": "normal" if args.normal else "long",
                  "system_dpi": ctypes.windll.user32.GetDpiForSystem(),
                  "windows_primary_scale_percent": ctypes.windll.shcore.GetScaleFactorForDevice(0),
                  "process_dpi_aware": bool(ctypes.windll.user32.IsProcessDPIAware()),
                  "visual_acceptance": "requires manual image inspection",
                  "captures": [], "tk_errors": []}
        def capture(root, name, controls=()):
            root.update()
            root.lift()
            root.update()
            hwnd = int(root.tk.call("wm", "frame", root._w), 16)
            from ctypes import wintypes
            ctypes.windll.user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
            ctypes.windll.user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
            ctypes.windll.user32.MonitorFromWindow.restype = wintypes.HANDLE
            ctypes.windll.shcore.GetScaleFactorForMonitor.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_int))
            monitor_scale = ctypes.c_int()
            scale_result = ctypes.windll.shcore.GetScaleFactorForMonitor(
                ctypes.windll.user32.MonitorFromWindow(hwnd, 2), ctypes.byref(monitor_scale))
            record = {"name": name, "client_geometry": root.winfo_geometry(),
                      "tk_scaling": float(root.tk.call("tk", "scaling")),
                      "window_dpi": ctypes.windll.user32.GetDpiForWindow(hwnd),
                      "windows_monitor_scale_percent": monitor_scale.value if scale_result == 0 else None,
                      "font": dict(root._root()._bagu_theme.fonts["body"].actual()),
                      "work_area": ui_theme.work_area(root), "controls": []}
            for control in controls:
                x = control.winfo_rootx() - root.winfo_rootx()
                y = control.winfo_rooty() - root.winfo_rooty()
                record["controls"].append({"name": str(control), "size": [control.winfo_width(), control.winfo_height()],
                    "requested_width": control.winfo_reqwidth(),
                    "inside": x >= 0 and y >= 0 and x + control.winfo_width() <= root.winfo_width()
                              and y + control.winfo_height() <= root.winfo_height()})
            try:
                image = ImageGrab.grab(window=hwnd)
                image.save(output / f"{name}.png")
                record["screenshot"] = f"{name}.png"
                record["image_size"] = list(image.size)
            except Exception as error:
                record["screenshot_error"] = f"{type(error).__name__}: {error}"
            report["captures"].append(record)

        def errors(kind, error, _traceback):
            report["tk_errors"].append(f"{kind.__name__}: {error}")
        mixed = "中文与 English 混排：缓存 Cache、事务 Transaction<T>、UTF-8，标点不丢失。"
        identifier = "provider/VeryLongModelName-Qwen3-235B-A22B-Instruct-2026-10-Preview" * 4
        path = "C:/学习项目/My English Workspace/" + "项目很长的目录名/Backend.Service/" * 12 + "最后目录-END"
        if args.normal:
            identifier = "deepseek-chat"
            path = "C:/学习项目/BaguaDemo"
        # A display-only ready bank lets the ordinary delivery gate accept the
        # synthetic quiz without loading local questions or starting workers.
        engine = SimpleNamespace(learning=SimpleNamespace(has_question_bank=lambda: True))
        app = DesktopApp(DesktopMonitor(engine))
        app.root.report_callback_exception = errors
        try:
            app.root.geometry("%dx%d" % app.root.minsize())
            app.show_restored_configuration()
            capture(app.root, "desktop-restored-configuration", [app.view_data_button, app.edit_restored_button])
            app.view_data_button.invoke()
            settings_page = app._page_frame("settings")
            settings_page.view.reveal(settings_page.open_data_button)
            capture(app.root, "settings-data-locations", [settings_page.open_data_button, settings_page.model_button])
            settings_page.model_button.invoke()
            model_page = app._page_frame("model")
            model_page.show_key_source("keyring")  # Display fixture only, no vault read.
            model_page.form_view.reveal(model_page.key_hint_area)
            capture(app.root, "model-key-source", [model_page.test_button, model_page.save_button])
            app._navigate_page("dashboard")
            app._render_dashboard({"state": "watching", "projects": [path],
                "stats": {"ok": True, "total_attempts": 1234, "accuracy": .875, "due_reviews": 16}})
            app.status.set(mixed * (1 if args.normal else 5))
            app.root.update()
            app.dashboard_view.canvas.yview_moveto(1)
            app.resume_button.focus_force()
            capture(app.root, "desktop-small-focus", [app.resume_button, app.configure_button, app.pause_button])
            app.root.state("zoomed")
            capture(app.root, "desktop-maximized", [app.resume_button, app.configure_button])
            app._show_quiz({"quiz_id": "display-fixture", "knowledge_point": "缓存一致性 Cache consistency / Redis",
                "project_root": path, "question": "数据库提交后应如何处理旧缓存？" if args.normal else (mixed + "LongUnbrokenIdentifier" * 4 + "\n") * 16,
                "options": {letter: mixed if args.normal else mixed * 3 + identifier for letter in "ABCD"}})
            app._navigate_page("practice")
            app.root.update()
            queue_page = app._page_frame("practice")
            queue_page.set_pending([app.practice.current], 4)
            practice = app._answer_window()
            practice.show()
            practice.geometry("%dx%d" % practice.minsize())
            app.root.state("normal")
            app.root.geometry("1200x800+0+0")
            capture(app.root, "quiz-pending-list", [queue_page.enter_button])
            app.root.geometry("%dx%d" % app.root.minsize())
            capture(practice, "quiz-question-small", [practice.submit_button, practice.dismiss_button])
            practice.view.canvas.focus_force()
            practice.view.canvas.event_generate("<End>")
            capture(practice, "quiz-options-end-focus", [practice.submit_button, practice.dismiss_button])
            app.practice.apply_grade({"ok": True, "correct": False, "correct_answer": "B", "your_answer": "A",
                            "analysis": "先提交数据库，再删除缓存，并对删除失败进行重试。" if args.normal else (mixed + "SQL.Transaction<Result>.Commit/rollback\n") * 45 + "解析末尾-END",
                            "schedule": {"interval_days": 2, "next_review_at": 1792771200}})
            capture(practice, "quiz-analysis-small", [practice.submit_button, practice.dismiss_button])
            practice.view.canvas.yview_moveto(1)
            capture(practice, "quiz-review-end", [practice.submit_button, practice.dismiss_button])
            practice.state("zoomed")
            capture(practice, "quiz-maximized", [practice.submit_button, practice.dismiss_button])
        finally:
            app.stop.set()
            app.root.destroy()
        (output / "capture-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "mode": report["mode"], "captures": len(report["captures"]),
                          "capture_failures": [r for r in report["captures"] if "screenshot_error" in r],
                          "tk_errors": report["tk_errors"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
