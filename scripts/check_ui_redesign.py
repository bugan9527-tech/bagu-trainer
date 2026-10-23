"""Capture the real shell with synthetic SQLite history and isolated settings.

No monitor worker, model calls, credential reads, or installed-client updates.
Tk scaling is a simulation and never changes Windows display settings.
"""
import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "ui-redesign-stage6" / "native")
    parser.add_argument("--tk-scale", type=float, choices=(1, 1.25, 1.5, 2))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bagu-stage2-capture-") as isolated:
        os.environ["BAGU_USER_DATA_DIR"] = isolated
        os.environ.pop("BAGU_LLM_API_KEY", None)
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        data = Path(isolated) / "data"
        data.mkdir()
        question = {"id": "screenshot-cache", "knowledge_point": "缓存穿透", "tags": ["缓存"],
                    "keywords": ["缓存"], "question": "合成截图样例：缓存未命中时，如何避免重复查询？"}
        (data / "questions.json").write_text(json.dumps([question], ensure_ascii=False), encoding="utf-8")
        projects = [r"C:\合成截图项目\缓存服务", r"C:\合成截图项目\订单服务"]
        (Path(isolated) / "config.json").write_text(json.dumps({"llm": None, "project_roots": projects,
                                                              "session_ingest": False}), encoding="utf-8")
        from bagu_trainer import engine
        from bagu_trainer import login_startup
        login_startup.shortcut_directory = lambda: Path(isolated) / "startup"
        from bagu_trainer.store import Store
        from bagu_trainer.scheduler import next_state
        from bagu_trainer.desktop import DesktopApp
        from bagu_trainer.tk_runtime import prepare_tk
        prepare_tk()
        import tkinter as tk
        import ttkbootstrap
        from PIL import ImageGrab
        if args.tk_scale is not None:
            original_tk = tk.Tk
            def scaled_root():
                root = original_tk()
                root.tk.call("tk", "scaling", args.tk_scale * 96 / 72)
                return root
            tk.Tk = scaled_root
        store = Store(data / "synthetic-history.db")
        engine._store = store
        timestamp = datetime(2026, 10, 22, 9, tzinfo=timezone(timedelta(hours=8))).timestamp()
        engine._now = lambda: timestamp + 3600
        snapshot = {"kp_id": question["id"], "kp_name": question["knowledge_point"], "generated_by": "llm",
                    "question": question["question"], "options": {"A": "缓存空值并设置合理过期时间", "B": "删除所有缓存",
                    "C": "持续重复查询数据库", "D": "关闭数据库服务"}, "answer": "A",
                    "analysis": "合成截图解析：缓存空值可以减少重复查询，合理的过期时间便于后续数据更新。"}
        for number in range(35):
            identifier = f"synthetic-history-{number}"
            stamp = timestamp - number * 60
            correct = number % 3 == 0
            store.create_quiz(identifier, question["id"], snapshot, "answered", projects[number % 2],
                              "合成截图样本，非真实模型生成", stamp - 5, stamp + 3600)
            store.record_attempt(identifier, question["id"], "A" if correct else "B", "A" if correct else "B",
                                 correct, stamp, next_state(correct, None, stamp))
        app = DesktopApp(SimpleNamespace(engine=engine, paused=False, ui_state={}))
        report = {"data_source": "35 synthetic attempts in temporary SQLite; real engine history/statistics reads",
                  "monitor": "not started; watching label is an explicitly injected display fixture",
                  "credentials_and_model": "not accessed", "user_data": "temporary directory only",
                  "clock": "2026-10-22 10:00:00 +08:00 (synthetic statistics clock)",
                  "mode": "Tk simulation" if args.tk_scale else "native Windows",
                  "system_dpi": ctypes.windll.user32.GetDpiForSystem(),
                  "requested_tk_scale": args.tk_scale, "captures": [], "errors": []}
        app.root.report_callback_exception = lambda *error: report["errors"].append(str(error[1]))

        def wait_for(condition):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                app.root.update()
                if condition():
                    return
                time.sleep(.02)
            raise RuntimeError("Synthetic capture timed out")

        def capture(name):
            app.root.update()
            hwnd = int(app.root.tk.call("wm", "frame", app.root._w), 16)
            ImageGrab.grab(window=hwnd).save(output / f"{name}.png")
            report["captures"].append({"name": name, "page": app.shell.current_page,
                                       "geometry": app.root.geometry(), "tk_scale": app.theme.scale})

        try:
            app._render_dashboard({"state": "watching", "projects": projects, "stats": engine.get_learning_stats()})
            app.status.set("隔离截图演示：统计来自合成学习记录；未启动真实监听或模型请求。")
            app.theme.window(app.root, (1200, 800), (560, 560))
            capture("dashboard-wide")
            # Wide list+detail splits for history and projects.
            app._navigate_page("history")
            wait_for(lambda: len(app.history_page.tree.get_children()) == 20)
            page = app.history_page
            page.open_selected()
            wait_for(lambda: hasattr(page, "detail_view"))
            capture("history-wide")
            app._navigate_page("projects")
            app._cached_pages["projects"].listbox.selection_set(0)
            app._cached_pages["projects"]._sync_detail()
            capture("projects-wide")
            app._navigate_page("bank")
            capture("bank-wide")
            app._navigate_page("model")
            capture("model-wide")
            app._navigate_page("settings")
            capture("settings-wide")
            # Practice with synthetic pending list + current question.
            app.practice.deliver({"quiz_id": "shot", "knowledge_point": "缓存穿透", "project_root": projects[0],
                                  "question": "合成截图样例：缓存未命中时如何避免重复查询数据库？",
                                  "options": {"A": "缓存空值并设置过期", "B": "删除全部缓存", "C": "持续重复查询", "D": "关闭数据库"}})
            app._navigate_page("practice")
            app._pending_items = [
                {"quiz_id": "shot", "knowledge_point": "缓存穿透", "question": "合成截图样例：缓存未命中时如何避免重复查询数据库？"},
                {"quiz_id": "shot2", "knowledge_point": "缓存一致性", "question": "合成截图样例：写操作后如何处理旧缓存？", "manual_review": True},
            ]
            app._due_reviews = 4
            app._refresh_practice()
            capture("practice-wide")
            # Narrow: single column and a collapsed icon rail.
            app.theme.window(app.root, (560, 560), (560, 560))
            app._navigate_page("history")
            capture("history-narrow-detail")
            page.back()
            capture("history-narrow-list")
            app._navigate_page("practice")
            capture("practice-narrow")
            app.shell.set_collapsed(True)
            app.root.update()
            capture("collapsed-rail")
            app.shell.set_collapsed(False)
        finally:
            app.root.destroy()
            engine._store = None
            store.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "captures": len(report["captures"]),
                          "errors": report["errors"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
