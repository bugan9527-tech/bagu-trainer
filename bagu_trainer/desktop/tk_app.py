"""Windows desktop quiz application. UI stays on the main Tk thread."""
import argparse
import os
import queue
import threading
import time

from bagu_trainer.desktop.ui_theme import apply_theme
from bagu_trainer.desktop.ui_theme import SPACE
from bagu_trainer.desktop.ui_theme import PRIMARY
from bagu_trainer.desktop.ui_theme import SECTION
from bagu_trainer.desktop.ui_theme import MUTED
from bagu_trainer.desktop.ui_state import cancel_timer
from bagu_trainer.desktop.ui_state import set_text
from bagu_trainer.desktop.ui_state import window_var


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI 八股训练器桌面版")
    parser.add_argument("--background", action="store_true", help="登录时最小化启动")
    parser.add_argument("--check-runtime", action="store_true", help="验证窗口运行环境后退出")
    parser.add_argument("--configure", action="store_true", help="导航到主窗口的模型配置页")
    parser.add_argument("--migrate-from", help="从旧版项目目录复制配置、题库和学习记录")
    parser.add_argument("--check-package", metavar="REPORT", help="验证便携运行环境并写入诊断 JSON")
    parser.add_argument("--install-startup", action="store_true", help="启用登录自启动")
    parser.add_argument("--uninstall-startup", action="store_true", help="取消登录自启动")
    parser.add_argument("--startup-directory", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("桌面自启动当前仅支持 Windows")
    if options.check_runtime:
        from bagu_trainer.desktop.tk_runtime import prepare_tk
        from bagu_trainer.desktop.tk_runtime import create_root
        prepare_tk()
        import tkinter as tk
        root = create_root()
        apply_theme(root)
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return 0
    if options.install_startup or options.uninstall_startup:
        from bagu_trainer.infrastructure import login_startup
        if options.uninstall_startup:
            login_startup.uninstall(options.startup_directory)
        else:
            login_startup.install(options.startup_directory)
        return 0
    if options.check_package and not options.migrate_from:
        from bagu_trainer.desktop.runtime import package_report
        package_report(options.check_package)
        return 0
    from bagu_trainer.infrastructure.app_cleanup import maintenance_active
    if maintenance_active():
        return 2
    from bagu_trainer.desktop.cleanup_cli import journal_path
    from bagu_trainer.desktop.cleanup_cli import launch_uninstall
    if journal_path().is_file():
        launch_uninstall()
        return 2
    from bagu_trainer.infrastructure.desktop_instance import OwnerLock
    from bagu_trainer.infrastructure.desktop_instance import WindowBroker
    from bagu_trainer.infrastructure.desktop_instance import remote_call
    owner = OwnerLock()
    if not owner.acquire():
        if not options.background:
            try:
                remote_call("_navigate", {"page": "model"}) if options.configure else remote_call("_show_dashboard", {})
            except Exception:
                pass
        return 0
    bridge = None
    try:
        if options.migrate_from:
            from bagu_trainer.infrastructure.migration import migrate_legacy
            migrate_legacy(options.migrate_from)
        if options.check_package:
            from bagu_trainer.desktop.runtime import package_report
            package_report(options.check_package)
            return 0
        # The broker also activates setup while configuration/engine is not ready.
        from bagu_trainer.desktop.startup import StartupCoordinator
        coordinator = StartupCoordinator()
        bridge = WindowBroker(coordinator.window_command, owner)
        bridge.start()
        from bagu_trainer.infrastructure.cleanup_manifest import register_profile
        try:
            register_profile(global_index=True)
        except (OSError, ValueError):
            pass  # Broken configuration is repaired in the main window.
        coordinator.run(options.background, page="model" if options.configure else "dashboard")
    finally:
        if bridge is not None:
            bridge.close()
        else:
            owner.close()
    return 0


class WorkQueue(queue.Queue):
    """Wake a waiting consumer on every enqueue (including shutdown)."""
    def __init__(self):
        super().__init__()
        self.wake = threading.Event()

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.wake.set()


class WorkerStop(threading.Event):
    def __init__(self, wake):
        super().__init__()
        self.wake = wake

    def set(self):
        super().set()
        self.wake.set()


class DesktopApp:
    def __init__(self, monitor, background=False, events=None, coordinator=None):
        from bagu_trainer.desktop.tk_runtime import prepare_tk
        from bagu_trainer.desktop.tk_runtime import create_root
        prepare_tk()
        import tkinter as tk
        from tkinter import ttk
        from bagu_trainer.desktop.ui_layout import ScrollableFrame
        from bagu_trainer.desktop.ui_layout import MessageArea
        from bagu_trainer.desktop.ui_layout import FlowActions
        from bagu_trainer.desktop.ui_layout import wrapped_label
        self.tk, self.ttk = tk, ttk
        self.monitor = monitor
        self.coordinator = coordinator
        self.ready = coordinator is None or getattr(monitor, "engine", None) is not None
        from bagu_trainer.application.learning.history import LearningHistory
        self.history_service = getattr(monitor, "engine", None) or LearningHistory()
        self.events = events if events is not None else queue.Queue()
        self.monitor.ui_post = self.events.put
        from bagu_trainer.application.maintenance import ContentMaintenance
        self.maintenance = ContentMaintenance(
            engine=lambda: monitor.engine,
            before_change=self.monitor.reset,
            unavailable=lambda: monitor.set_ready(False))
        if coordinator is not None:
            coordinator.maintenance = self.maintenance
        snapshot = getattr(self.monitor, "project_snapshot", None)
        self._project_snapshot = snapshot() if snapshot else {"projects": {}, "ready": False}
        self._project_request_id = 0
        self.jobs = WorkQueue()
        self.stop = WorkerStop(self.jobs.wake)
        self.monitor.stop = self.stop
        self.closed = self.closing = False
        self.worker_thread = None
        self.worker_done = threading.Event()
        self.pending_actions = set()
        self.history_page = None
        self._cached_history = None
        # Application task layer: holds the bank build, test and save tasks so
        # ordinary page switches never cancel or destroy them.
        from bagu_trainer.desktop.task_controller import TaskController
        self.tasks = TaskController()
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
        from bagu_trainer.infrastructure import config
        from bagu_trainer.desktop.ui_window import WindowPlacement
        self.window_placement = WindowPlacement(self.root, self.theme, config.USER_DATA_ROOT / "window.json")
        self.status = window_var(self.root, "正在建立文件监听基线…")
        self.listen_state = window_var(self.root, "正在准备监听")
        self.global_status = window_var(self.root, "正在准备监听")
        self.project_count = None
        self.projects_text = window_var(self.root, "正在读取监听项目…")
        self.current_project = window_var(self.root, "等待项目就绪")
        self.learning_values = {name: window_var(self.root, "—") for name in ("answered", "accuracy", "reviews", "pending")}
        # Both practice views share one controller and root-owned variables;
        # hiding the answer window or switching pages never resets an answer.
        self.answer = window_var(self.root)
        self.feedback = window_var(self.root, "选择一个选项后提交，即可查看解析。")
        self.answer_hint = window_var(self.root, "尚未提交：请选择一个选项。当前没有可取消的提交。")
        self.practice_window = None
        self._practice_serial = 0
        self._practice_request = None
        from bagu_trainer.desktop.practice_controller import PracticeController
        self.practice = PracticeController(self.answer, self.feedback, self.answer_hint,
                                           on_submit=self._submit_quiz, on_dismiss=self._dismiss_quiz,
                                           can_submit=self._can_submit_quiz, on_next=self._next_quiz)
        self.practice.on_changed = self._practice_changed
        from bagu_trainer.desktop.ui_shell import AppShell
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
        self.restored_notice = None
        self.removed_projects = []
        self.project_cleanup_text = window_var(self.root)
        self.project_cleanup_notice = wrapped_label(self.dashboard_view.body, self.theme,
                                                   textvariable=self.project_cleanup_text, style=MUTED)
        self.project_cleanup_notice.grid(row=3, column=0, sticky="ew", pady=px((0, SPACE.lg)))
        self.project_cleanup_notice.grid_remove()
        self.readiness_text = window_var(self.root, "正在检查本地准备状态…" if coordinator else "")
        self.readiness_area = ttk.Frame(self.dashboard_view.body)
        self.readiness_area.grid(row=1, column=0, sticky="ew", pady=px((0, SPACE.lg)))
        self.readiness_area.columnconfigure(0, weight=1)
        wrapped_label(self.readiness_area, self.theme, textvariable=self.readiness_text).grid(row=0, column=0, sticky="ew")
        repair = FlowActions(self.readiness_area, self.theme)
        repair.grid(row=1, column=0, sticky="ew")
        for page, label in (("bank", "前往题库管理"), ("model", "前往模型配置"), ("projects", "前往监听项目")):
            repair.add(ttk.Button(repair, text=label, command=lambda p=page: self._navigate_page(p)))
        repair.add(ttk.Button(repair, text="重新检查 / 加载引擎", command=lambda: self.jobs.put(("prepare", None))))
        if coordinator is None:
            self.readiness_area.grid_remove()
        content = ttk.Frame(self.dashboard_view.body)
        content.columnconfigure(0, weight=1)
        content.grid(row=4, column=0, sticky="ew")
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
        # Build the hidden reader during startup, keeping first explicit entry
        # as responsive as subsequent switches. This never maps or focuses it.
        self._answer_window()
        self.poll = self.root.after(25, self._drain)
        self.focus_timer = self.root.after_idle(self.shell.heading.focus_set)
        if background:
            cancel_timer(self.root, self.focus_timer)
            self.focus_timer = self.root.after(50, self.root.iconify)

    def show_restored_configuration(self):
        """Persistent local-restore notice, independent of changing worker status."""
        if self.restored_notice is not None:
            return
        if self.ready:
            self.readiness_area.grid_remove()
        from bagu_trainer.desktop.ui_layout import FlowActions
        from bagu_trainer.desktop.ui_layout import wrapped_label
        notice = self.ttk.Frame(self.dashboard_view.body)
        notice.grid(row=2, column=0, sticky="ew", pady=self.theme.px((0, SPACE.lg)))
        notice.columnconfigure(0, weight=1)
        self.restored_notice = notice
        self.restored_message = window_var(notice, "已恢复当前 Windows 用户保存在本机的配置。")
        wrapped_label(notice, self.theme, textvariable=self.restored_message).grid(row=0, column=0, sticky="ew")
        actions = FlowActions(notice, self.theme)
        actions.grid(row=1, column=0, sticky="ew", pady=self.theme.px((SPACE.sm, 0)))
        self.view_data_button = actions.add(self.ttk.Button(actions, text="查看数据目录",
                                             command=lambda: self._navigate_page("settings")))
        self.edit_restored_button = actions.add(self.ttk.Button(actions, text="修改配置",
                                                 command=lambda: self._navigate_page("model")))

    def run(self):
        self._start_worker()
        self.root.mainloop()

    def _start_worker(self):
        if self.coordinator is not None:
            self.jobs.put(("prepare", None))
        self.worker_thread = threading.Thread(target=DesktopApp._worker, args=(self._worker_context(),),
                                              daemon=True, name="bagu-desktop-monitor")
        self.worker_thread.start()

    def _navigate_page(self, page):
        if (self.closed or self.closing
                or page not in dict(self.shell.PAGES)):
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
            from bagu_trainer.desktop.projects_panel import ProjectsPanel
            panel = ProjectsPanel(self.content_host, self.theme,
                                  on_saved=self._projects_saved)
            panel.set_runtime_snapshot(self._project_snapshot)
            return panel
        if page == "bank":
            from bagu_trainer.desktop.bank_panel import BankPanel
            panel = BankPanel(self.content_host, self.theme, tasks=self.tasks,
                              on_built=self._bank_built, on_cleanup=self._queue_bank_cleanup,
                              maintenance=self.maintenance)
            return panel
        if page == "model":
            from bagu_trainer.desktop.model_panel import ModelPanel
            # The panel owns its own worker/queue; never share the app's event
            # queue (its (tag, result, error) triples would collide with the
            # app's 2-tuple events).
            panel = ModelPanel(self.content_host, self.theme, on_saved=self._model_saved)
            return panel
        if page == "settings":
            from bagu_trainer.desktop.settings_page import SettingsPage
            panel = SettingsPage(self.content_host, self.theme,
                                 on_navigate=self._navigate_page, on_uninstall=self._request_uninstall)
            return panel
        if page == "practice":
            from bagu_trainer.desktop.practice_page import PracticePage
            panel = PracticePage(self.content_host, self.theme, self.practice,
                                 on_enter=self._enter_pending)
            return panel
        raise ValueError(f"Unknown page: {page}")

    def _projects_saved(self, projects):
        # Reuse the existing serial worker to apply the watch-list change:
        # preserve unchanged-project baselines, add baselines only for new
        # projects and drop monitoring state for removed ones.
        self._project_request_id += 1
        request = {"request": self._project_request_id, "roots": list(projects)}
        self.jobs.put(("apply_projects" if self.ready else "prepare", request))
        self._sync_actions()
        return self._project_request_id

    def _bank_built(self, result):
        # A completed build does not touch the running bank; the engine reloads
        # at a safe boundary (generation/submit) only.
        self.status.set(f"新题库已构建（{result.questions} 道题），将在生成/提交安全边界应用。")
        if self.coordinator is not None:
            self.jobs.put(("prepare", None))

    def _bank_empty(self):
        return (not self.ready or self.monitor.engine is None
                or not self.monitor.engine.learning.has_question_bank())

    def _queue_bank_cleanup(self, maintenance, plan, include_unknown=False):
        if self.closed or self.closing or self.pending_actions:
            return False
        self.pending_actions.add("bank_cleanup")
        if self._cached_history is not None:
            self._cached_history.invalidate_content()
            self.jobs.put(("history_barrier", tuple(self._cached_history.futures)))
        self.jobs.put(("bank_cleanup", (maintenance, plan, include_unknown)))
        self._sync_actions()
        return True

    def _model_saved(self):
        self.status.set("模型配置已保存，即时生效。")
        if self.coordinator is not None:
            self.jobs.put(("prepare", None))

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
        self.practice.pending_ids = [item["quiz_id"] for item in self._pending_items]
        frame = self._page_frame("practice")
        if hasattr(frame, "set_pending"):
            frame.set_pending(self._pending_items, self._due_reviews)
        self.learning_values["pending"].set(str(len(self._pending_items)))
        self._update_practice_notice()
        self._practice_changed()

    def _practice_changed(self):
        """Fan out one controller notification to both views on the Tk thread."""
        frame = self._cached_pages.get("practice")
        if frame is not None:
            frame.refresh()
        if self.practice_window is not None:
            self.practice_window.refresh()

    def _answer_window(self):
        if self.practice_window is None:
            from bagu_trainer.desktop.practice_window import PracticeWindow
            self.practice_window = PracticeWindow(self.root, self.theme, self.practice,
                                                  on_destroy=self._answer_window_destroyed)
        return self.practice_window

    def _answer_window_destroyed(self, window):
        if self.practice_window is window:
            self.practice_window = None

    def _request_practice(self, kind, target=None, page_id=None):
        if self.closed or self.closing or self.tasks.bank.cleaning or self._bank_empty():
            return False
        if kind == "enter_quiz" and target == self.practice.quiz_id:
            self._answer_window().show()
            return self.practice.entering_quiz_id is None
        if self.practice.busy:
            # Repeated opening restores the same window without another job.
            if kind == "resume" or (self._practice_request and self._practice_request["target"] == target):
                self._answer_window().show()
            return False
        reservation = target if kind == "enter_quiz" else "__" + kind + "__"
        if not self.practice.begin_enter(reservation):
            return False
        self._practice_serial += 1
        request = dict(token=self._practice_serial, kind=kind, target=target,
                       reservation=reservation, page_id=page_id)
        self._practice_request = request
        self.pending_actions.add(kind)
        self._answer_window().show("正在加载题目，请稍候…")
        self.jobs.put(("practice_open", request))
        self._sync_actions()
        return True

    def _enter_pending(self, quiz_id):
        """按题进入: request the worker to deliver a specific ready/delivered quiz."""
        return self._request_practice("enter_quiz", quiz_id)

    def _next_quiz(self, quiz_id):
        if self.practice.busy or self.closed or self.closing:
            return False
        if not self.practice.has_next:
            if self.practice_window is not None:
                self.practice_window.hide()
            return True
        return self._request_practice("next_quiz", quiz_id)

    def _continue_practice(self):
        if self._bank_empty():
            self.status.set("当前无题库，请重新下载并构建来源后练习。")
            self._navigate_page("bank")
            return
        self._navigate_page("practice")
        if self.practice.current is not None:
            self._answer_window().show()
        else:
            self._request_practice("resume")

    def _start_repractice(self, page_id, attempt_id):
        accepted = self._request_practice("repractice", attempt_id, page_id)
        if accepted:
            self._navigate_page("practice")
        return accepted

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
        from bagu_trainer.desktop.history_page import HistoryPage
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
        self.monitor.apply_projects(paths)

    def _worker_context(self):
        # The long-running thread must not retain an App/Tk/Variable even if
        # an external caller destroys the root without the normal close flow.
        from types import SimpleNamespace
        return SimpleNamespace(monitor=self.monitor, events=self.events, jobs=self.jobs,
                               stop=self.stop, worker_done=self.worker_done, coordinator=self.coordinator,
                               maintenance=self.maintenance)

    def _request_action(self, name, value=None):
        if name == "resume":
            return self._request_practice("resume")
        if self.closed or self.closing or self.history_page is not None or name in self.pending_actions:
            return False
        if name == "configure":
            self._navigate_page("model")
            return True
        if name == "retry_generation" and (self.generation_busy or not self.generation_retryable or self.monitor.paused):
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
        self.shell.lock_navigation(locked)
        self.history_button.state(["disabled"] if locked or self.pending_actions else ["!disabled"])
        for name, button in (("add_project", self.add_button),
                             ("configure", self.configure_button), ("bank", self.bank_button), ("pause", self.pause_button)):
            disabled = (locked or name in self.pending_actions)
            button.state(["disabled"] if disabled else ["!disabled"])
        self.pause_button.state(["disabled"] if locked or not self.ready or "pause" in self.pending_actions else ["!disabled"])
        bank_empty = self._bank_empty()
        self.resume_button.state(["disabled"] if locked or bank_empty else ["!disabled"])
        retry_ok = self.generation_retryable and not self.generation_busy and not self.monitor.paused and not bank_empty
        self.retry_button.state(["!disabled"] if retry_ok and not locked and "retry_generation" not in self.pending_actions else ["disabled"])
        self.retry_button.configure(text="正在重试…" if "retry_generation" in self.pending_actions else "重试出题")
        if self.closing:
            hint = "正在安全退出：当前出题请求或记录写入不可取消，将在任务结束后关闭。"
            self.listen_state.set("正在安全退出")
        elif not self.ready:
            hint = "尚未完成使用准备，请在题库管理、模型配置和监听项目页修复，再重新检查。"
            self.listen_state.set("尚未就绪 · 可在侧栏完成准备")
        elif bank_empty:
            hint = "当前无题库，请重新下载并构建来源后练习。"
            self.listen_state.set("无题库 · 请重新下载来源")
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
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bagu-generation")
        background = None
        background_kind = None
        retry_requested = False
        next_scan = 0
        wake = getattr(self.jobs, "wake", threading.Event())
        next_project_cleanup = time.monotonic() + 30
        try:
            while True:
                try:
                    # Clear before draining: a concurrent put always leaves a wakeup.
                    wake.clear()
                    while True:
                        try:
                            name, value = self.jobs.get_nowait()
                        except queue.Empty:
                            break
                        # Already queued writes must finish during normal exit;
                        # never start another generation or scan while closing.
                        if self.stop.is_set() and name not in {"submit", "bank_cleanup", "history_barrier"}:
                            continue
                        try:
                            if name == "history_barrier":
                                from concurrent.futures import CancelledError
                                for future in value:
                                    try:
                                        future.result()
                                    except CancelledError:
                                        pass
                            elif name == "prepare":
                                if self.coordinator is not None:
                                    engine = self.coordinator.prepare()
                                    self.monitor.engine = engine
                                    self.monitor.capabilities_ready = engine is not None and self.coordinator.report.ready
                                    if hasattr(self.monitor, "set_ready"):
                                        self.monitor.set_ready(self.monitor.capabilities_ready)
                                        if self.monitor.capabilities_ready and (value or self.monitor.applied_roots is None):
                                            DesktopApp._apply_projects_serial(self, engine.load_config().get("project_roots", []))
                                        if value:
                                            self.events.put(("projects_applied", {**value,
                                                "snapshot": self.monitor.project_snapshot(),
                                                "error": "" if self.monitor.capabilities_ready else "后台尚未就绪，监听范围未应用。"}))
                                    self.events.put(("prepared", (self.coordinator.report, engine)))
                                    if not self.monitor.capabilities_ready:
                                        from bagu_trainer.application.learning.history import LearningHistory
                                        try:
                                            self.events.put(("dashboard", LearningHistory().dashboard()))
                                        except (OSError, ValueError):
                                            self.events.put(("dashboard", {"state": "not_ready", "projects": [], "stats": None}))
                            elif name == "submit":
                                quiz_id, answer = value
                                try:
                                    grade = self.monitor.engine.learning.submit_answer(quiz_id, answer)
                                except Exception as error:
                                    grade = {"ok": False, "retryable": True,
                                             "note": f"判分或保存未完成（{type(error).__name__}），请点击重试提交。"}
                                self.events.put(("grade", {"quiz_id": quiz_id, "answer": answer, "grade": grade}))
                            elif name == "practice_open":
                                # Data only: delivery/validity checks stay off the Tk thread.
                                request = value
                                kind, target = request["kind"], request["target"]
                                quiz, note, failed = None, "", False
                                try:
                                    if kind == "repractice":
                                        result = self.monitor.engine.learning.repractice_history(target)
                                        quiz = result.get("quiz") if result.get("ok") else None
                                        if not quiz:
                                            failed, note = True, result.get("note") or "重练准备失败，请重试。"
                                    elif kind == "resume":
                                        quiz = self.monitor.resume_quiz()
                                    elif kind == "enter_quiz":
                                        quiz = self.monitor.enter_quiz(target)
                                        if not quiz or quiz.get("quiz_id") != target:
                                            quiz, failed, note = None, True, "该题已不存在或已过期，请刷新列表后重试。"
                                    else:
                                        items = self.monitor.list_pending()
                                        index = next((i for i, item in enumerate(items) if item["quiz_id"] == target), -1)
                                        for item in items[index + 1:] + items[:index + 1]:
                                            if item["quiz_id"] != target:
                                                quiz = self.monitor.enter_quiz(item["quiz_id"])
                                                if quiz:
                                                    break
                                except Exception:
                                    failed, note = True, "题目加载失败，请重试；原题和作答草稿已保留。"
                                if not quiz and not note:
                                    note = "当前没有其他有效待答题，可以结束练习。"
                                self.events.put(("practice_opened", dict(request=request, quiz=quiz, failed=failed, note=note)))
                            elif name == "retry_generation":
                                retry_requested = True
                            elif name == "reload_bank":
                                try:
                                    if self.monitor.engine is not None:
                                        self.maintenance.apply_bank()
                                    elif self.coordinator is not None:
                                        self.jobs.put(("prepare", None))
                                except Exception as error:
                                    self.events.put(("bank_applied", f"新题库应用失败（{type(error).__name__}），可重新构建后重试。"))
                                else:
                                    self.events.put(("bank_applied", None))
                            elif name == "bank_cleanup":
                                maintenance, plan, include_unknown = value
                                try:
                                    if plan is None:
                                        from bagu_trainer.infrastructure.bank_cleanup import CleanupResult
                                        result = maintenance.recover() or CleanupResult([], [])
                                    else:
                                        result = maintenance.clean(plan.token, include_unknown=include_unknown,
                                            progress=lambda text: self.events.put(("cleanup_progress", text)))
                                    if result.failures:
                                        self.monitor.capabilities_ready = False
                                    self.monitor.active = None
                                    self.events.put(("bank_cleaned", (result, None)))
                                except Exception as error:
                                    self.events.put(("bank_cleaned", (None, str(error))))
                            elif name == "apply_projects":
                                roots = value["roots"] if isinstance(value, dict) else value
                                try:
                                    DesktopApp._apply_projects_serial(self, roots)
                                except Exception as error:
                                    if isinstance(value, dict):
                                        self.events.put(("projects_applied", {**value,
                                            "snapshot": self.monitor.project_snapshot(),
                                            "error": f"后台应用失败（{type(error).__name__}），原监听范围保留。"}))
                                    else:
                                        raise
                                else:
                                    if isinstance(value, dict):
                                        self.events.put(("projects_applied", {**value,
                                            "snapshot": self.monitor.project_snapshot(), "error": ""}))
                                    self.events.put(("status", f"成功：监听项目已更新为 {len(roots)} 个，未变化项目基线保留。"))
                            elif name == "startup":
                                from bagu_trainer.infrastructure import login_startup
                                login_startup.install() if value else login_startup.uninstall()
                                self.events.put(("status", "成功：已启用登录自启动。" if value else "成功：已取消登录自启动。"))
                            elif name == "uninstall":
                                from bagu_trainer.desktop.cleanup_cli import launch_uninstall
                                launch_uninstall()
                                self.events.put(("uninstall_started", None))
                            elif name == "add_project":
                                from bagu_trainer.application.monitor import add_project
                                add_project(value)
                                self.events.put(("status", "成功：监听项目已添加，正在建立基线。"))
                            elif name == "pause":
                                self.monitor.set_paused(value)
                            elif name == "dismiss":
                                if not value or self.monitor.active == value:
                                    self.monitor.active = None
                        except Exception as error:
                            if name == "prepare" and isinstance(value, dict) and hasattr(self.monitor, "project_snapshot"):
                                self.events.put(("projects_applied", {**value,
                                    "snapshot": self.monitor.project_snapshot(),
                                    "error": f"后台准备失败（{type(error).__name__}），监听范围未应用。"}))
                            self.events.put(("status", f"操作失败（{type(error).__name__}），请检查配置或目录后再次点击原操作重试。"))
                            self.events.put(("generation", {"busy": False, "retryable": bool(getattr(self.monitor, "failed_generations", {})),
                                                            "reason": "出题未完成，请检查配置后重试。"}))
                        finally:
                            if name != "retry_generation":
                                self.events.put(("action_done", name))
                    if self.stop.is_set():
                        break
                    coordinator = getattr(self, "coordinator", None)
                    if coordinator is not None and time.monotonic() >= next_project_cleanup:
                        next_project_cleanup = time.monotonic() + 30
                        removed, remaining = coordinator.prune_projects()
                        if removed:
                            if self.monitor.engine is not None:
                                DesktopApp._apply_projects_serial(self, remaining)
                            self.jobs.put(("prepare", None))
                            continue
                    if background is not None and background.done():
                        completed, background = background, None
                        if background_kind == "retry_generation":
                            self.events.put(("action_done", "retry_generation"))
                        result = completed.result()
                    else:
                        result = None
                    now = time.monotonic()
                    if background is None and retry_requested:
                        retry_requested = False
                        background_kind = "retry_generation"
                        background = executor.submit(self.monitor.retry_generation)
                        background.add_done_callback(lambda _: wake.set())
                    elif background is None and now >= next_scan:
                        next_scan = now + 3
                        if self.monitor.engine is not None and getattr(self.monitor, "capabilities_ready", True):
                            background_kind = "poll"
                            background = executor.submit(self.monitor.poll)
                            background.add_done_callback(lambda _: wake.set())
                    # Practice operations refresh immediately, independent of scan deadlines.
                    if self.monitor.engine is None or not getattr(self.monitor, "capabilities_ready", True):
                        wake.wait(max(0, min(next_scan, next_project_cleanup) - time.monotonic()))
                        continue
                    labels = {
                        "paused": "监听已暂停。",
                        "watching": f"正在监听 {result['projects'] if result else 0} 个项目；文件稳定 15 秒后检查知识点。",
                        "quiz_ready": "成功：题目准备就绪。",
                        "generation_unavailable": "出题失败，请查看原因并重试；监听仍在运行。",
                        "project_unavailable": "监听目录不可用，请恢复目录或添加有效项目后重试。",
                        "bank_empty": "当前无题库：请重新下载并构建来源后练习。",
                        "not_ready": "请在侧栏完成使用准备。",
                    }
                    if result is not None:
                        self.events.put(("status", labels[result["state"]]))
                        DesktopApp._post_dashboard(self, result["state"])
                    # Post the read-only pending list so the badge/page stay fresh
                    # even while browsing; viewing the list never delivers.
                    self.events.put(("pending", self.monitor.list_pending()))
                    # Background arrivals are list-only. Delivery is explicit,
                    # through practice_open, and never opens or focuses a window.
                except Exception as error:
                    self.events.put(("status", f"本次检测失败（{type(error).__name__}），稍后自动重试；请检查监听目录或配置。"))
                    self.events.put(("generation", {"busy": False, "retryable": bool(getattr(self.monitor, "failed_generations", {})),
                                                    "reason": "本次检测未完成。"}))
                deadline = min(next_scan, next_project_cleanup)
                # A running model completion wakes us; do not poll an expired scan deadline.
                if background is not None:
                    deadline = next_project_cleanup
                wake.wait(max(0, deadline - time.monotonic()))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            self.worker_done.set()

    def _post_dashboard(self, state):
        # Read-only aggregation on the existing worker; never touch Tk or alter
        # monitor decisions, pending queues, credential receipts or learning data.
        try:
            engine = self.monitor.engine
            roots = getattr(self.monitor, "applied_roots", None)
            if roots is None:
                roots = engine.load_config().get("project_roots", [str(engine.PROJECT_ROOT)])
            stats = engine.learning.learning_stats()
            self.events.put(("dashboard", {"state": state, "projects": roots, "stats": stats}))
        except Exception:
            self.events.put(("dashboard", {"state": state, "projects": None, "stats": None}))

    def _render_dashboard(self, snapshot):
        if self.closed or self.closing:
            return
        labels = {"paused": "监听已暂停", "watching": "正在监听代码改动", "quiz_ready": "练习已就绪",
                  "generation_unavailable": "监听中 · 出题暂不可用", "project_unavailable": "请检查监听目录",
                  "bank_empty": "无题库 · 请重新下载来源", "not_ready": "尚未就绪 · 可在侧栏完成准备"}
        self.listen_state.set("进行中 · 正在准备题目" if self.generation_busy else labels.get(snapshot.get("state"), "正在准备监听"))
        roots = snapshot.get("projects")
        if roots is not None:
            self.project_count = len(roots)
        self._sync_global_status()
        if roots is not None:
            text = (f"正在监听 {len(roots)} 个项目\n" if self.ready else
                (f"已配置 {len(roots)} 个项目，等待其余准备项\n" if roots else "尚未添加监听项目，请前往监听项目页。")) + "\n".join(map(str, roots))
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
            if not self.ready:
                self.learning_values["pending"].set(str(stats.get("pending", 0)))

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
                elif name == "uninstall_started":
                    self._close()
                elif name == "prepared":
                    report, engine = value
                    self.ready = engine is not None and report.ready
                    from bagu_trainer.application.learning.history import LearningHistory
                    self.history_service = engine if self.ready else LearningHistory()
                    text = "\n".join(item.detail for item in report.checks)
                    self.readiness_text.set(("使用准备已完成。" if self.ready else "尚未完成使用准备") + "\n" + text + ("\n" + report.error if report.error else ""))
                    self.status.set("监听与练习已就绪。" if self.ready else "可浏览完整侧栏，在对应页面完成准备。")
                    self._sync_actions()
                    if self.ready and self.restored_notice is None:
                        self.show_restored_configuration()
                    if self.ready:
                        self.readiness_area.grid_remove()
                    else:
                        self.readiness_area.grid()
                elif name == "projects_pruned":
                    removed, remaining = value
                    self.removed_projects = list(dict.fromkeys([*self.removed_projects, *removed]))
                    self.project_cleanup_text.set(
                        f"已自动移除 {len(self.removed_projects)} 个失效监听路径，学习记录已保留：\n"
                        + "\n".join(self.removed_projects))
                    self.project_cleanup_notice.grid()
                    panel = self._cached_pages.get("projects")
                    if panel is not None:
                        panel.forget_removed_projects(removed)
                elif name == "project_states":
                    old_order = (self._project_snapshot.get("version", -1), self._project_snapshot.get("sequence", -1))
                    if (value.get("version", 0), value.get("sequence", 0)) >= old_order:
                        self._project_snapshot = value
                        panel = self._cached_pages.get("projects")
                        if panel is not None:
                            panel.set_runtime_snapshot(value)
                elif name == "projects_applied":
                    snapshot = value["snapshot"]
                    old_order = (self._project_snapshot.get("version", -1), self._project_snapshot.get("sequence", -1))
                    if (snapshot.get("version", 0), snapshot.get("sequence", 0)) >= old_order:
                        self._project_snapshot = snapshot
                    panel = self._cached_pages.get("projects")
                    if panel is not None:
                        panel.application_finished(value["request"], value["roots"], snapshot, value["error"])
                elif name == "cleanup_progress":
                    panel = self._cached_pages.get("bank")
                    if panel is not None:
                        panel.status.set(value)
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
                    # Legacy/background producers must not replace the answer view.
                    if value and all(q["quiz_id"] != value["quiz_id"] for q in self._pending_items):
                        self._pending_items.append(value)
                        self._refresh_practice()
                elif name == "practice_opened":
                    self._finish_practice_open(value)
                elif name == "pending":
                    # Read-only pending list from the worker; update badge + page
                    # without delivering anything or stealing the current page.
                    self._pending_items = value
                    self._refresh_practice()
                elif name == "bank_cleaned":
                    result, error = value
                    panel = self._cached_pages.get("bank")
                    if panel is not None:
                        panel.finish_cleanup(result=result, error=error)
                    else:
                        self.tasks.bank.cleaning = False
                    if result is not None:
                        self.practice.clear()
                        self._pending_items = []
                        self._refresh_practice()
                        if self._cached_history is not None:
                            self._cached_history.invalidate_content()
                        if result.failures:
                            self.ready = False
                        elif self.coordinator is not None:
                            self.jobs.put(("prepare", None))
                        if result.switched:
                            self.tasks.bank.state = "applied"
                        self.status.set(result.message)
                        if result.empty:
                            self.practice.clear()
                            self._pending_items = []
                            self._refresh_practice()
                            self.generation_reason = "当前无题库，请重新下载并构建来源后练习。历史答题快照保留。"
                            self.generation_retryable = False
                            self.feedback.set(self.generation_reason)
                            self.listen_state.set("无题库 · 请重新下载来源")
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
                    self._navigate_page(value or "dashboard")
        except queue.Empty:
            pass
        if self.closing:
            if self._exit_tasks_finished():
                self.root.destroy()
                return
        elif self.tasks.bank.state == "built" and not self.tasks.bank.cleaning and not self._bank_reload_pending:
            # The existing serial engine worker is the safety boundary. Tk
            # never waits on the generation lock or mutates a running bank.
            self._bank_reload_pending = True
            self.jobs.put(("reload_bank", None))
        self.monitor.ui_state = {"window_state": self.root.state(), "monitor_paused": self.monitor.paused,
                                 "page": self.shell.current_page,
                                 "inflight_submissions": len(self.inflight_submissions),
                                 "practice_pending_count": len(self._pending_items),
                                 "practice_due_reviews": self._due_reviews,
                                 "pending_jobs": self.jobs.qsize(), "closing": self.closing,
                                 "status": self.status.get(),
                                 "bank_inventory_ready": bool(getattr(self._cached_pages.get("bank"), "inventory", None))}
        self.poll = self.root.after(25, self._drain)

    def _show_quiz(self, quiz, manual=False):
        """Route a delivered quiz through the single practice entry point."""
        if self.closed or self.closing or not quiz or self._bank_empty():
            return
        self.practice.deliver(quiz, manual=manual)
        self._refresh_practice()

    def _finish_practice_open(self, result):
        request = result["request"]
        if self._practice_request != request:
            return
        self._practice_request = None
        self.pending_actions.discard(request["kind"])
        quiz = result["quiz"]
        if quiz and request["kind"] == "enter_quiz" and quiz.get("quiz_id") != request["target"]:
            quiz = None
            result = {**result, "failed": True, "note": "题目加载结果已失效，请重新进入。"}
        # Content teardown clears the reservation, so a late delivery is ignored.
        applied = self.practice.complete_open(request["reservation"], quiz)
        if applied:
            window = self.practice_window
            if window is not None:
                window.notice.set(result["note"] if not quiz else "")
                if not quiz and not result["failed"] and request["kind"] == "next_quiz":
                    window.hide()
            if result["note"]:
                self.status.set(result["note"])
        elif self.practice_window is not None:
            self.practice_window.notice.set("题库或题目状态已变化，请从列表重新进入。")
        if request["kind"] == "repractice":
            if self._cached_history is not None and self._cached_history.page_id == request["page_id"]:
                self._cached_history.retry_finished({"ok": applied and bool(quiz),
                                                      "note": result["note"] or "题目已失效，请重新进入。"})
        self._refresh_practice()
        self._sync_actions()

    def _can_submit_quiz(self, quiz_id):
        return (not self.closed and not self.closing
                and not self.tasks.bank.cleaning
                and not self._bank_empty()
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
            self.practice_window = None
            self._practice_request = None
            self.practice.on_changed = None
            if self._cached_history is not None and not self._cached_history.closed:
                self._cached_history.closed = True
                self._cached_history.executor.shutdown(wait=False, cancel_futures=True)
            # A running bank build is cancellable; cooperate on teardown.
            self.tasks.cancel_build()
            self.stop.set()
            self.jobs.put(("wake", None))
            self.monitor.ui_post = None
            cancel_timer(self.root, self.poll)
            cancel_timer(self.root, self.focus_timer)
            self.poll = self.focus_timer = None

    def _close(self):
        if self.closed or self.closing:
            return
        self.window_placement.save()
        self.closing = True
        if self.practice_window is not None:
            self.practice_window.hide()
        self.stop.set()
        self.jobs.put(("wake", None))
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

    def _request_uninstall(self):
        """The settings page has already displayed and confirmed the inventory."""
        if self.closing or self.closed:
            return
        if "uninstall" in self.pending_actions:
            return
        self.pending_actions.add("uninstall")
        self.status.set("正在准备独立清理工具；准备成功后安全退出。")
        self.jobs.put(("uninstall", None))
        self._sync_actions()

    def _exit_tasks_finished(self):
        workers = [self.worker_thread, self.tasks.bank.worker]
        workers.extend(getattr(page, "worker", None) for page in self._cached_pages.values())
        workers.extend(getattr(page, "cleanup_worker", None) for page in self._cached_pages.values())
        return not any(worker is not None and worker.is_alive() for worker in workers)


if __name__ == "__main__":
    main()
