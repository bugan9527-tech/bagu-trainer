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
from .ui_theme import SPACE, PRIMARY, DANGER, SECTION, MUTED, CAPTION


def _detail(path):
    """Read-only availability note for a configured project."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return "目录不可用或已移动"
    if not resolved.is_dir():
        return "不是有效目录"
    try:
        with os.scandir(resolved):
            pass
        return "可读取"
    except OSError:
        return "不可读取（权限或已断开）"


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
        self.wide = False

        from . import config
        cfg = config.load_config()
        roots = cfg.get("project_roots")
        self.projects = [str(r) for r in roots if isinstance(r, str)] if isinstance(roots, list) else []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        wrapped_label(self, theme, text="选择希望监听代码改动的项目文件夹，可添加多个。失效目录可先移除，再选择新位置。",
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
        self.listbox = self._tk_listbox(listing)
        self.listbox.bind("<<ListboxSelect>>", lambda _event: self._sync_detail())
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.listbox.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.listbox.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.listbox.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        self.detail_frame = ttk.Frame(self.workspace)
        self.detail_frame.columnconfigure(0, weight=1)
        wrapped_label(self.detail_frame, theme, text="项目详情", style=CAPTION).grid(row=0, column=0, sticky="ew")
        self.detail = window_var(self, "尚未添加项目。")
        wrapped_label(self.detail_frame, theme, textvariable=self.detail, style=MUTED).grid(row=1, column=0, sticky="nw")

        actions = FlowActions(self, theme)
        actions.grid(row=3, column=0, sticky="ew", pady=px((SPACE.sm, 0)))
        self.add_button = actions.add(ttk.Button(actions, text="添加项目文件夹", style=PRIMARY, command=self._add))
        self.remove_button = actions.add(ttk.Button(actions, text="移除选中目录", style=DANGER, command=self._remove))
        self.save_button = actions.add(ttk.Button(actions, text="保存并应用", command=self._save))

        self._render()

    def _tk_listbox(self, parent):
        import tkinter as tk
        box = tk.Listbox(parent, height=5, width=1, exportselection=False)
        self.theme.listbox(box)
        return box

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

    def _render(self):
        self.listbox.configure(state="normal")
        self.listbox.delete(0, "end")
        for project in self.projects:
            self.listbox.insert("end", project)
        self._sync_detail()
        self._sync_controls()

    def _sync_detail(self):
        selection = self.listbox.curselection()
        if not selection:
            self.detail.set("选择一个项目查看详情；未选择时移除不可用。" if self.projects else "尚未添加项目。")
            return
        path = self.projects[selection[0]]
        self.detail.set(f"路径：{path}\n状态：{_detail(path)}")

    def _sync_controls(self):
        locked = self.busy
        self.add_button.state(["disabled"] if locked else ["!disabled"])
        selected = bool(self.listbox.curselection())
        self.remove_button.state(["!disabled"] if selected and not locked else ["disabled"])
        self.save_button.state(["!disabled"] if self.dirty and not locked else ["disabled"])
        self.hint.set("正在保存并应用监听范围…" if locked else
                      ("有未保存的更改，点击“保存并应用”生效。" if self.dirty else
                       "更改监听项目后需点击“保存并应用”才影响监听范围。"))

    def _mark_dirty(self):
        self.dirty = True
        self._sync_controls()
        if self.on_changed is not None:
            self.on_changed()

    def _add(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self, title="选择要监听的代码项目")
        if not path:
            return
        resolved = Path(path).resolve()
        if str(resolved) not in self.projects:
            self.projects.append(str(resolved))
            self._render()
            self._mark_dirty()

    def _remove(self):
        for index in reversed(self.listbox.curselection()):
            del self.projects[index]
        self._render()
        self._mark_dirty()

    def _save(self):
        if self.busy or not self.dirty:
            return
        self.busy = True
        self._sync_controls()
        try:
            from . import startup
            startup.set_projects(self.projects)
        except Exception as error:
            self.busy = False
            self.hint.set(f"保存失败（{type(error).__name__}）：{error} 列表未更改。")
            self._sync_controls()
            return
        self.busy = False
        self.dirty = False
        self._sync_controls()
        if self.on_saved is not None:
            self.on_saved(self.projects)

    def focus_target(self):
        return self.listbox

    def current_projects(self):
        return list(self.projects)
