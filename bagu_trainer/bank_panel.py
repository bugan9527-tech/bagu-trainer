"""Bank management: independent disk, task and active-version state."""
import queue
import threading
import time
from pathlib import Path
from tkinter import ttk

from . import config
from .bank_build.control import BuildCancelled, BuildControl
from .bank_build.service import BankBuildService, load_sources
from .bank_build.source_snapshot import download_label
from .bank_inventory import BankInventory, LOCAL_LABELS, format_bytes
from .bank_cleanup import BankCleanupService
from .ui_layout import ScrollableFrame, FlowActions, wrapped_label, MessageArea
from .ui_state import window_var, set_text, cancel_timer
from .ui_theme import COLORS, SPACE, PRIMARY, SECTION, MUTED


TASK_LABELS = {"waiting": "等待", "downloading": "处理中", "completed": "完成", "failed": "失败", "cancelled": "取消"}


class BankPanel(ttk.Frame):
    def __init__(self, parent, theme, *, tasks, service=None, on_built=None, on_cleanup=None, protected_versions=None):
        super().__init__(parent)
        self.theme, self.tasks, self.on_built = theme, tasks, on_built
        self.service = service or BankBuildService()
        self.on_cleanup = on_cleanup
        self.protected_versions = protected_versions or (lambda: ())
        self.cleanup = BankCleanupService(self.service)
        self.sources = load_sources(self.service.manifest)
        self.project_root = Path(getattr(self.service, "project_root", config.USER_DATA_ROOT))
        self.closed = self.scanning = self.deleting = False
        self.inventory = None
        self._scanner = BankInventory(self.project_root, limits=getattr(self.service, "limits", None))
        self._disk_events = queue.Queue()
        self._scan_cancel = threading.Event()
        self._rescan = False
        self._revision = 0
        self._next_scan = 0
        self._last_task_state = tasks.bank.state
        self._last_source_states = ()
        px = theme.px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.summary = window_var(self, "当前题库：正在后台读取…")
        wrapped_label(self, theme, textvariable=self.summary, style=SECTION).grid(row=0, column=0, sticky="ew")
        self.space_summary = window_var(self, "空间：正在后台统计；远端下载大小未知。")
        wrapped_label(self, theme, textvariable=self.space_summary, style=MUTED).grid(row=1, column=0, sticky="ew", pady=px(SPACE.xs))
        self.view = ScrollableFrame(self, theme)
        self.view.grid(row=2, column=0, sticky="nsew")
        body = self.view.body
        body.columnconfigure(0, weight=1)
        wrapped_label(body, theme, text="已下载表示本地有该 commit 的完整来源，不代表当前用于练习。全部来源就绪后才构建切换。",
                      style=MUTED).grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        listing = ttk.Frame(body)
        listing.grid(row=1, column=0, sticky="ew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.source_list = ttk.Treeview(listing, columns=("repository", "download", "local", "task", "used", "size", "remote", "commit", "license"),
                                        selectmode="extended", height=5)
        self.source_list.heading("#0", text="中文名称")
        self.source_list.column("#0", width=px(210), minwidth=px(210), stretch=False)
        for name, title, width in (("repository", "来源仓库", 340), ("download", "下载状态", 100), ("local", "本地来源", 160), ("task", "本次任务", 90), ("used", "当前题库", 160),
                                    ("size", "本地跨版本合计", 130), ("remote", "远端下载大小", 110), ("commit", "固定 commit", 110),
                                    ("license", "观察到的声明", 140)):
            self.source_list.heading(name, text=title)
            self.source_list.column(name, width=px(width), minwidth=px(width), stretch=False)
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.source_list.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.source_list.xview)
        self.source_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.source_list.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for tag, color in (("good", COLORS.success), ("bad", COLORS.danger), ("working", COLORS.primary), ("unknown", COLORS.muted)):
            self.source_list.tag_configure(tag, foreground=color)
        self.source_list.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self._populate()
        row = FlowActions(body, theme)
        row.grid(row=2, column=0, sticky="ew", pady=px(SPACE.sm))
        field = ttk.Frame(row)
        ttk.Label(field, text="任务总时限（秒）").pack(side="left")
        self.timeout = window_var(self, "自动")
        self.timeout_entry = ttk.Entry(field, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=px(SPACE.sm))
        row.add(field)
        self.reload_button = row.add(ttk.Button(row, text="重新加载清单", command=self._reload))
        self.refresh_button = row.add(ttk.Button(row, text="刷新状态与空间", command=self.refresh))
        self.status = window_var(self, "选择来源后开始；普通重建复用已校验缓存。")
        MessageArea(body, self.status, theme, lines=2, muted=True).grid(row=3, column=0, sticky="ew")
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.grid(row=4, column=0, sticky="ew", pady=px(SPACE.sm))
        self.operation_hint = window_var(self)
        wrapped_label(body, theme, textvariable=self.operation_hint, style=MUTED).grid(row=5, column=0, sticky="ew")

        # A compact inspector and actions stay visible even when the table scrolls.
        footer = ttk.Frame(self)
        footer.grid(row=3, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        footer.columnconfigure(0, weight=1)
        self.local_detail = window_var(self, "请选择来源查看独立状态与空间。")
        self.task_detail = window_var(self)
        self.used_detail = window_var(self)
        self.local_label = wrapped_label(footer, theme, textvariable=self.local_detail)
        self.task_label = wrapped_label(footer, theme, textvariable=self.task_detail)
        self.used_label = wrapped_label(footer, theme, textvariable=self.used_detail)
        for index, label in enumerate((self.local_label, self.task_label, self.used_label)):
            label.grid(row=index, column=0, sticky="ew")
        actions = FlowActions(footer, theme)
        actions.grid(row=3, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.cancel_button = actions.add(ttk.Button(actions, text="取消构建", command=self._cancel))
        self.start_button = actions.add(ttk.Button(actions, text="构建 / 重新构建", style=PRIMARY, command=self._start))
        self.retry_button = actions.add(ttk.Button(actions, text="仅重试失败项", command=self._retry))
        self.delete_button = actions.add(ttk.Button(actions, text="删除题库", command=self._delete))
        self.history_button = actions.add(ttk.Button(actions, text="清理历史版本", command=lambda: self._plan_cleanup("history")))
        self.recover_button = actions.add(ttk.Button(actions, text="重试未完成清理", command=self._recover_cleanup))
        self.bind("<Map>", self._mapped, add="+")
        self.bind("<Destroy>", self._destroyed, add="+")
        self._sync_controls()
        self._request_scan()
        self.poll = self.after(100, self._drain)

    def _populate(self):
        for repo, source in self.sources.items():
            self.source_list.insert("", "end", iid=repo, text=source.display_name,
                values=(repo, "未下载", "待检查", "未参与", "待检查", "统计中", "未知", source.commit[:12], source.license), tags=("unknown",))

    def _request_scan(self):
        if self.closed:
            return
        if self.scanning or self.deleting:
            self._rescan = True
            return
        self.scanning = True
        self._next_scan = time.monotonic() + 2
        if self.inventory is not None:
            self.space_summary.set("后台刷新中（显示上次快照） · " + self._space_text)
        self._rescan = False
        sources, revision = dict(self.sources), self._revision
        generation = self.tasks.bank.generation if self.tasks.bank.busy else ""
        scanner, events, cancel = self._scanner, self._disk_events, self._scan_cancel
        def scan():
            try:
                events.put(("scan", revision, scanner.scan(sources, generation=generation, cancel=cancel)))
            except BuildCancelled:
                events.put(("scan_cancelled", revision, None))
            except Exception as error:
                events.put(("scan_error", revision, str(error)))
        self.scan_worker = threading.Thread(target=scan, daemon=True, name="bagu-bank-inventory")
        self.scan_worker.start()

    def _apply_inventory(self, inventory):
        self.inventory = inventory
        current = Path(inventory.current_directory).name if inventory.current_directory else "未知"
        self.summary.set(f"当前磁盘题库：{current}（运行中引擎应用状态另见任务提示）")
        totals = inventory.totals
        suffix = " · 统计不完整/变化中" if inventory.warnings else ""
        self._space_text = (f"来源缓存 {format_bytes(totals['cache'])} · 当前题库/运行中构建 {format_bytes(totals['current'])} · "
            f"历史版本 {format_bytes(totals['history'])} · 临时文件 {format_bytes(totals['temporary'])}\n"
            f"文件字节数，硬链接去重；共享/未归属元信息 {format_bytes(inventory.unassigned)} 已含于以上分类；未观测的远端大小为未知{suffix}")
        self.space_summary.set(self._space_text)
        self._render_rows()

    def _render_rows(self):
        for repo, source in self.sources.items():
            row = self.inventory.sources.get(repo) if self.inventory else None
            task = self.tasks.bank.sources.get(repo)
            task_text = TASK_LABELS[task.state] if task else "未参与"
            local = LOCAL_LABELS[row.local_state] if row else "待检查"
            tag = ("bad" if (task and task.state == "failed") or (row and row.local_state in {"invalid", "incomplete"}) else
                   "working" if task and task.state in {"waiting", "downloading"} else
                   "good" if row and row.local_state == "verified" else "unknown")
            if self.source_list.exists(repo):
                remote = self.tasks.bank.download_sizes.get(repo)
                downloaded = download_label(source, row.downloaded if row else False, task, self.tasks.bank.state == "building")
                self.source_list.item(repo, values=(repo, downloaded, local, task_text, row.used if row else "待检查",
                    format_bytes(row.local_bytes) if row else "统计中", format_bytes(remote) if remote else "未知", source.commit[:12], source.license), tags=(tag,))
        self._selection_changed()

    def _selection_changed(self):
        selected = self.source_list.selection()
        if not selected:
            self.local_detail.set("请选择来源查看独立状态与空间。")
            self.task_detail.set("")
            self.used_detail.set("")
        else:
            repo = selected[0]
            row = self.inventory.sources.get(repo) if self.inventory else None
            task = self.tasks.bank.sources.get(repo)
            remote = self.tasks.bank.download_sizes.get(repo)
            downloaded = download_label(self.sources[repo], row.downloaded if row else False, task, self.tasks.bank.state == "building")
            self.local_detail.set(f"{self.sources[repo].display_name}（{repo}） · 下载状态：{downloaded} · 本地：{LOCAL_LABELS[row.local_state] if row else '待检查'} · "
                                  f"跨版本占用 {format_bytes(row.local_bytes) if row else '统计中'} · "
                                  f"下载大小：{'本次响应 ' + format_bytes(remote) if remote else '未知'}")
            self.local_label.configure(foreground=(COLORS.danger if row and row.local_state in {"invalid", "incomplete"} else
                                                  COLORS.success if row and row.local_state == "verified" else COLORS.muted))
            self.task_detail.set("本次任务：" + (f"{TASK_LABELS[task.state]} · {task.stage} · 重试 {max(0, task.attempt - 1)} 次" if task else "未参与"))
            self.task_label.configure(foreground=(COLORS.danger if task and task.state == "failed" else
                COLORS.success if task and task.state == "completed" else
                COLORS.primary if task and task.state == "downloading" else COLORS.muted))
            self.used_detail.set("当前题库：" + (row.used if row else "正在检查"))
            self.used_label.configure(foreground=COLORS.success if row and row.used == "当前版本使用" else COLORS.muted)
        if hasattr(self, "start_button"):
            self._sync_controls()

    def _sync_controls(self):
        if self.closed:
            return
        task = self.tasks.bank
        locked = task.busy or self.deleting
        selected = bool(self.source_list.selection())
        self.start_button.state(["disabled"] if locked or not selected else ["!disabled"])
        self.start_button.configure(text="构建中…" if task.busy else "构建 / 重新构建")
        self.delete_button.state(["!disabled"] if selected and not locked else ["disabled"])
        self.history_button.state(["disabled"] if locked else ["!disabled"])
        self.recover_button.state(["disabled"] if locked or not (self.project_root / "data" / "source-deletion.json").is_file() else ["!disabled"])
        failures = any(s.state == "failed" for s in task.sources.values())
        self.retry_button.state(["!disabled"] if failures and not locked else ["disabled"])
        self.cancel_button.state(["!disabled"] if task.busy and not task.cleaning and not task.cancel.is_set() else ["disabled"])
        for widget in (self.timeout_entry, self.reload_button, self.source_list):
            widget.state(["disabled"] if locked else ["!disabled"])
        hint = ("删除准备/执行中：先校验保留内容并安全切换，关联作答与题目快照也将删除。" if self.deleting else
                "构建进行中，可取消；隐藏页面不取消任务。" if task.busy else
                "新题库已落盘，等待引擎在安全边界应用。" if task.state == "built" else
                "新题库已应用。" if task.state == "applied" else
                "失败：" + task.error if task.state == "failed" else
                "已取消，已完成缓存保留。" if task.state == "cancelled" else "选择来源后可构建或删除；删除涵盖全部本地版本和关联学习记录。")
        set_text(self.operation_hint, hint)

    def _start(self):
        self._start_sources(list(self.source_list.selection()))

    def _retry(self):
        task = self.tasks.bank
        failed = [repo for repo, status in task.sources.items() if status.state == "failed"]
        if failed:
            self._start_sources(task.repositories, versions=task.versions, retry_failed=failed)

    def _start_sources(self, repositories, *, versions=None, retry_failed=None):
        if self.closed or self.tasks.bank.busy or self.deleting or not repositories:
            return
        try:
            timeout = None if self.timeout.get().strip() in {"", "自动"} else float(self.timeout.get())
            if timeout is not None:
                BuildControl(timeout=timeout)
        except ValueError:
            self.status.set("任务总时限须为“自动”或有效正数。")
            return
        versions = versions or {repo: self.sources[repo].commit for repo in repositories}
        self.tasks.start_build(self.service, repositories, timeout=timeout, versions=versions, retry_failed=retry_failed)
        self._request_scan()
        self._sync_controls()

    def _cancel(self):
        self.tasks.cancel_build()
        self._sync_controls()

    def _delete(self):
        self._plan_cleanup("sources")

    def _plan_cleanup(self, kind):
        if self.closed or self.tasks.bank.busy or self.deleting:
            return
        selected = list(self.source_list.selection()) if kind == "sources" else []
        if kind == "sources" and not selected:
            return
        self.deleting = True
        self.tasks.bank.cleaning = True
        self._revision += 1
        self._scan_cancel.set()
        scan_worker = getattr(self, "scan_worker", None)
        protected = tuple(self.protected_versions())
        events, cleanup = self._disk_events, self.cleanup
        def prepare():
            try:
                if scan_worker:
                    scan_worker.join()
                plan = cleanup.plan(kind, selected, protected=protected)
                events.put(("cleanup_plan", 0, plan))
            except Exception as error:
                events.put(("cleanup_error", 0, str(error)))
        self.worker = threading.Thread(target=prepare, daemon=True, name="bagu-bank-cleanup-plan")
        self.worker.start()
        self._sync_controls()

    def _confirm_cleanup(self, plan):
        learning = getattr(plan, "learning", {})
        legacy_unknown = sum(rewrite.unknown for rewrite in getattr(plan, "legacy", ()))
        if not plan.targets and not plan.requires_switch and not legacy_unknown and not any(learning.get(key) for key in ("affected", "unknown", "affected_states", "unknown_states")):
            self.finish_cleanup(error="没有可清理的对象；当前使用的版本已排除。")
            return
        import tkinter as tk
        window = ttk.Frame(self.view.body, padding=self.theme.px(SPACE.sm))
        window.grid(row=6, column=0, sticky="ew")
        self.confirmation = window
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        wrapped_label(window, self.theme, text="请核对删除对象、使用情况及预计释放空间", style=SECTION).grid(row=0, column=0, sticky="ew", padx=16, pady=8)
        content = ttk.Frame(window)
        content.grid(row=1, column=0, sticky="nsew", padx=16)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        text = tk.Text(content, wrap="word", width=1, height=10)
        scroll = ttk.Scrollbar(content, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        lines = ["所选来源：" + ("、".join(plan.repositories) or "独立清理历史版本"),
                 f"预计释放文件字节：{format_bytes(plan.estimated_bytes)}（共享硬链接只在最后一份引用删除时计入；未扣除新版本构建空间）"]
        lines += [f"本地来源版本：{repo} @ {commit}" for repo, commit in getattr(plan, "commits", ())]
        lines += [f"{target.label}\n  {target.relative}\n  当前使用：{'是，必须先切换新题库' if target.used else '否'}" for target in plan.targets]
        if plan.requires_switch:
            lines.append("将先构建并校验不含所选来源的新题库，运行中引擎切换成功后才清理；构建或切换失败保留原文件。")
        elif any(target.used for target in plan.targets):
            lines.append("运行中引擎仍在使用旧版本；先应用已校验的磁盘当前版本，再清理缓存。")
        if plan.last_source:
            lines.append("这是最后一个可用来源：将切换为空题库，后续需要重新下载并构建来源才能练习。")
        if plan.kind == "sources":
            lines.append(f"关联题目快照 {len(learning.get('affected', []))} 个，答题记录 {learning.get('attempts', 0)} 条。关联待答题、历史重练和学习状态也将删除，无法在应用内撤销。")
        else:
            lines.append("单独清理历史版本保留学习记录。")
        text.insert("1.0", "\n\n".join(lines))
        text.configure(state="disabled")
        actions = FlowActions(window, self.theme)
        actions.grid(row=2, column=0, sticky="ew", padx=16, pady=8)
        self.include_unknown = tk.BooleanVar(master=self, value=False)
        unknown = [*learning.get("unknown", []), *learning.get("unknown_states", [])]
        unknown += [None] * legacy_unknown
        if unknown:
            ttk.Checkbutton(window, text=f"额外确认：一并删除 {len(unknown)} 组来源不明的旧快照、备份、作答和学习状态（可能涉及其他来源）",
                variable=self.include_unknown).grid(row=3, column=0, sticky="ew")
        def cancel():
            window.destroy()
            self.finish_cleanup(error="已取消清理，文件未改动。")
        def confirm():
            if unknown and not self.include_unknown.get():
                self.status.set("来源不明的旧记录需要额外确认，否则不能完成彻底删除。")
                return
            include_unknown = self.include_unknown.get()
            window.destroy()
            self._execute_cleanup(plan, include_unknown=include_unknown)
        self.confirm_cancel = actions.add(ttk.Button(actions, text="取消", command=cancel))
        self.confirm_delete = actions.add(ttk.Button(actions, text="确认清理", command=confirm))
        self.confirm_text = text
        self.confirm_cancel.focus_set()

    def _execute_cleanup(self, plan, include_unknown=False):
        if self.on_cleanup:
            if self.on_cleanup(self.cleanup, plan, include_unknown) is False:
                self.finish_cleanup(error="当前有其他任务，未执行删除；请等待后重新确认。")
            return
        events, cleanup = self._disk_events, self.cleanup
        def execute():
            try:
                events.put(("cleanup_result", 0, cleanup.execute(plan, include_unknown=include_unknown)))
            except Exception as error:
                events.put(("cleanup_error", 0, str(error)))
        self.worker = threading.Thread(target=execute, daemon=True, name="bagu-bank-cleanup")
        self.worker.start()

    def _recover_cleanup(self):
        if self.tasks.bank.busy or self.deleting:
            return
        self.deleting = self.tasks.bank.cleaning = True
        self._scan_cancel.set()
        if self.on_cleanup:
            if self.on_cleanup(self.cleanup, None, False) is False:
                self.finish_cleanup(error="当前有其他任务，请等待后重试。")
            return
        events, cleanup = self._disk_events, self.cleanup
        def recover():
            try:
                from .source_deletion import SourceDeletion
                from .bank_cleanup import CleanupResult
                result = SourceDeletion(cleanup).recover()
                events.put(("cleanup_result", 0, result or CleanupResult([], [])))
            except Exception as error:
                events.put(("cleanup_error", 0, str(error)))
        self.worker = threading.Thread(target=recover, daemon=True, name="bagu-bank-recovery")
        self.worker.start()

    def finish_cleanup(self, result=None, error=None):
        self.deleting = False
        self.tasks.bank.cleaning = False
        self._scan_cancel = threading.Event()
        self._revision += 1
        self.status.set(result.message if result is not None else f"清理未完成：{error}")
        self._rescan = True
        self._sync_controls()

    def _reload(self):
        if self.closed or self.tasks.bank.busy or self.deleting:
            return
        try:
            sources = load_sources(self.service.manifest)
        except Exception as error:
            self.status.set(f"读取清单失败：{error}。请修复清单后重试。")
            return
        selected = set(self.source_list.selection())
        self.sources = sources
        self._revision += 1
        self.inventory = None
        self.source_list.delete(*self.source_list.get_children())
        self._populate()
        self.source_list.selection_set(list(selected.intersection(sources)))
        self.refresh()

    def refresh(self):
        self._request_scan()
        self._render_rows()

    def _mapped(self, event):
        if event.widget is self:
            self.refresh()

    def _drain(self):
        if self.closed:
            return
        cancel_timer(self, self.poll)
        changed = self.tasks.drain_bank()
        task = self.tasks.bank
        source_states = (tuple((repo, s.state, s.stage, s.attempt) for repo, s in task.sources.items()),
                         tuple(task.download_sizes.items()))
        if changed or task.state != self._last_task_state or source_states != self._last_source_states:
            self._revision += 1
            self._last_task_state = task.state
            self._last_source_states = source_states
            self._request_scan()
            self._render_rows()
            if changed and task.state == "built" and self.on_built:
                self.on_built(task.result)
        if task.busy:
            self.status.set(f"进行中 · {task.stage}：{task.message}")
            total = task.progress.get("total")
            if total:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=min(100, 100 * task.progress["completed"] / total))
            elif str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        elif changed:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=100 if task.state == "built" else 0)
            self.status.set(task.error or "构建完成；当前使用情况正在后台刷新。")
        try:
            while True:
                kind, revision, value = self._disk_events.get_nowait()
                if kind == "cleanup_plan":
                    self._confirm_cleanup(value)
                elif kind == "cleanup_result":
                    self.finish_cleanup(result=value)
                elif kind == "cleanup_error":
                    self.finish_cleanup(error=value)
                else:
                    self.scanning = False
                    self._next_scan = time.monotonic() + 2
                    if revision == self._revision:
                        if kind == "scan":
                            self._apply_inventory(value)
                        elif kind == "scan_error":
                            self.inventory = None
                            self._render_rows()
                            self.space_summary.set(f"空间统计失败：{value}；请刷新重试，不代表占用为零。")
        except queue.Empty:
            pass
        if not self.scanning and not self.deleting and (self._rescan or (self.winfo_ismapped() and time.monotonic() >= self._next_scan)):
            self._request_scan()
        self._sync_controls()
        self.poll = self.after(100, self._drain)

    def focus_target(self):
        return self.source_list

    def _destroyed(self, event=None):
        if event is None or event.widget is self:
            self.closed = True
            self._scan_cancel.set()
            cancel_timer(self, getattr(self, "poll", None))
