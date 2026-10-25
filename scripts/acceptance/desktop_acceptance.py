"""Standalone acceptance driver; never included in the production portable ZIP.

Can be frozen separately so a clean Windows guest needs no installed Python/Git.
Uses real Tk, engine, watcher, scorer and SQLite; only HTTP source/model fixtures
and an optional CI credential store are injected through existing internal APIs.
"""
import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.acceptance.contract import complete_milestones
from scripts.portable_artifacts import resolve_target, validate_target

KEY = "acceptance-local-only-not-a-secret"
REPOSITORY = "Snailclimb/JavaGuide"
TEXT = ("# 什么是数据库索引？\n\n数据库索引用额外的数据结构记录键值与数据位置的对应关系。"
        "查询可以利用索引缩小扫描范围。维护索引需要存储空间，插入和更新数据也可能增加"
        "索引维护成本，因此应结合查询条件选择索引。\n")
# Multiple unrelated concepts give the unchanged BM25 evidence gate a realistic
# corpus. A one-question fixture has near-zero IDF and should not force a match.
DISTRACTORS = [
    ("栈结构如何保持后进先出顺序？", "栈只允许在同一端插入和删除元素。新元素压入栈顶，弹出操作取出栈顶元素，因此最后加入的元素最先离开。调用栈可以利用这个顺序保存返回位置。"),
    ("队列如何保证先进先出顺序？", "队列从尾部加入元素，并从头部取出元素。入队和出队分别作用于不同端，能够保留元素到达的先后顺序。循环队列可以使用两个位置表示读端和写端。"),
    ("哈希表如何解决哈希碰撞？", "不同键可能映射到同一位置，这种情况称为哈希碰撞。链地址法在每个位置保存一组元素，开放寻址则寻找其他可用位置。装载因子会影响碰撞频率。"),
    ("二分查找为什么要求有序数组？", "二分查找通过比较中间元素排除一半区间。有序性保证目标只可能处于其中一边。每轮缩小区间直到发现目标或区间为空，边界条件决定是否包含中间位置。"),
    ("互斥锁如何保护临界区？", "互斥锁确保同一时刻只有一个执行线程进入受保护区域。线程进入前获取锁，离开时释放锁，其他竞争者等待可用。所有相关访问必须遵守同一个锁协议。"),
    ("TCP 三次握手为什么需要三个报文？", "连接建立时双方交换同步报文并确认对方序列号。客户端发送同步请求，服务端回复同步和确认，客户端再确认服务端报文，以便双方建立连接状态。"),
    ("对象引用计数如何处理循环引用？", "引用计数记录对象被引用的数量，计数归零时对象可以释放。循环引用中的对象可能相互维持计数，需要额外的循环检测或其他垃圾回收策略协助处理。"),
    ("递归算法为什么需要终止条件？", "递归把一个问题转换为规模更小的同类问题。终止条件处理最简单的情况，阻止调用无限继续。每次调用必须向终止条件前进，并正确合并较小问题的结果。"),
    ("异常处理如何保证资源释放？", "程序在遇到错误时可能提前离开正常控制流。将释放逻辑放入清理块或上下文管理器，可以让正常返回和异常离开都执行关闭动作，避免资源一直被占用。"),
]
CHANGE = ('# 数据库索引 查询优化 扫描范围 索引维护\n'
          'query = "SELECT id FROM orders WHERE customer_id = 100"\n'
          'index = "CREATE INDEX customer_lookup ON orders(customer_id)"\n')
QUESTION = {"question": "数据库查询需要减少扫描的数据量，应当如何结合查询条件优化索引？",
            "options": {"A": "结合查询条件建立合适索引", "B": "删除所有索引", "C": "总是全表扫描", "D": "只改变量名"},
            "answer": "A", "analysis": "本地模拟解析：索引缩小扫描范围，同时增加存储与维护成本。",
            "grounding": {"context_quote": "数据库索引 查询优化 扫描范围 索引维护",
                "source_quote": "数据库索引用额外的数据结构记录键值与数据位置的对应关系",
                "rationale": "订单查询改动涉及索引与扫描范围，适合考查依据查询条件建立索引的取舍。"}}
REVIEW = {"context_relevant": True, "source_supported": True, "answer_unique": True,
          "no_answer_leak": True, "correct_answer": "A", "reason": "索引场景、引用依据和唯一答案均由本地固定验收响应确认。"}
UPDATED_MODEL = "updated-local-model"


def capture_window(work, name, *, root=None, pid=None, expected_title="AI 八股训练器"):
    """Owned windows only. A capture is evidence, never an automatic visual pass."""
    record = {"name": name, "captured": False}
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import ImageGrab
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        if root is not None:
            root.update_idletasks()
            root.update()  # process Configure/paint events after an explicit scroll
            hwnd = int(root.tk.call("wm", "frame", root._w), 16)
        else:
            owned = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            @callback_type
            def collect(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value == expected_title:
                        owned.append(hwnd)
                return True
            # The startup broker can report running just before the native
            # window is mapped; wait for that owned window's first display.
            deadline = time.monotonic() + 3
            while not owned and time.monotonic() < deadline:
                user32.EnumWindows(collect, 0)
                if not owned:
                    time.sleep(0.1)
            check(owned, "No visible main window in owned production process")
            hwnd = owned[0]
        image = ImageGrab.grab(window=hwnd)
        path = work / "screenshots" / (name + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        record.update(captured=True, path=str(path.relative_to(work)), size=list(image.size))
        user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
        record["window_dpi"] = user32.GetDpiForWindow(hwnd)
        record["windows_primary_scale_percent"] = ctypes.windll.shcore.GetScaleFactorForDevice(0)
    except Exception as error:
        record["error"] = type(error).__name__ + ": " + str(error)
    return record


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def check(value, message):
    if not value:
        raise AssertionError(message)


def database_state(path):
    connection = sqlite3.connect(path)
    try:
        return {"attempts": connection.execute("SELECT count(*) FROM attempts").fetchone()[0],
                "answered": connection.execute("SELECT count(*) FROM quizzes WHERE status='answered'").fetchone()[0],
                "concepts": [list(row) for row in connection.execute("SELECT kp_id,attempts,mastery,next_review_at FROM concept_state ORDER BY kp_id")]}
    finally:
        connection.close()


def phase(options):
    # Imports happen only after the orchestrator has selected an isolated profile.
    from bagu_trainer import config, desktop, first_use, secrets, startup
    from bagu_trainer.bank_build.service import BankBuildService
    from bagu_trainer.bank_storage import active_bank_dir
    if options.memory_credentials:
        class Credentials:
            def get_password(self, *_):
                return KEY
            def set_password(self, *_):
                pass
        secrets._load_keyring = lambda: Credentials()
    work = options.work
    from bagu_trainer import login_startup
    login_startup.shortcut_directory = lambda: work / "临时 Startup"
    profile = config.USER_DATA_ROOT
    watched = work / "监听 项目"
    errors, milestones, screenshots, ui_pages = [], [], [], {}
    def capture(name, root):
        screenshots.append(capture_window(work, options.phase + "-" + name, root=root))
    old_dashboard = desktop.DesktopApp.__init__
    bank_service = BankBuildService(manifest=work / "fixture-sources.json",
        opener=lambda request, timeout: urllib.request.urlopen(options.base_url + "/bank.zip", timeout=timeout))
    def schedule(root, callback, cleanup):
        deadline = time.monotonic() + 95
        def tick():
            try:
                check(time.monotonic() < deadline, "UI phase timed out; milestones=" + repr(milestones))
                if callback():
                    return
            except Exception as error:
                errors.append(type(error).__name__ + ": " + str(error))
                write_json(work / "failure.json", {"errors": errors, "milestones": milestones})
                cleanup()
                return
            root.after(40, tick)
        root.after(80, tick)
    def dashboard_init(app, monitor, *args, **kwargs):
        old_dashboard(app, monitor, *args, **kwargs)
        stage = [0]
        first_quiz = [None]
        review_retried = [False]
        settings = [None]
        previous_receipt = [None]
        entered_pending = [False]
        def cleanup():
            if settings[0] is not None and not settings[0].closed:
                settings[0]._close()
            from tkinter import messagebox
            previous = messagebox.askyesno
            try:
                messagebox.askyesno = lambda *args, **kwargs: True
                app._close()
            finally:
                messagebox.askyesno = previous
        setup_stage = [0 if options.phase == "first" else 9]
        def prepare_inline():
            if setup_stage[0] >= 9:
                return False
            bank = app._page_frame("bank")
            model = app._page_frame("model")
            if app.tasks.bank.busy or model.busy:
                return True
            n = setup_stage[0]
            if n == 0:
                check("bagu_trainer.engine" not in sys.modules, "Engine loaded before readiness")
                app._navigate_page("bank")
                capture("main-bank-empty", app.root)
                bank._start()
                check("选择" in bank.status.get(), "Missing selection guidance")
                bank.source_list.selection_set(REPOSITORY)
                bank._start()
            elif n == 1:
                check("503" in bank.status.get(), "Missing download failure guidance")
                check(not (config.data_dir() / "active-bank.json").exists(), "Failed build became active")
                milestones.append("download_failure_and_automatic_retries")
                bank._start()
            elif n == 2:
                check((active_bank_dir() / "question_concepts.json").is_file(), "Concept build missing")
                milestones.append("bank_download_extract_import_tag_concepts")
                provenance = json.loads((active_bank_dir() / "sources.json").read_text(encoding="utf-8"))
                aliases = provenance["sources"][0]["path_aliases"]
                check(set(aliases) == {"docs/Async.md", "docs/async.md"}, "Case aliases missing")
                for stored in aliases.values():
                    check((active_bank_dir() / "repos" / "JavaGuide" / stored).read_text(encoding="utf-8") == TEXT,
                          "Case-distinct source content lost")
                milestones.append("bank_case_distinct_paths_preserved")
                app._navigate_page("model")
                model.base_url.set("bad-url")
                model.model_name.set("local-model")
                model.api_key.set(KEY)
                model._test()
            elif n == 3:
                check(model.verified is None and "地址" in model.status.get(), "Invalid URL guidance missing")
                model.base_url.set(options.base_url + "/v1")
                model.model_name.set("missing-model")
                model._test()
            elif n == 4:
                check(model.verified is None and "404" in model.status.get(), "Invalid model guidance missing")
                milestones.append("invalid_config_and_model_404_recovery")
                model.model_name.set("local-model")
                model._test()
            elif n == 5:
                check(model.verified is not None, "Valid local model did not pass test")
                capture("main-model-verified", app.root)
                model._save()
            elif n == 6:
                check(not model.api_key.get(), "Model key was not cleared after save")
                check(KEY not in config.CONFIG_PATH.read_text(encoding="utf-8"), "Plaintext key in configuration")
                milestones.append("model_explicit_test_and_secure_save")
                app._navigate_page("projects")
                projects = app._page_frame("projects")
                projects.projects = [str(watched)]
                projects._render()
                projects._mark_dirty()
                projects._save()
                milestones.append("project_selection")
                capture("main-projects", app.root)
            elif n == 7:
                if not app.ready:
                    return True
                app._navigate_page("dashboard")
            setup_stage[0] += 1
            return True
        def step():
            if prepare_inline():
                return False
            engine = monitor.engine
            if options.phase == "restart":
                if not monitor.initialized:
                    return False
                check(app.practice.current is None, "Restart automatically repeated answered quiz")
                state = database_state(config.db_path())
                expected = json.loads((work / "first-state.json").read_text(encoding="utf-8"))
                check(state == expected, "Restart/upgrade lost learning data")
                check(all(Path(path).exists() for path in config.load_config()["project_roots"]), "Projects lost")
                check(config.load_config()["llm"]["model"] == UPDATED_MODEL, "Updated model lost on restart")
                from bagu_trainer import onboarding
                check(onboarding.saved_model_verified(), "Saved settings verification receipt lost on restart")
                milestones.append("restart_direct_main_and_history_restore")
                milestones.append("updated_model_receipt_restored_without_network")
                capture("main", app.root)
                cleanup()
                return True
            if stage[0] == 0:
                if not monitor.initialized:
                    return False
                milestones.append("watcher_baseline")
                baselines = (set(monitor.initialized), dict(monitor.observed), dict(engine.session._diff_readers))
                for key, _ in app.shell.PAGES:
                    app._navigate_page(key)
                    app.root.update_idletasks()
                    frame = app.shell.current_frame
                    check(app.shell.current_page == key and frame.master is app.content_host,
                          "Page is not embedded: " + key)
                    image_name = app.shell.buttons[key].cget("image")
                    check(bool(image_name), "Missing frozen navigation icon: " + key)
                    if key == "bank":
                        check(bool(frame.sources), "Frozen bank page has no source manifest")
                    ui_pages[key] = {"class": type(frame).__name__, "embedded": True, "icon": True}
                    capture("page-" + key, app.root)
                check(baselines == (set(monitor.initialized), dict(monitor.observed), dict(engine.session._diff_readers)),
                      "Ordinary navigation rebuilt baselines")
                milestones.append("seven_embedded_pages_icons_and_navigation_keep_baselines")
                app._navigate_page("model")
                check(app.shell.current_page == "model" and monitor.delivery_paused.is_set(),
                      "Acceptance did not enter model browsing before generation")
                capture("main-watching", app.root)
                (watched / "query.py").write_text(CHANGE, encoding="utf-8")
                stage[0] = 1
            elif stage[0] == 1:
                if not entered_pending[0]:
                    check(app.shell.current_page == "model" and monitor.delivery_paused.is_set(),
                          "Browsing changed before explicit pending entry: " + app.shell.current_page)
                    check(app.practice.current is None, "Quiz delivered while model page is being browsed")
                if app.practice.current is None:
                    if app._pending_items and not entered_pending[0]:
                        check(app.shell.current_page == "model", "New quiz stole the current page")
                        milestones.append("new_quiz_updates_pending_without_stealing_page")
                        entered_pending[0] = True
                        app._navigate_page("practice")
                        app._enter_pending(app._pending_items[0]["quiz_id"])
                        return False
                    if app.generation_retryable and not app.generation_busy and not review_retried[0]:
                        check(app.retry_button.instate(["!disabled"]), "Rejected review cannot retry")
                        check(not engine.session._state["pending"], "Rejected candidate entered pending")
                        check(engine.session._store._conn.execute("SELECT count(*) FROM quizzes").fetchone()[0] == 0,
                              "Rejected candidate was persisted as a quiz")
                        milestones.append("rejected_review_blocks_popup_then_explicit_retry")
                        review_retried[0] = True
                        app.retry_button.invoke()
                    return False
                app._navigate_page("practice")
                practice = app._page_frame("practice")
                check(practice.submit_button.instate(["!disabled"]), "Practice cannot be answered")
                check(review_retried[0], "Rejected first review was not exercised")
                check(engine.session._state["quizzes"][monitor.active]["grounding"]["review"]["context_relevant"],
                      "Displayed quiz has no approved review")
                capture("quiz", app.root)
                first_quiz[0] = monitor.active
                app.answer.set("B")
                assert app.practice.submit()
                milestones.append("real_file_change_debounce_embedded_practice")
                stage[0] = 2
            elif stage[0] == 2:
                if "已记录作答" not in app.feedback.get():
                    return False
                check("回答错误" in app.feedback.get() and "正确答案：A" in app.feedback.get(), "Wrong grade/analysis")
                assert not app.practice.submit()  # finished button prevents duplicate scoring.
                state = database_state(config.db_path())
                check(state["attempts"] == 1 and state["answered"] == 1, "Answer not persisted exactly once")
                check(state["concepts"][0][1] == 1 and state["concepts"][0][2] == 0, "Concept state missing")
                write_json(work / "first-state.json", state)
                milestones.append("grade_analysis_and_sqlite_persistence")
                practice = app._page_frame("practice")
                app.root.update_idletasks()
                practice.view._key_position(1)  # same explicit End action as a reader
                capture("grade-and-review", app.root)
                app.practice.dismiss()
                from bagu_trainer import onboarding
                from bagu_trainer.settings_window import ModelSettingsWindow
                previous_receipt[0] = onboarding.read_state()["model_verification"]
                settings[0] = ModelSettingsWindow(app.root)
                settings[0].model_var.set(UPDATED_MODEL)
                check(settings[0].save_button.instate(["disabled"]), "Edited model can save before testing")
                settings[0]._save()
                check(config.load_config()["llm"]["model"] == "local-model", "Untested edit was persisted")
                capture("settings-edited-save-disabled", settings[0].root)
                settings[0]._test()
                stage[0] = 3
            elif stage[0] == 3:
                window = settings[0]
                if window.busy:
                    return False
                check(window.verified is not None and window.save_button.instate(["!disabled"]), "Edited model did not pass explicit test")
                capture("settings-tested", window.root)
                window._save()
                stage[0] = 4
            elif stage[0] == 4:
                window = settings[0]
                if window.busy or app.pending_actions:
                    return False
                from bagu_trainer import onboarding
                check(config.load_config()["llm"]["model"] == UPDATED_MODEL, "Changed model was not saved")
                check(onboarding.saved_model_verified(), "Updated configuration receipt is not valid")
                check(onboarding.read_state()["model_verification"] != previous_receipt[0], "Model edit kept old receipt")
                check(not window.key_var.get(), "Key input not cleared after save")
                check(database_state(config.db_path()) == json.loads((work / "first-state.json").read_text(encoding="utf-8")), "Model save changed learning history")
                milestones.append("settings_edit_retest_save_and_updated_receipt")
                capture("settings-saved", window.root)
                window._close()
                settings[0] = None
                app._navigate_page("history")
                stage[0] = 5
            elif stage[0] == 5:
                page = app.history_page
                if page is None or page.total != 1:
                    return False
                check(len(page.tree.get_children()) == 1, "History did not show the persisted attempt")
                page.open_selected()
                stage[0] = 6
            elif stage[0] == 6:
                page = app.history_page
                if not hasattr(page, "retry_button"):
                    return False
                capture("history-snapshot", app.root)
                from bagu_trainer.ui_copy import question_text, solution_text
                import tkinter as tk
                try:
                    clipboard_before = app.root.clipboard_get()
                except tk.TclError:
                    clipboard_before = None
                try:
                    page.copy_question_button.invoke()
                    check(app.root.clipboard_get() == question_text(QUESTION["question"], QUESTION["options"]),
                          "Frozen history question copy mismatch")
                    page.copy_solution_button.invoke()
                    check(app.root.clipboard_get() == solution_text(QUESTION["answer"], QUESTION["analysis"]),
                          "Frozen history answer/analysis copy mismatch")
                finally:
                    app.root.clipboard_clear()
                    if clipboard_before is not None:
                        app.root.clipboard_append(clipboard_before)
                check(page.retry_button.instate(["!disabled"]), "AI snapshot cannot be repracticed")
                page.retry()
                stage[0] = 7
            elif stage[0] == 7:
                if not app.practice.current or not app.practice.current.get("manual_review"):
                    return False
                check(app.shell.current_page == "practice", "Repractice did not enter embedded practice")
                retry = engine.session._store.get_quiz(app.practice.quiz_id)
                check(retry["snapshot"]["retry_of_quiz_id"] == first_quiz[0], "Repractice source association lost")
                app.practice.choose("A")
                check(app.practice.submit(), "Repractice cannot submit")
                check(not app.practice.submit(), "Duplicate repractice submit accepted")
                stage[0] = 8
            elif stage[0] == 8:
                if app.practice.submit_state != "finished":
                    return False
                check(engine.session._store.attempt_count(first_quiz[0]) == 1, "Original answer counted twice")
                check(engine.session._store.attempt_count(app.practice.quiz_id) == 1, "Repractice counted twice")
                state = database_state(config.db_path())
                check(state["attempts"] == 2 and state["answered"] == 2, "History/repractice persistence missing")
                write_json(work / "first-state.json", state)
                milestones.append("history_snapshot_repractice_and_idempotent_second_answer")
                capture("repractice-grade", app.root)
                app.practice.dismiss()
                cleanup()
                return True
            return False
        schedule(app.root, step, cleanup)
    from bagu_trainer.bank_panel import BankPanel
    original_bank_panel = BankPanel.__init__
    def fixture_bank_panel(self, *args, **kwargs):
        kwargs["service"] = bank_service
        original_bank_panel(self, *args, **kwargs)
    BankPanel.__init__ = fixture_bank_panel
    desktop.DesktopApp.__init__ = dashboard_init
    try:
        desktop.main([])
    finally:
        if "bagu_trainer.engine" in sys.modules:
            engine = sys.modules["bagu_trainer.engine"]
            if engine.session._store:
                engine.session._store.close()
    check(not errors, "; ".join(errors))
    check(milestones, "No acceptance milestones completed")
    check(complete_milestones(options.phase, milestones), "Acceptance skipped a required milestone: " + repr(milestones))
    write_json(work / (options.phase + "-result.json"), {"passed": True, "milestones": milestones,
               "screenshots": screenshots, "ui_pages": ui_pages,
               "frozen": bool(getattr(sys, "frozen", False)), "credential_backend": "memory fixture" if options.memory_credentials else "Windows vault, isolated profile"})


def orchestrate(options):
    work = options.work
    # Gate all production inputs BEFORE GUI children, HTTP mocks or credentials.
    artifact = None
    if options.production_exe or options.output_dir or options.archive or getattr(sys, "frozen", False):
        if options.production_exe and options.production_exe.name != "BaguTrainer.exe":
            raise ValueError("--production-exe must name BaguTrainer.exe")
        target = resolve_target(options.output_dir,
                                bundle=options.production_exe.parent if options.production_exe else None,
                                archive=options.archive)
        expected = (json.loads((Path(sys._MEIPASS) / "acceptance-provenance.json").read_text(encoding="utf-8"))
                    if getattr(sys, "frozen", False) else None)
        artifact = validate_target(target, expected_source=expected,
                                   driver=Path(sys.executable) if getattr(sys, "frozen", False) else None)
        options.production_exe = target.bundle / "BaguTrainer.exe"
    work.mkdir(parents=True, exist_ok=False)
    if artifact is not None:
        # Keep target identity even when a later GUI milestone fails.
        write_json(work / "ARTIFACT.json", artifact)
    watched = work / "监听 项目"
    watched.mkdir()
    (watched / "query.py").write_text('query = "SELECT id FROM orders"\n', encoding="utf-8")
    write_json(work / "fixture-sources.json", {"sources": [{"repository": REPOSITORY, "directory": "JavaGuide",
        "audited_commit": "a" * 40, "observed_license": "synthetic self-authored test fixture"}]})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("snapshot/README.md", TEXT + "\n" + "\n\n".join("# " + title + "\n\n" + body for title, body in DISTRACTORS))
        output.writestr("snapshot/docs/Async.md", TEXT)
        output.writestr("snapshot/docs/async.md", TEXT)
    counts = {"downloads": 0, "probes": 0, "quizzes": 0, "reviews": 0, "rejected": 0}
    models = []
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path != "/bank.zip":
                self.send(404, {})
                return
            counts["downloads"] += 1
            self.send(503, {}) if counts["downloads"] <= 3 else self.send(200, archive.getvalue())
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions" or request.get("model") not in {"local-model", UPDATED_MODEL}:
                counts["rejected"] += 1
                self.send(404, {})
                return
            if self.headers.get("Authorization") != "Bearer " + KEY:
                self.send(401, {})
                return
            is_probe = "max_tokens" in request
            is_review = "review_context_grounding" in request["messages"][-1]["content"]
            kind = "probe" if is_probe else "review" if is_review else "quiz"
            models.append({"model": request["model"], "kind": kind})
            counts[{"probe": "probes", "quiz": "quizzes", "review": "reviews"}[kind]] += 1
            if is_review:
                # The first audit rejects the candidate. Only an explicit desktop retry can recover.
                content = json.dumps({**REVIEW, "context_relevant": counts["reviews"] > 1}, ensure_ascii=False)
            else:
                content = "OK" if is_probe else json.dumps(QUESTION, ensure_ascii=False)
            self.send(200, {"choices": [{"message": {"content": content}}]})
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    environment = dict(os.environ, BAGU_USER_DATA_DIR=str(work / "用户 数据"), PATH="",
                       BAGU_TEST_INSTANCE="desktop-acceptance-" + hashlib.sha256(str(work).encode("utf-8")).hexdigest()[:16],
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
    for variable in ("BAGU_LLM_API_KEY", "PYTHON_KEYRING_BACKEND", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(variable, None)
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-B", str(Path(__file__).resolve())]
    results = {"date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "os": platform.platform(),
               "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
               "path_empty_in_app": True, "mcp_configured": False,
               "clean_machine_claim": False, "mock_network": "localhost HTTP, self-authored ZIP and fixed model responses"}
    if artifact is not None:
        results["artifact"] = artifact
    if getattr(sys, "frozen", False):
        results.update(json.loads((Path(sys._MEIPASS) / "acceptance-provenance.json").read_text(encoding="utf-8")))
    try:
        for name in ("first", "restart"):
            args = [*command, "--work", str(work), "--phase", name, "--base-url", base]
            if options.memory_credentials:
                args.append("--memory-credentials")
            result = subprocess.run(args, env=environment, capture_output=True, timeout=120)
            check(result.returncode == 0, f"{name} child failed: " + result.stderr.decode("utf-8", errors="replace")[-3000:])
            results[name] = json.loads((work / (name + "-result.json")).read_text(encoding="utf-8"))
            if name == "first":
                bank_hashes = {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (work / "用户 数据" / "data" / "banks").rglob("*") if p.is_file()}
        expected_counts = {"downloads": 4, "probes": 2, "quizzes": 2, "reviews": 2, "rejected": 1}
        check(counts == expected_counts, "Unexpected network requests or restart requested model")
        check(models == [{"model": "local-model", "kind": "probe"}, {"model": "local-model", "kind": "quiz"},
                         {"model": "local-model", "kind": "review"}, {"model": "local-model", "kind": "quiz"},
                         {"model": "local-model", "kind": "review"},
                         {"model": UPDATED_MODEL, "kind": "probe"}], "Model edit was not explicitly tested")
        check(all(hashlib.sha256((work / p).read_bytes()).hexdigest() == digest for p, digest in bank_hashes.items()), "Bank changed on restart")
        check(KEY not in (work / "用户 数据" / "config.json").read_text(encoding="utf-8"), "Plaintext fixture key in config")
        if options.production_exe:
            from bagu_trainer import desktop_instance
            desktop_instance.RUNTIME = work / "用户 数据" / "runtime"
            production_manifest = json.loads((options.production_exe.parent / "BUILD_MANIFEST.json").read_text(encoding="utf-8"))
            results["production_commit"] = production_manifest.get("source_commit")
            results["production_dirty"] = production_manifest.get("source_dirty")
            results["production_source_tree"] = production_manifest.get("source_tree", {}).get("sha256")
            if results.get("source_tree"):
                check(results["source_commit"] == results["production_commit"],
                      "Frozen driver and production commits differ")
                check(results["source_dirty"] == results["production_dirty"],
                      "Frozen driver and production dirty states differ")
                check(results["source_tree"]["sha256"] == results["production_source_tree"],
                      "Frozen driver and production code fingerprints differ")
            prod_env = dict(environment)
            if options.memory_credentials:
                # Matches the verified receipt without touching a CI runner's vault.
                prod_env["BAGU_LLM_API_KEY"] = KEY
            upgraded = work / "升级 后程序"
            shutil.copytree(options.production_exe.parent, upgraded)
            def smoke(exe, label, background=False):
                child = subprocess.Popen([str(exe), *(["--background"] if background else [])], env=prod_env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                activated = not background
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        check(child.poll() is None, "Production EXE unexpectedly exited")
                        try:
                            status = desktop_instance.remote_call("_desktop_status", {})
                            if background and not activated and status.get("window_state") == "iconic":
                                credentials = work / "用户 数据" / "runtime" / "bridge-credentials.json"
                                original_credentials = credentials.read_bytes()
                                activation = subprocess.run([str(exe)], env=prod_env, capture_output=True, timeout=20)
                                check(activation.returncode == 0 and credentials.read_bytes() == original_credentials,
                                      "Second instance replaced the existing owner")
                                activated = True
                                results["background_singleton_activation"] = True
                            # The first UI poll proves mainloop has processed
                            # the actual dashboard, beyond constructor/startup.
                            if activated and status.get("startup_state") == "running" and status.get("window_state") == "normal":
                                results.setdefault("production_screenshots", []).append(capture_window(work, label, pid=child.pid))
                                check(json.loads((work / "用户 数据" / "config.json").read_text(encoding="utf-8"))["llm"]["model"] == UPDATED_MODEL,
                                      "Production EXE lost updated settings")
                                return
                        except (OSError, ValueError):
                            pass
                        time.sleep(0.1)
                    raise AssertionError("Production EXE did not enter main window")
                finally:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=10)
            smoke(options.production_exe, "production-main-after-model-save")
            check(database_state(work / "用户 数据" / "data" / "trainer.db") == json.loads((work / "first-state.json").read_text(encoding="utf-8")), "Production EXE lost learning data")
            smoke(upgraded / options.production_exe.name, "production-upgraded-main")
            check(database_state(work / "用户 数据" / "data" / "trainer.db") == json.loads((work / "first-state.json").read_text(encoding="utf-8")), "Upgrade lost learning data")
            smoke(options.production_exe, "production-background-activated", background=True)
            for arguments in (("--check-runtime",), ("--check-package", str(work / "production-package.json"))):
                result = subprocess.run([str(options.production_exe), *arguments], env=prod_env, capture_output=True, timeout=30)
                check(result.returncode == 0, "Frozen command failed: " + arguments[0])
            package = json.loads((work / "production-package.json").read_text(encoding="utf-8"))
            check(package["frozen"] and package["ui_theme"] == "bagu-light", "Frozen theme/runtime check failed")
            check(set(package["ui_diagnostics"]["navigation_icons"]) ==
                  {"dashboard", "practice", "history", "projects", "bank", "model", "settings"},
                  "Frozen navigation icon resources missing")
            results["production_commands"] = {"check-runtime": True, "check-package": True,
                                               "theme": package["ui_theme"], "navigation_icons": 7}
            results["production_exe"] = {"first_start": "running", "upgraded_start": "running", "history_retained": True}
            results["updated_model_production_restart"] = {"model": UPDATED_MODEL, "direct_main": True,
                                                          "no_new_model_requests": counts == expected_counts}
        check(counts == expected_counts, "Production restart unexpectedly sent model requests")
        # Independent profile, same two processes in source and frozen runs.
        # No acceptance hook is added to the production executable.
        lifecycle = work / "bank-lifecycle"
        lifecycle_env = dict(environment, BAGU_USER_DATA_DIR=str(lifecycle / "user"),
                             PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
        for name in ("prepare", "resume"):
            child = subprocess.run([*command, "--work", str(lifecycle), "--phase", "bank-" + name],
                                   env=lifecycle_env, capture_output=True, timeout=90)
            error_file = lifecycle / "ERROR.json"
            check(child.returncode == 0, "Bank lifecycle failed: " +
                  (error_file.read_text(encoding="utf-8") if error_file.exists() else
                   child.stderr.decode("utf-8", errors="replace")[-3000:]))
            results["bank_" + name] = json.loads((lifecycle / (name + "-result.json")).read_text(encoding="utf-8"))
        results["requests"] = counts
        results["model_requests"] = models
        results["passed"] = True
        write_json(work / "ACCEPTANCE.json", results)
        print(json.dumps(results, ensure_ascii=True))
    finally:
        server.shutdown()
        server.server_close()
        if not options.memory_credentials:
            # Delete only this driver's isolated credential, never the default service.
            from bagu_trainer import secrets
            profile = os.path.normcase(str((work / "用户 数据").resolve()))
            service = "bagu-trainer.profile." + hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
            keyring = secrets._load_keyring()
            if keyring is not None:
                try:
                    keyring.delete_password(service, secrets.KEYRING_USER)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--phase", choices=["first", "restart", "bank-prepare", "bank-resume"])
    parser.add_argument("--base-url")
    parser.add_argument("--memory-credentials", action="store_true")
    parser.add_argument("--production-exe", type=lambda p: Path(p).resolve())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--archive", type=Path)
    options = parser.parse_args()
    if options.phase and options.phase.startswith("bank-"):
        from scripts.acceptance.bank_lifecycle import run
        run(options.work, options.phase.removeprefix("bank-"))
    elif options.phase:
        phase(options)
    else:
        orchestrate(options)


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    # Existing work/evidence belongs to a previous run; even ERROR.json must not
    # overwrite it. Phase children intentionally share their parent's new work.
    if "--phase" not in sys.argv and "--work" in sys.argv:
        existing = Path(sys.argv[sys.argv.index("--work") + 1])
        if existing.exists():
            if sys.stderr is not None:
                print("Work directory already exists; choose a new --work: " + str(existing), file=sys.stderr)
            raise SystemExit(1)
    try:
        main()
    except Exception as error:
        # A windowed acceptance EXE must fail with an exit code/report instead of
        # showing a modal crash dialog that would block an unattended run.
        if "--work" in sys.argv:
            location = Path(sys.argv[sys.argv.index("--work") + 1])
            write_json(location / "ERROR.json", {"error": type(error).__name__, "message": str(error)})
        if sys.stderr is not None:
            import traceback
            traceback.print_exc()
        raise SystemExit(1)
