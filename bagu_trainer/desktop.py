"""Windows desktop quiz application. UI stays on the main Tk thread."""
import argparse
import os
import queue
import threading


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
        from .tk_runtime import prepare_tk
        prepare_tk()
        import tkinter as tk
        root = tk.Tk()
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
        from .tk_runtime import prepare_tk
        prepare_tk()
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.monitor = monitor
        self.events = events if events is not None else queue.Queue()
        self.monitor.ui_post = self.events.put
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.root = tk.Tk()
        self.root.title("AI 八股训练器")
        self.root.geometry("560x330")
        self.root.minsize(480, 320)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.popup = None
        self.answer = tk.StringVar(self.root)
        self.status = tk.StringVar(self.root, "正在建立文件监听基线…")
        panel = ttk.Frame(self.root, padding=22)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="AI 八股训练器", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(panel, text="代码改动稳定后，相关题目会自动弹出。", wraplength=490).pack(anchor="w", pady=(10, 14))
        ttk.Label(panel, textvariable=self.status, wraplength=490).pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="添加监听项目", command=self._add_project).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="继续答题 (Ctrl+R)", command=lambda: self.jobs.put(("resume", None))).pack(side="left", padx=6)
        self.root.bind("<Control-r>", lambda _event: self.jobs.put(("resume", None)))
        self.pause_button = ttk.Button(row, text="暂停监听", command=self._pause)
        self.pause_button.pack(side="left", padx=6)
        ttk.Button(panel, text="配置出题模型", command=lambda: self.jobs.put(("configure", None))).pack(anchor="w", pady=8)
        login = self.tk.Menu(self.root, tearoff=False)
        login.add_command(label="启用登录自启动", command=lambda: self.jobs.put(("startup", True)))
        login.add_command(label="取消登录自启动", command=lambda: self.jobs.put(("startup", False)))
        menu = ttk.Menubutton(panel, text="登录自启动", menu=login)
        menu.pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(200, self._drain)
        self.root.after_idle(self.root.focus_set)
        if background:
            self.root.after(50, self.root.iconify)

    def run(self):
        threading.Thread(target=self._worker, daemon=True, name="bagu-desktop-monitor").start()
        self.root.mainloop()

    def _worker(self):
        while not self.stop.is_set():
            try:
                while True:
                    try:
                        name, value = self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    if name == "submit":
                        quiz_id, answer = value
                        self.events.put(("grade", self.monitor.engine.submit_answer(quiz_id, answer)))
                    elif name == "resume":
                        quiz = self.monitor.resume_quiz()
                        if quiz:
                            self.events.put(("quiz", quiz))
                        else:
                            self.events.put(("status", "当前没有已展示的待答题。"))
                    elif name == "configure":
                        self.monitor.engine.open_config_window()
                    elif name == "startup":
                        from . import login_startup
                        login_startup.install() if value else login_startup.uninstall()
                        self.events.put(("status", "已启用登录自启动。" if value else "已取消登录自启动。"))
                    elif name == "add_project":
                        from .desktop_monitor import add_project
                        add_project(value)
                    elif name == "pause":
                        self.monitor.paused = value
                        # Discard in-flight activity on pause; resume establishes fresh baselines.
                        self.monitor.waiting.clear()
                        self.monitor.initialized.clear()
                    elif name == "dismiss":
                        self.monitor.active = None
                result = self.monitor.poll()
                labels = {
                    "paused": "监听已暂停。",
                    "watching": f"正在监听 {result['projects']} 个项目；文件稳定 15 秒后检查知识点。",
                    "quiz_ready": "已匹配相关知识点，题目准备就绪。",
                    "generation_unavailable": "命中知识点，但暂时无法出题。请检查模型配置或来源正文。",
                    "project_unavailable": "有监听目录不可用，请确认项目文件夹仍然存在。",
                }
                self.events.put(("status", labels[result["state"]]))
                quiz = self.monitor.take_quiz()
                if quiz:
                    self.events.put(("quiz", quiz))
            except Exception as error:
                self.events.put(("status", f"本次检测未完成（{type(error).__name__}），稍后会自动重试。"))
            self.stop.wait(3)

    def _drain(self):
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "status":
                    if value != self.status.get():
                        self.status.set(value)
                elif name == "quiz":
                    self._show_quiz(value)
                elif name == "grade":
                    self._show_grade(value)
                elif name == "show":
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.monitor.ui_state = {"window_state": self.root.state(),
                                     "monitor_paused": self.monitor.paused,
                                     "status": self.status.get()}
            self.root.after(200, self._drain)

    def _show_quiz(self, quiz):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.deiconify()
            self.popup.lift()
            return
        tk, ttk = self.tk, self.ttk
        popup = self.popup = tk.Toplevel(self.root)
        popup.title("代码知识点 · 随堂练习")
        popup.geometry("700x620")
        popup.minsize(560, 440)
        outer = ttk.Frame(popup, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=quiz.get("knowledge_point") or "相关知识点",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(outer, text=quiz.get("project_root", ""), wraplength=630).pack(anchor="w", pady=(0, 12))
        # Scrollable body keeps long questions and feedback reachable on small screens.
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        ttk.Label(body, text=quiz["question"], wraplength=500, justify="left").pack(fill="x", pady=(0, 18))
        self.answer.set("")
        self.option_buttons = []
        for letter in "ABCD":
            button = ttk.Radiobutton(body, text=f"{letter}. {quiz['options'][letter]}",
                                     value=letter, variable=self.answer)
            button.pack(anchor="w", fill="x", pady=7)
            button.configure(style="Quiz.TRadiobutton")
            # ttk radiobuttons don't support wraplength; use their label element.
            ttk.Style(popup).configure("Quiz.TRadiobutton", wraplength=490)
            self.option_buttons.append(button)
        self.feedback = tk.StringVar(popup, "选择一个选项后提交，即可查看解析。快捷键：A–D 选择，Ctrl+Enter 提交。")
        ttk.Label(body, textvariable=self.feedback, wraplength=500, justify="left").pack(fill="x", pady=18)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=10)
        self.submit_button = ttk.Button(actions, text="提交答案", command=lambda: self._submit(quiz["quiz_id"]))
        self.submit_button.pack(side="left")
        ttk.Button(actions, text="稍后 / 关闭", command=self._dismiss).pack(side="left", padx=10)
        for letter in "ABCD":
            def choose(_event, selected=letter):
                if not self.submit_button.instate(["disabled"]):
                    self.answer.set(selected)
                return "break"
            popup.bind("<KeyPress-" + letter.lower() + ">", choose)
            popup.bind("<KeyPress-" + letter + ">", choose)
        popup.bind("<Control-Return>", lambda _event: self._submit(quiz["quiz_id"]))
        popup.protocol("WM_DELETE_WINDOW", self._dismiss)
        popup.lift()
        popup.after_idle(lambda: popup.winfo_exists() and self.option_buttons[0].focus_set())
        popup.attributes("-topmost", True)
        popup.after(1200, lambda: popup.winfo_exists() and popup.attributes("-topmost", False))

    def _submit(self, quiz_id):
        if self.submit_button.instate(["disabled"]):
            return
        if not self.answer.get():
            self.feedback.set("请先选择 A、B、C 或 D。")
            return
        self.submit_button.state(["disabled"])
        self.jobs.put(("submit", (quiz_id, self.answer.get())))

    def _show_grade(self, grade):
        if self.popup is None or not self.popup.winfo_exists():
            return
        if not grade.get("ok"):
            self.feedback.set(grade.get("note") or "提交失败，请稍后重试。")
            return
        for button in self.option_buttons:
            button.state(["disabled"])
        days = grade.get("schedule", {}).get("interval_days", 0)
        self.feedback.set(("回答正确" if grade["correct"] else "回答错误") +
                          f"，正确答案：{grade['correct_answer']}\n\n{grade.get('analysis', '')}"
                          f"\n\n已记录作答，复习间隔：{days:g} 天。")

    def _dismiss(self):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None
        self.jobs.put(("dismiss", None))

    def _pause(self):
        paused = self.pause_button.cget("text") == "暂停监听"
        self.pause_button.configure(text="恢复监听" if paused else "暂停监听")
        self.jobs.put(("pause", paused))

    def _add_project(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self.root, title="选择要监听的代码项目")
        if path:
            self.jobs.put(("add_project", path))

    def _close(self):
        from tkinter import messagebox
        if messagebox.askyesno("退出训练器", "退出后将停止自动出题。确定退出？", parent=self.root):
            self.stop.set()
            self.root.destroy()


if __name__ == "__main__":
    main()
