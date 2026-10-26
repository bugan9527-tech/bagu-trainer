"""Reusable project (watch-list) configuration panel.

Lists configured projects with path + availability detail, and supports add /
remove.  Persistence reuses ``startup.set_projects`` so the exact validation
rules (absolute path, existing directory, dedupe, atomic write) stay in one
place.  The host decides when to apply the change to the monitor's baseline;
the panel only edits and persists the list.

Wide workspaces show the list and detail side by side; narrow workspaces stack
the detail below the list.
"""
import os
from pathlib import Path

from tkinter import ttk

from .ui_layout import ScrollableFrame, FlowActions, wrapped_label
from .ui_state import window_var, set_text
from .ui_copy import ReadOnlyText
from .ui_theme import SPACE, PRIMARY, DANGER, SECTION, MUTED, CAPTION


def _key(path):
    # UI refresh must not resolve symlinks or touch a disconnected network drive.
    return os.path.normcase(os.path.abspath(str(path)))


STATES = {"pending": "待应用", "applying": "应用中", "initializing": "初始化中",
          "watching": "监听中", "paused": "已暂停", "unavailable": "目录不可用", "not_ready": "未就绪"}


class ProjectsPanel(ttk.Frame):
    def __init__(self, parent, theme, *, on_changed=None, on_saved=None):
        super().__init__(parent)
        self.theme = theme
        px = theme.px
        self.on_changed = on_changed
        self.on_saved = on_saved
        self.projects = []
        self.dirty = False
        self.busy = False
        self.save_error = ""
        self.wide = False
        self.runtime_snapshot = {"projects": {}, "applied_roots": [], "ready": False}
        self._snapshot_order = (-1, -1)
        self.applying_request = None
        self.apply_error = ""
        self._row_ids = {}

        from . import config
        cfg = config.load_config()
        roots = cfg.get("project_roots")
        self.projects = [str(r) for r in roots if isinstance(r, str)] if isinstance(roots, list) else []

        self.saved_projects = list(self.projects)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        wrapped_label(self, theme, text="选择希望监听代码改动的项目文件夹，可添加多个。已不存在的本地目录会自动移除，学习记录保留。",
                      style=MUTED).grid(row=0, column=0, sticky="ew", pady=px((0, SPACE.sm)))
        self.hint = window_var(self, "更改监听项目后需点击“保存并应用”才影响监听范围。")
        wrapped_label(self, theme, textvariable=self.hint, style=SECTION).grid(row=1, column=0, sticky="ew", pady=px((0, SPACE.sm)))

        self.workspace = ttk.Frame(self)
        self.workspace.grid(row=2, column=0, sticky="nsew")
        self.workspace.rowconfigure(0, weight=1)
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.bind("<Configure>", self._resize, add="+")

        self.list_frame = ttk.Frame(self.workspace)
        self.list_frame.grid(row=0, column=0, sticky="nsew")
        self.list_frame.columnconfigure(0, weight=1)
        self.list_frame.rowconfigure(1, weight=1)
        wrapped_label(self.list_frame, theme, text="监听项目", style=CAPTION).grid(row=0, column=0, sticky="ew")
        listing = ttk.Frame(self.list_frame)
        listing.grid(row=1, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("path", "status"), show="headings",
                                 selectmode="extended", height=5)
        self.tree.heading("path", text="项目路径")
        self.tree.heading("status", text="监听状态")
        self.tree.column("path", width=px(360), minwidth=px(140), stretch=True)
        self.tree.column("status", width=px(120), minwidth=px(120), stretch=False, anchor="center")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vertical.set)

        self.detail_frame = ttk.Frame(self.workspace)
        self.detail_frame.columnconfigure(0, weight=1)
        wrapped_label(self.detail_frame, theme, text="项目详情", style=CAPTION).grid(row=0, column=0, sticky="ew")
        self.detail = window_var(self, "尚未添加项目。")
        self.detail_text = ReadOnlyText(self.detail_frame, theme, textvariable=self.detail)
        self.detail_text.grid(row=1, column=0, sticky="new")

        actions = FlowActions(self, theme)
        actions.grid(row=3, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.add_button = actions.add(ttk.Button(actions, text="添加项目文件夹", style=PRIMARY, command=self._add))
        self.remove_button = actions.add(ttk.Button(actions, text="移除选中目录", style=DANGER, command=self._remove))
        self.save_button = actions.add(ttk.Button(actions, text="保存并应用", command=self._save))

        self._render()

    def _resize(self, event):
        if event.widget is not self.workspace:
            return
        wide = event.width >= self.theme.px(720)
        if wide != self.wide:
            self.wide = wide
            self._layout()

    def _layout(self):
        split = self.wide
        self.workspace.columnconfigure(0, weight=1, uniform="projects" if split else "")
        self.workspace.columnconfigure(1, weight=1 if split else 0, uniform="projects" if split else "")
        if split:
            self.list_frame.grid(row=0, column=0, sticky="nsew")
            self.detail_frame.grid(row=0, column=1, sticky="nsew",
                                   padx=(self.theme.px(SPACE.lg), 0))
        else:
            self.list_frame.grid(row=0, column=0, sticky="nsew")
            self.detail_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 0), pady=(self.theme.px(SPACE.lg), 0))
            self.workspace.rowconfigure(1, weight=0)

    def set_runtime_snapshot(self, snapshot):
        order = (snapshot.get("version", 0), snapshot.get("sequence", 0))
        if order < self._snapshot_order:
            return
        self._snapshot_order = order
        self.runtime_snapshot = snapshot
        self._render()

    def application_finished(self, request, roots, snapshot, error=""):
        self.set_runtime_snapshot(snapshot)
        if request != self.applying_request:
            return
        self.applying_request = None
        self.apply_error = error
        self._render()

    def _state(self, path):
        key = _key(path)
        runtime_key = self.runtime_snapshot.get("aliases", {}).get(key, key)
        record = self.runtime_snapshot.get("projects", {}).get(runtime_key)
        if key not in {_key(p) for p in self.saved_projects}:
            return {**(record or {}), "state": "pending"}
        if self.applying_request is not None:
            return {**(record or {}), "state": "applying"}
        if record is not None:
            if not self.runtime_snapshot.get("ready"):
                return {**record, "state": "not_ready", "activity": ""}
            return record
        applied = {_key(p) for p in self.runtime_snapshot.get("applied_roots", [])}
        return {"state": "initializing" if self.runtime_snapshot.get("ready") and runtime_key in applied else "not_ready"}

    def _render(self):
        # Stable row ids: normal status refresh only touches changed cells.
        wanted = {_key(path): path for path in self.projects}
        for key in set(self._row_ids) - set(wanted):
            self.tree.delete(self._row_ids.pop(key))
        for index, (key, path) in enumerate(wanted.items()):
            state = STATES[self._state(path)["state"]]
            iid = self._row_ids.get(key)
            if iid is None:
                iid = self.tree.insert("", index, values=(path, state))
                self._row_ids[key] = iid
            else:
                if self.tree.set(iid, "path") != path:
                    self.tree.set(iid, "path", path)
                if self.tree.set(iid, "status") != state:
                    self.tree.set(iid, "status", state)
                if self.tree.index(iid) != index:
                    self.tree.move(iid, "", index)
        self._sync_detail()
        self._sync_controls()

    def _selection_changed(self, _event=None):
        self._sync_detail()
        self._sync_controls()

    def _sync_detail(self):
        selection = self.tree.selection()
        if not selection:
            set_text(self.detail, "选择一个项目查看详情；未选择时移除不可用。" if self.projects else "尚未添加项目。")
            return
        path = self.tree.set(selection[0], "path")
        record = self._state(path)
        from datetime import datetime, timedelta, timezone
        checked = record.get("last_success_at")
        when = (datetime.fromtimestamp(checked, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                + "（北京时间）") if checked is not None else "尚未检查"
        note = record.get("reason") or ""
        runtime_key = self.runtime_snapshot.get("aliases", {}).get(_key(path), _key(path))
        if self.apply_error and runtime_key not in self.runtime_snapshot.get("projects", {}):
            note = "配置已保存，但尚未成功纳入后台监听。"
        activity = record.get("activity")
        text = f"完整路径：{path}\n当前状态：{STATES[record['state']]}\n最近成功检查：{when}"
        if note:
            text += "\n说明：" + note
        if activity:
            text += "\n活动：" + activity
        set_text(self.detail, text)

    def _sync_controls(self):
        locked = self.busy
        self.add_button.state(["disabled"] if locked else ["!disabled"])
        selected = bool(self.tree.selection())
        self.remove_button.state(["!disabled"] if selected and not locked else ["disabled"])
        self.save_button.state(["!disabled"] if (self.dirty or self.apply_error) and not locked else ["disabled"])
        if locked:
            hint = "正在保存并应用监听范围…"
        elif self.save_error:
            hint = self.save_error
        elif self.apply_error:
            active = "、".join(self.runtime_snapshot.get("applied_roots", [])) or "无"
            hint = self.apply_error + " 仍生效的后台范围：" + active + "。可点击“重试应用”。"
        elif self.dirty:
            hint = "有未保存的更改，点击“保存并应用”生效。"
        elif self.applying_request is not None:
            hint = "配置已保存，等待后台确认应用；当前监听范围以后台确认为准。"
        else:
            hint = "更改监听项目后需点击“保存并应用”才影响监听范围。"
        self.save_button.configure(text="重试应用" if self.apply_error and not self.dirty else "保存并应用")
        set_text(self.hint, hint)

    def _mark_dirty(self):
        self.dirty = True
        self.save_error = ""
        self._render()
        if self.on_changed is not None:
            self.on_changed()

    def _add(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self, title="选择要监听的代码项目")
        if not path:
            return
        resolved = Path(path).resolve()
        if _key(resolved) not in {_key(p) for p in self.projects}:
            self.projects.append(str(resolved))
            self._render()
            self._mark_dirty()

    def _remove(self):
        removed = {self.tree.set(iid, "path") for iid in self.tree.selection()}
        self.projects = [path for path in self.projects if path not in removed]
        self._render()
        self._mark_dirty()

    def _save(self):
        if self.busy or not (self.dirty or self.apply_error):
            return
        self.save_error = ""
        self.busy = True
        self._sync_controls()
        try:
            from . import startup
            startup.set_projects(self.projects)
        except Exception as error:
            self.busy = False
            reason = "请选择至少一个代码项目。" if not self.projects else "请检查项目目录及配置写入权限。"
            self.save_error = f"保存失败（{type(error).__name__}）：{reason} 已保存的配置未更改，请修正后重试。"
            self._sync_controls()
            return
        self.busy = False
        self.dirty = False
        self.saved_projects = list(self.projects)
        self.apply_error = ""
        if self.on_saved is not None:
            self.applying_request = self.on_saved(list(self.projects))
        self._render()

    def focus_target(self):
        return self.tree

    def forget_removed_projects(self, removed):
        """Reflect worker cleanup while preserving any unsaved project edits."""
        keys = {_key(path) for path in removed}
        self.projects = [path for path in self.projects if _key(path) not in keys]
        self.saved_projects = [path for path in self.saved_projects if _key(path) not in keys]
        self._render()

    def current_projects(self):
        return list(self.projects)
