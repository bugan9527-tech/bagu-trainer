"""Windows desktop quiz application. UI stays on the main Tk thread."""
import argparse
import os
import queue
import threading

from .ui_theme import apply_theme, SPACE, PRIMARY, SECTION, MUTED
from .ui_state import cancel_timer, set_text, window_var


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI 八股训练器桌面版")
    parser.add_argument("--background", action="store_true", help="登录时最小化启动")
    parser.add_argument("--check-runtime", action="store_true", help="验证窗口运行环境后退出")
    parser.add_argument("--configure", action="store_true", help="打开模型配置窗口")
    parser.add_argument("--migrate-from", help="从旧版项目目录复制配置、题库和学习记录")
    parser.add_argument("--check-package", metavar="REPORT", help="验证便携运行环境并写入诊断 JSON")
    parser.add_argument("--install-startup", action="store_true", help="启用登录自启动")
    parser.add_argument("--uninstall-startup", action="store_true", help="取消登录自启动")
    parser.add_argument("--startup-directory", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("桌面自启动当前仅支持 Windows")
    if options.check_runtime:
        from .tk_runtime import prepare_tk, create_root
        prepare_tk()
        import tkinter as tk
        root = create_root()
        apply_theme(root)
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return 0
    if options.configure:
        from .settings_window import build_window
        build_window().mainloop()
        return 0
    if options.install_startup or options.uninstall_startup:
        from . import login_startup
        if options.uninstall_startup:
            login_startup.uninstall(options.startup_directory)
        else:
            login_startup.install(options.startup_directory)
        return 0
    if options.check_package and not options.migrate_from:
        from .runtime import package_report
        package_report(options.check_package)
        return 0
    from .desktop_instance import OwnerLock, WindowBroker, remote_call
    owner = OwnerLock()
    if not owner.acquire():
        if not options.background:
            try:
                remote_call("_show_dashboard", {})
            except Exception:
                pass
        return 0
    bridge = None
    try:
        if options.migrate_from:
            from .migration import migrate_legacy
            migrate_legacy(options.migrate_from)
        if options.check_package:
            from .runtime import package_report
            package_report(options.check_package)
            return 0
        # The broker also activates setup while configuration/engine is not ready.
        from .startup import StartupCoordinator
        coordinator = StartupCoordinator()
        bridge = WindowBroker(coordinator.window_command, owner)
        bridge.start()
        coordinator.run(options.background)
    finally:
        if bridge is not None:
            bridge.close()
        else:
            owner.close()
    return 0


class DesktopApp:
    def __init__(self, monitor, background=False, events=None):
        from .tk_runtime import prepare_tk, create_root
        prepare_tk()
        import tkinter as tk
        from tkinter import ttk
        from .ui_layout import ScrollableFrame, MessageArea, FlowActions, wrapped_label
        self.tk, self.ttk = tk, ttk
        self.monitor = monitor
        self.events = events if events is not None else queue.Queue()
        self.monitor.ui_post = self.events.put
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.closed = self.closing = False
        self.worker_thread = None
        self.worker_done = threading.Event()
        self.pending_actions = set()
        self.history_page = None
        self._cached_history = None
        # Application task layer: holds the bank build, test and save tasks so
        # ordinary page switches never cancel or destroy them.
        from .task_controller import TaskController
        self.tasks = TaskController(ui_events=self.events)
        self._bank_reload_pending = False
        self._cached_pages = {}
        self.generation_busy = self.generation_retryable = False
        self.generation_reason = ""
        self.inflight_submissions = set()
        self.pending_answers = {}
        self._pending_items = []
        self._due_reviews = 0
        self.poll = self.focus_timer = None
        self.root = create_root()
        self.theme = apply_theme(self.root)
        px = self.theme.px
        self.root.title("AI 八股训练器")
        self.theme.window(self.root, (1200, 800), (560, 560))
        from . import config
        from .ui_window import WindowPlacement
        self.window_placement = WindowPlacement(self.root, self.theme, config.USER_DATA_ROOT / "window.json")
        self.status = window_var(self.root, "正在建立文件监听基线…")
        self.listen_state = window_var(self.root, "正在准备监听")
        self.global_status = window_var(self.root, "正在准备监听")
        self.project_count = None
        self.projects_text = window_var(self.root, "正在读取监听项目…")
        self.current_project = window_var(self.root, "等待项目就绪")
        self.learning_values = {name: window_var(self.root, "—") for name in ("answered", "accuracy", "reviews", "pending")}
        # Practice delivery is owned by the PracticeController (the single entry
        # point); the old popup/history_deferred/deferred_quiz bookkeeping is
        # gone.  The three Tk variables survive page switches.
        self.answer = window_var(self.root)
        self.feedback = window_var(self.root, "选择一个选项后提交，即可查看解析。")
        self.answer_hint = window_var(self.root, "尚未提交：请选择一个选项。当前没有可取消的提交。")
        from .practice_controller import PracticeController
        self.practice = PracticeController(self.answer, self.feedback, self.answer_hint,
                                           on_submit=self._submit_quiz, on_dismiss=self._dismiss_quiz,
                                           can_submit=self._can_submit_quiz)
        from .ui_shell import AppShell
        self.shell = AppShell(self.root, self.theme, self._navigate_page, self.global_status)
        self.content_host = self.shell.content_host
        panel = ttk.Frame(self.content_host, padding=px(SPACE.lg))
        self.panel = panel
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        wrapped_label(panel, self.theme, text="监听与学习概况", style=MUTED).grid(row=0, column=0, sticky="ew")
        wrapped_label(panel, self.theme, textvariable=self.listen_state, style=SECTION).grid(
            row=1, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        self.dashboard_view = ScrollableFrame(panel, self.theme)
        self.dashboard_view.grid(row=3, column=0, sticky="nsew")
        compact = self.theme.compact
        status_host = self.dashboard_view.body if compact else panel
        self.status_area = MessageArea(status_host, self.status, self.theme, lines=2)
        self.status_area.grid(row=0 if compact else 2, column=0, sticky="ew", pady=px((0, SPACE.lg)))
        self.task_hint = window_var(self.root, "空闲：等待代码改动。当前没有可取消的出题请求。")
        wrapped_label(self.status_area, self.theme, textvariable=self.task_hint, style=MUTED).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=px((SPACE.sm, 0)))
        self.task_progress = ttk.Progressbar(self.status_area, mode="indeterminate")
        self.task_progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=px((SPACE.sm, 0)))
        self.task_progress.grid_remove()
        content = self.dashboard_view.body
        if compact:
            content = ttk.Frame(content)
            content.columnconfigure(0, weight=1)
            content.grid(row=1, column=0, sticky="ew")
        wrapped_label(content, self.theme, text="当前项目", style=SECTION).grid(row=0, column=0, sticky="ew")
        wrapped_label(content, self.theme, textvariable=self.current_project, style=MUTED).grid(
            row=1, column=0, sticky="ew", pady=px((SPACE.sm, SPACE.sm)))
        self.projects_area = MessageArea(content, self.projects_text, self.theme, lines=3, muted=True)
        self.projects_area.grid(row=2, column=0, sticky="ew")
        wrapped_label(content, self.theme, text="学习概况", style=SECTION).grid(
            row=3, column=0, sticky="ew", pady=px((SPACE.lg, SPACE.sm)))
        metrics = ttk.Frame(content)
        metrics.grid(row=4, column=0, sticky="ew")
        for column, (name, title) in enumerate((("answered", "累计作答"), ("accuracy", "正确率"),
                                                ("pending", "待答题"), ("reviews", "待复习知识点"))):
            metrics.columnconfigure(column, weight=1, uniform="metric")
            tile = ttk.Frame(metrics, padding=px(SPACE.sm))
            tile.grid(row=0, column=column, sticky="nsew")
            wrapped_label(tile, self.theme, textvariable=self.learning_values[name], style=SECTION).pack(fill="x")
            wrapped_label(tile, self.theme, text=title, style=MUTED).pack(fill="x", pady=px((SPACE.xs, 0)))
        self.actions = FlowActions(panel, self.theme)
        self.actions.grid(row=4, column=0, sticky="ew", pady=px((SPACE.lg, 0)))
        self.resume_button = self.actions.add(ttk.Button(self.actions, text="继续答题 (Ctrl+R)", style=PRIMARY,
                                   command=self._continue_practice))
        self.pause_button = self.actions.add(ttk.Button(self.actions, text="暂停全部监听", command=self._pause))
        # Secondary actions scroll with the overview, leaving a bounded footer
        # even when the sidebar narrows the workspace at high DPI.
        tools = FlowActions(content, self.theme)
        tools.grid(row=5, column=0, sticky="ew", pady=px((SPACE.lg, 0)))
        self.add_button = tools.add(ttk.Button(tools, text="添加监听项目", command=self._add_project))
        self.history_button = tools.add(ttk.Button(tools, text="答题记录", command=self._open_history))
        self.root.bind("<Control-r>", self._continue_practice_key)
        self.root.bind("<Escape>", self._escape_page)
        self.bank_button = tools.add(ttk.Button(tools, text="更换题库", command=lambda: self._navigate_page("bank")))
        self.configure_button = tools.add(ttk.Button(tools, text="配置出题模型", command=lambda: self._navigate_page("model")))
        self.retry_button = tools.add(ttk.Button(tools, text="重试出题", command=lambda: self._request_action("retry_generation")))
        login = self.tk.Menu(self.root, tearoff=False)
        login.add_command(label="启用登录自启动", command=lambda: self._request_action("startup", True))
        login.add_command(label="取消登录自启动", command=lambda: self._request_action("startup", False))
        tools.add(ttk.Menubutton(tools, text="登录自启动", menu=login))
        self.shell.show_page("dashboard", self.panel)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self._sync_actions()
        self.poll = self.root.after(200, self._drain)
        self.focus_timer = self.root.after_idle(self.shell.heading.focus_set)
        if background:
            cancel_timer(self.root, self.focus_timer)
            self.focus_timer = self.root.after(50, self.root.iconify)

    def run(self):
        self._start_worker()
        self.root.mainloop()

    def _start_worker(self):
        self.worker_thread = threading.Thread(target=DesktopApp._worker, args=(self._worker_context(),),
                                              daemon=True, name="bagu-desktop-monitor")
        self.worker_thread.start()

    def _navigate_page(self, page):
        if (self.closed or self.closing
                or page not in dict(self.shell.PAGES) or self.pending_actions):
            return
        if page == self.shell.current_page:
            return
        if self.history_page is not None:
            if self.history_page.retry_busy:
                return
            self.history_page.leave()
            self.history_page = None
        if page == "history":
            self._open_history()
            return
        self._set_browsing(page not in ("dashboard", "practice"))
        if page == "practice":
            frame = self._page_frame("practice")
            self.shell.show_page("practice", frame, focus=frame.focus_target())
            self._refresh_practice()
        elif page == "dashboard":
            self.shell.show_page("dashboard", self.panel)
        else:
            self.shell.show_page(page, self._page_frame(page), focus=self._page_focus(page))
        self._sync_actions()

    def _page_frame(self, page):
        """Return (and lazily build) the real embedded page for a navigation key."""
        if page in self._cached_pages:
            return self._cached_pages[page]
        frame = self._build_page(page)
        self._cached_pages[page] = frame
        return frame

    def _page_focus(self, page):
        frame = self._page_frame(page)
        focus = getattr(frame, "focus_target", None)
        return focus() if callable(focus) else None

    def _build_page(self, page):
        if page == "projects":
            from .projects_panel import ProjectsPanel
            panel = ProjectsPanel(self.content_host, self.theme,
                                  on_saved=self._projects_saved)
            return panel
        if page == "bank":
            from .bank_panel import BankPanel
            panel = BankPanel(self.content_host, self.theme, tasks=self.tasks,
                              on_built=self._bank_built)
            return panel
        if page == "model":
            from .model_panel import ModelPanel
            # The panel owns its own worker/queue; never share the app's event
            # queue (its (tag, result, error) triples would collide with the
            # app's 2-tuple events).
            panel = ModelPanel(self.content_host, self.theme, on_saved=self._model_saved)
            return panel
        if page == "settings":
            from .settings_page import SettingsPage
            panel = SettingsPage(self.content_host, self.theme, on_status=self._set_status)
            return panel
        if page == "practice":
            from .practice_page import PracticePage
            panel = PracticePage(self.content_host, self.theme, self.practice,
                                 on_enter=self._enter_pending)
            return panel
        raise ValueError(f"Unknown page: {page}")

    def _projects_saved(self, projects):
        # Reuse the existing serial worker to apply the watch-list change:
        # preserve unchanged-project baselines, add baselines only for new
        # projects and drop monitoring state for removed ones.
        self.jobs.put(("apply_projects", projects))
        self._sync_actions()

    def _bank_built(self, result):
        # A completed build does not touch the running bank; the engine reloads
        # at a safe boundary (generation/submit) only.
        self.status.set(f"新题库已构建（{result.questions} 道题），将在生成/提交安全边界应用。")

    def _model_saved(self):
        self.status.set("模型配置已保存，即时生效。")

    def _set_status(self, message):
        self.status.set(message)

    def _set_browsing(self, browsing):
        if not hasattr(self.monitor, "delivery_paused"):
            self.monitor.delivery_paused = threading.Event()
        if browsing:
            self.monitor.delivery_paused.set()
        else:
            self.monitor.delivery_paused.clear()
        self._update_practice_notice()

    def _update_practice_notice(self):
        """New questions update the nav badge + a global hint without stealing
        the current page.  The notice only shows when not already on practice."""
        count = len(self._pending_items)
        self.shell.set_badge("practice", count)
        if self.shell.current_page not in ("practice", "dashboard") and count:
            self.shell.show_practice_notice(count)
        else:
            self.shell.show_practice_notice(0)

    def _refresh_practice(self):
        """Push the latest pending list + due-review count into the practice page."""
        frame = self._page_frame("practice")
        if hasattr(frame, "set_pending"):
            frame.set_pending(self._pending_items, self._due_reviews)
        self.learning_values["pending"].set(str(len(self._pending_items)))
        self._update_practice_notice()

    def _enter_pending(self, quiz_id):
        """按题进入: request the worker to deliver a specific ready/delivered quiz."""
        if self.closed or self.closing or "enter_quiz" in self.pending_actions:
            return False
        if not self.practice.begin_enter(quiz_id):
            return False
        self.pending_actions.add("enter_quiz")
        self.jobs.put(("enter_quiz", quiz_id))
        self._sync_actions()
        return True

    def _continue_practice(self):
        self._navigate_page("practice")
        if self.practice.current is None and not self.practice.busy:
            self._request_action("resume")

    def _continue_practice_key(self, event):
        # Ctrl+R jumps to practice, but never while typing in a form field.
        widget = self.root.focus_get()
        if widget is not None and widget.winfo_class() in ("TEntry", "TCombobox", "Text", "TSpinbox"):
            return None
        self._continue_practice()
        return "break"

    def _escape_page(self, event):
        if (self.history_page is not None and event.widget.winfo_toplevel() is self.root
                and self.history_page.detail_id is not None):
            self.history_page.back()
            return "break"

    def _open_history(self):
        if (self.closed or self.closing or self.history_page is not None
                or self.pending_actions):
            return
        from .history_page import HistoryPage
        # This navigation must not stop the worker or rebuild file baselines.
        self._set_browsing(True)
        if self._cached_history is None:
            self._cached_history = HistoryPage(self, self.content_host)
        self.history_page = self._cached_history
        self.history_page.enter()
        self.shell.show_page("history", self.history_page.frame, focus=self.history_page.focus_target())
        self._sync_actions()

    def _return_from_history(self):
        if self.history_page is None or self.history_page.retry_busy:
            return
        self._navigate_page("dashboard")

    def _apply_projects_serial(self, paths):
        """Apply a watch-list change on the worker: preserve unchanged-project
        baselines, add baselines only for new projects, and drop monitoring
        state for removed projects (their history stays)."""
        engine = self.monitor.engine
        normalize = getattr(engine, "_normalize_project_root", None) or (lambda p: str(p))
        new_roots = list(dict.fromkeys(normalize(p) for p in paths))
        new_set = set(new_roots)
        # Drop state for removed projects; keep baselines for unchanged ones.
        for removed in list(self.monitor.initialized - new_set):
            self.monitor.initialized.discard(removed)
            self.monitor.observed.pop(removed, None)
            self.monitor.waiting.pop(removed, None)
            getattr(engine, "_diff_readers", {}).pop(removed, None)
        self.monitor.failed_generations = {r: f for r, f in self.monitor.failed_generations.items() if r in new_set}
        # New projects get a baseline on the next poll (root not in initialized).

    def _worker_context(self):
        # The long-running thread must not retain an App/Tk/Variable even if
        # an external caller destroys the root without the normal close flow.
        from types import SimpleNamespace
        return SimpleNamespace(monitor=self.monitor, events=self.events, jobs=self.jobs,
                               stop=self.stop, worker_done=self.worker_done)

    def _request_action(self, name, value=None):
        if self.closed or self.closing or self.history_page is not None or name in self.pending_actions:
            return False
        if name == "configure":
            self._navigate_page("model")
            return True
        if name == "retry_generation" and (self.generation_busy or not self.generation_retryable or self.monitor.paused):
            return False
        if name == "resume" and self.generation_busy:
            return False
        self.pending_actions.add(name)
        self.jobs.put((name, value))
        self._sync_actions()
        return True

    def _sync_actions(self):
        if self.closed:
            return
        locked = self.closing
        # A submission in flight must NOT lock navigation: switching pages keeps
        # the current question and the late grade is written back by quiz id.
        self.shell.lock_navigation(locked or bool(self.pending_actions))
        self.history_button.state(["disabled"] if locked or self.pending_actions else ["!disabled"])
        for name, button in (("add_project", self.add_button),
                             ("configure", self.configure_button), ("bank", self.bank_button), ("pause", self.pause_button)):
            disabled = (locked or name in self.pending_actions)
            button.state(["disabled"] if disabled else ["!disabled"])
        self.resume_button.state(["disabled"] if locked or self.generation_busy else ["!disabled"])
        retry_ok = self.generation_retryable and not self.generation_busy and not self.monitor.paused
        self.retry_button.state(["!disabled"] if retry_ok and not locked and "retry_generation" not in self.pending_actions else ["disabled"])
        self.retry_button.configure(text="正在重试…" if "retry_generation" in self.pending_actions else "重试出题")
        if self.closing:
            hint = "正在安全退出：当前出题请求或记录写入不可取消，将在任务结束后关闭。"
            self.listen_state.set("正在安全退出")
        elif self.generation_busy:
            hint = "进行中：模型出题。当前请求不支持取消；可暂停后续监听，请勿重复重试。"
            if "pause" in self.pending_actions:
                hint = "已请求暂停后续监听；当前出题不可取消，将在请求结束后暂停。"
            self.listen_state.set("进行中 · 正在准备题目")
        elif self.generation_retryable:
            hint = "失败：" + self.generation_reason + (" 请恢复监听后点击“重试出题”。" if self.monitor.paused else " 可检查模型配置后点击“重试出题”。")
        elif self.pending_actions:
            hint = "进行中：正在处理操作；当前操作暂不可取消，重复操作已阻止。"
        else:
            hint = self.generation_reason or "空闲：等待新的代码改动。没有失败任务时“重试出题”不可用。"
        set_text(self.task_hint, hint)
        self._sync_global_status()

    def _sync_global_status(self):
        suffix = f" · {self.project_count} 个项目" if self.project_count is not None else ""
        set_text(self.global_status, self.listen_state.get() + suffix)

    def _worker(self):
        try:
            while True:
                try:
                    while True:
                        try:
                            name, value = self.jobs.get_nowait()
                        except queue.Empty:
                            break
                        # Already queued writes must finish during normal exit;
                        # never start another generation or scan while closing.
                        if self.stop.is_set() and name not in {"submit", "repractice"}:
                            continue
                        try:
                            if name == "submit":
                                quiz_id, answer = value
                                try:
                                    grade = self.monitor.engine.submit_answer(quiz_id, answer)
                                except Exception as error:
                                    grade = {"ok": False, "retryable": True,
                                             "note": f"判分或保存未完成（{type(error).__name__}），请点击重试提交。"}
                                self.events.put(("grade", {"quiz_id": quiz_id, "answer": answer, "grade": grade}))
                            elif name == "repractice":
                                page_id, attempt_id = value
                                try:
                                    result = self.monitor.engine.repractice_attempt(attempt_id)
                                except Exception as error:
                                    result = {"ok": False, "note": f"重练准备失败（{type(error).__name__}），请重试。"}
                                self.events.put(("repractice", (page_id, result)))
                            elif name == "resume":
                                quiz = self.monitor.resume_quiz()
                                self.events.put(("quiz", quiz)) if quiz else self.events.put(("status", "空状态：当前没有已展示的待答题。等待新题或新的代码改动。"))
                            elif name == "enter_quiz":
                                try:
                                    quiz = self.monitor.enter_quiz(value)
                                except Exception as error:
                                    quiz = None
                                    self.events.put(("status", f"切题失败（{type(error).__name__}），请重试。"))
                                else:
                                    if not quiz:
                                        self.events.put(("status", "该题已不存在或已过期，请从列表刷新后重试。"))
                                self.events.put(("enter_quiz", quiz))
                            elif name == "retry_generation":
                                self.monitor.retry_generation()
                            elif name == "reload_bank":
                                try:
                                    self.monitor.engine.reload_bank()
                                except Exception as error:
                                    self.events.put(("bank_applied", f"新题库应用失败（{type(error).__name__}），可重新构建后重试。"))
                                else:
                                    self.events.put(("bank_applied", None))
                            elif name == "apply_projects":
                                DesktopApp._apply_projects_serial(self, value)
                                self.events.put(("status", f"成功：监听项目已更新为 {len(value)} 个，未变化项目基线保留。"))
                            elif name == "startup":
                                from . import login_startup
                                login_startup.install() if value else login_startup.uninstall()
                                self.events.put(("status", "成功：已启用登录自启动。" if value else "成功：已取消登录自启动。"))
                            elif name == "add_project":
                                from .desktop_monitor import add_project
                                add_project(value)
                                self.events.put(("status", "成功：监听项目已添加，正在建立基线。"))
                            elif name == "pause":
                                self.monitor.paused = value
                                self.monitor.waiting.clear()
                                self.monitor.initialized.clear()
                            elif name == "dismiss":
                                if not value or self.monitor.active == value:
                                    self.monitor.active = None
                        except Exception as error:
                            self.events.put(("status", f"操作失败（{type(error).__name__}），请检查配置或目录后再次点击原操作重试。"))
                            self.events.put(("generation", {"busy": False, "retryable": bool(getattr(self.monitor, "failed_generations", {})),
                                                            "reason": "出题未完成，请检查配置后重试。"}))
                        finally:
                            self.events.put(("action_done", name))
                    if self.stop.is_set():
                        break
                    result = self.monitor.poll()
                    labels = {
                        "paused": "监听已暂停。",
                        "watching": f"正在监听 {result['projects']} 个项目；文件稳定 15 秒后检查知识点。",
                        "quiz_ready": "成功：题目准备就绪。",
                        "generation_unavailable": "出题失败，请查看原因并重试；监听仍在运行。",
                        "project_unavailable": "监听目录不可用，请恢复目录或添加有效项目后重试。",
                    }
                    self.events.put(("status", labels[result["state"]]))
                    DesktopApp._post_dashboard(self, result["state"])
                    # Post the read-only pending list so the badge/page stay fresh
                    # even while browsing; viewing the list never delivers.
                    self.events.put(("pending", self.monitor.list_pending()))
                    gate = getattr(self.monitor, "delivery_paused", None)
                    quiz = None if gate is not None and gate.is_set() else self.monitor.take_quiz()
                    if quiz:
                        self.events.put(("quiz", quiz))
                except Exception as error:
                    self.events.put(("status", f"本次检测失败（{type(error).__name__}），稍后自动重试；请检查监听目录或配置。"))
                    self.events.put(("generation", {"busy": False, "retryable": bool(getattr(self.monitor, "failed_generations", {})),
                                                    "reason": "本次检测未完成。"}))
                self.stop.wait(3)
        finally:
            self.worker_done.set()

    def _post_dashboard(self, state):
        # Read-only aggregation on the existing worker; never touch Tk or alter
        # monitor decisions, pending queues, credential receipts or learning data.
        try:
            engine = self.monitor.engine
            roots = engine.load_config().get("project_roots") or [str(engine.PROJECT_ROOT)]
            stats = engine.get_learning_stats()
            self.events.put(("dashboard", {"state": state, "projects": roots, "stats": stats}))
        except Exception:
            self.events.put(("dashboard", {"state": state, "projects": None, "stats": None}))

    def _render_dashboard(self, snapshot):
        if self.closed or self.closing:
            return
        labels = {"paused": "监听已暂停", "watching": "正在监听代码改动", "quiz_ready": "练习已就绪",
                  "generation_unavailable": "监听中 · 出题暂不可用", "project_unavailable": "请检查监听目录"}
        self.listen_state.set("进行中 · 正在准备题目" if self.generation_busy else labels.get(snapshot.get("state"), "正在准备监听"))
        roots = snapshot.get("projects")
        if roots is not None:
            self.project_count = len(roots)
        self._sync_global_status()
        if roots is not None:
            text = f"正在监听 {len(roots)} 个项目\n" + "\n".join(map(str, roots))
            if text != self.projects_text.get():
                self.projects_text.set(text)
            if self.practice.current is None:
                self.current_project.set(str(roots[0]) if roots else "尚未添加项目")
        stats = snapshot.get("stats")
        if stats and stats.get("ok"):
            self.learning_values["answered"].set(str(stats["total_attempts"]))
            self.learning_values["accuracy"].set(f"{stats['accuracy']:.0%}" if stats["total_attempts"] else "暂无作答")
            self.learning_values["reviews"].set(str(stats["due_reviews"]))
            self._due_reviews = stats["due_reviews"]
            self._refresh_practice()

    def _drain(self):
        if self.closed:
            return
        cancel_timer(self.root, self.poll)
        self.poll = None
        try:
            while True:
                name, value = self.events.get_nowait()
                if self.closing:
                    continue
                if name == "status":
                    set_text(self.status, value)
                elif name == "generation":
                    self.generation_busy = value["busy"]
                    if not value["busy"]:
                        self.generation_retryable = value.get("retryable", False)
                        self.generation_reason = value.get("reason", "")
                        self.listen_state.set("失败 · 模型出题未完成" if self.generation_retryable else
                                              ("成功 · 题目已就绪" if value.get("state") == "quiz_ready" else
                                               ("监听已暂停" if self.monitor.paused else "正在监听代码改动")))
                        self.task_progress.stop()
                        self.task_progress.grid_remove()
                    else:
                        self.task_progress.grid()
                        self.task_progress.start(15)
                    self._sync_actions()
                elif name == "action_done":
                    self.pending_actions.discard(value)
                    if value == "pause":
                        self.pause_button.configure(text="恢复全部监听" if self.monitor.paused else "暂停全部监听")
                        self.listen_state.set("监听已暂停" if self.monitor.paused else "正在监听代码改动")
                    self._sync_actions()
                elif name == "dashboard":
                    self._render_dashboard(value)
                elif name == "quiz":
                    self._show_quiz(value)
                elif name == "pending":
                    # Read-only pending list from the worker; update badge + page
                    # without delivering anything or stealing the current page.
                    self._pending_items = value
                    self._refresh_practice()
                elif name == "enter_quiz":
                    # The worker delivered a specific quiz (按题进入); park it as
                    # the current question through the controller.
                    self.pending_actions.discard("enter_quiz")
                    self.practice.finish_enter(value)
                    self._refresh_practice()
                    self._sync_actions()
                elif name == "bank_applied":
                    self._bank_reload_pending = False
                    if value:
                        self.tasks.bank.state = "failed"
                        self.tasks.bank.error = value
                        self.status.set(value)
                    else:
                        self.tasks.mark_applied()
                    panel = self._cached_pages.get("bank")
                    if panel is not None:
                        panel.refresh()
                elif name == "history":
                    if self._cached_history is not None:
                        self._cached_history.receive(*value)
                elif name == "repractice":
                    page_id, result = value
                    self.pending_actions.discard("repractice")
                    if self.history_page is not None and self.history_page.page_id == page_id:
                        self.history_page.retry_finished(result)
                    if result.get("ok"):
                        # Historical repractice enters the practice center and
                        # preserves its source; it never overwrites an automatic
                        # question (the controller queues it instead).
                        self._show_quiz(result["quiz"], manual=True)
                        self._navigate_page("practice")
                    self._sync_actions()
                elif name == "grade":
                    if value.get("grade", value).get("ok") and self._cached_history is not None:
                        self._cached_history.dirty = True
                    if "grade" in value:
                        self._apply_grade(value["quiz_id"], value["grade"], value["answer"])
                    else:
                        # A bare grade (no id) targets the current question.
                        self._apply_grade(self.practice.quiz_id, value)
                    self._sync_actions()
                elif name == "show":
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        if self.closing:
            if self._exit_tasks_finished():
                self.root.destroy()
                return
        elif self.tasks.bank.state == "built" and not self._bank_reload_pending:
            # The existing serial engine worker is the safety boundary. Tk
            # never waits on the generation lock or mutates a running bank.
            self._bank_reload_pending = True
            self.jobs.put(("reload_bank", None))
        self.monitor.ui_state = {"window_state": self.root.state(), "monitor_paused": self.monitor.paused,
                                 "page": self.shell.current_page,
                                 "inflight_submissions": len(self.inflight_submissions),
                                 "pending_jobs": self.jobs.qsize(), "closing": self.closing,
                                 "status": self.status.get()}
        self.poll = self.root.after(200, self._drain)

    def _show_quiz(self, quiz, manual=False):
        """Route a delivered quiz through the single practice entry point."""
        if self.closed or self.closing or not quiz:
            return
        self.practice.deliver(quiz, manual=manual)
        self._refresh_practice()

    def _can_submit_quiz(self, quiz_id):
        return (not self.closed and not self.closing
                and self.practice.entering_quiz_id is None
                and quiz_id not in self.inflight_submissions)

    def _submit_quiz(self, quiz_id, answer):
        """Callback from the controller: enqueue a submit job (deduped)."""
        if (not self._can_submit_quiz(quiz_id) or quiz_id != self.practice.quiz_id
                or self.practice.submit_state != "submitting"):
            return False
        self.inflight_submissions.add(quiz_id)
        self.pending_answers[quiz_id] = answer
        self.jobs.put(("submit", (quiz_id, answer)))
        self._sync_actions()
        return True

    def _dismiss_quiz(self, quiz_id):
        """Callback from the controller: clear the delivered slot on the worker."""
        if self.closed or self.closing:
            return
        self.pending_actions.add("dismiss")
        self.jobs.put(("dismiss", quiz_id))
        self._sync_actions()

    def _apply_grade(self, quiz_id, grade, submitted_answer=None):
        """Write a grade back by quiz id; the controller ignores stale ids."""
        if quiz_id in self.inflight_submissions:
            self.inflight_submissions.discard(quiz_id)
            self.pending_answers.pop(quiz_id, None)
        if self.practice.quiz_id == quiz_id:
            self.practice.apply_grade(grade, submitted_answer)
        self._refresh_practice()
        self._sync_actions()

    def _pause(self):
        if self._request_action("pause", not self.monitor.paused):
            self.pause_button.configure(text="等待恢复…" if self.monitor.paused else "等待暂停…")

    def _add_project(self):
        from tkinter import filedialog
        if self.closed or self.closing or "add_project" in self.pending_actions:
            return
        path = filedialog.askdirectory(parent=self.root, title="选择要监听的代码项目")
        if path:
            self._request_action("add_project", path)

    def _destroyed(self, event):
        if event.widget is self.root:
            self.closed = True
            if self._cached_history is not None and not self._cached_history.closed:
                self._cached_history.closed = True
                self._cached_history.executor.shutdown(wait=False, cancel_futures=True)
            # A running bank build is cancellable; cooperate on teardown.
            self.tasks.cancel_build()
            self.stop.set()
            self.monitor.ui_post = None
            cancel_timer(self.root, self.poll)
            cancel_timer(self.root, self.focus_timer)
            self.poll = self.focus_timer = None

    def _close(self):
        if self.closed or self.closing:
            return
        from tkinter import messagebox
        if messagebox.askyesno("退出训练器", "退出后将停止自动出题。确定退出？", parent=self.root):
            self.window_placement.save()
            self.closing = True
            self.stop.set()
            # A cancellable bank build cooperates on exit; a non-cancellable
            # model save completes through its panel's own close-wait logic.
            self.tasks.cancel_build()
            model = self._cached_pages.get("model")
            if model is not None:
                model.close()
            self.task_progress.stop()
            if not self._exit_tasks_finished():
                self.status.set("正在安全退出：等待当前请求或记录写入结束，不会开始新的网络任务。")
                self._sync_actions()
                return
            self.root.destroy()

    def _exit_tasks_finished(self):
        workers = [self.worker_thread, self.tasks.bank.worker]
        workers.extend(getattr(page, "worker", None) for page in self._cached_pages.values())
        return not any(worker is not None and worker.is_alive() for worker in workers)


if __name__ == "__main__":
    main()
