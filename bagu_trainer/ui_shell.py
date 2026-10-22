"""Persistent desktop chrome. Navigation callbacks remain application owned.

Only frames parented to content_host may be displayed here. Hiding a page
does not destroy its widgets, cancel a task or change monitor state.

The sidebar uses unified line icons (no emoji), a clear selected/hover/focus
treatment, and collapses to an icon-only rail in narrow windows — with a hover
label and an expand toggle so the text hints are always reachable.
"""
from tkinter import ttk

from .ui_layout import ScrollableFrame, wrapped_label
from .ui_icons import make_icon
from .ui_theme import COLORS, SPACE, TITLE


class AppShell(ttk.Frame):
    PAGES = (("dashboard", "工作台"), ("practice", "练习中心"),
             ("history", "答题记录"), ("projects", "监听项目"),
             ("bank", "题库管理"), ("model", "模型配置"), ("settings", "应用设置"))
    RAIL_WIDTH = 80   # leave room for the vertical scrollbar at high DPI
    SIDEBAR_WIDTH = 200

    def __init__(self, root, theme, navigate, status):
        super().__init__(root)
        self.theme, self.current_page, self.current_frame = theme, None, None
        self.badges = {name: 0 for name, _ in self.PAGES}
        self.collapsed = False
        self.focus_timer = None
        self.bind("<Destroy>", self._destroyed, add="+")
        self.pack(fill="both", expand=True)
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)
        px = theme.px
        self.sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=px(self.SIDEBAR_WIDTH))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(1, weight=1)
        brand_row = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        brand_row.grid(row=0, column=0, sticky="ew", padx=px(SPACE.lg), pady=px(SPACE.lg))
        self.brand = wrapped_label(brand_row, theme, text="AI 八股训练器", style="Brand.TLabel")
        self.brand.pack(side="left")
        self.brand_mark = wrapped_label(brand_row, theme, text="八股", style="Brand.TLabel")

        navigation = self.navigation = ScrollableFrame(self.sidebar, theme)
        navigation.grid(row=1, column=0, sticky="nsew", padx=px(SPACE.sm))
        navigation.body.configure(style="Sidebar.TFrame")
        navigation.canvas.configure(background=COLORS.sidebar)
        self.buttons = {}
        for row, (name, title) in enumerate(self.PAGES):
            parent = self.sidebar if name == "settings" else navigation.body
            button = ttk.Button(parent, style="Navigation.TButton",
                                command=lambda key=name: navigate(key), takefocus=True)
            if name == "settings":
                button.grid(row=2, column=0, sticky="ew", padx=px(SPACE.sm))
            else:
                button.grid(row=row, column=0, sticky="ew", pady=px((0, SPACE.xs)))
            self.buttons[name] = button
            button.bind("<Enter>", lambda _event, n=name: self._show_hint(n))
            button.bind("<Leave>", lambda _event: self._clear_hint())
            button.bind("<FocusIn>", lambda _event, n=name: self._show_hint(n))
            button.bind("<FocusOut>", lambda _event: self._clear_hint())
            for key, direction in (("Up", -1), ("Down", 1), ("Home", "first"), ("End", "last")):
                button.bind(f"<{key}>", lambda _event, origin=name, step=direction: self._move_focus(origin, step))
            button.bind("<Return>", lambda _event, widget=button: self._activate(widget))
        self._update_selection()

        self.global_status = wrapped_label(self.sidebar, theme, textvariable=status, style="Sidebar.TLabel")
        self.global_status.grid(row=3, column=0, sticky="ew", padx=px(SPACE.lg), pady=px(SPACE.lg))
        # Hover label shown only while collapsed, so the icon rail keeps a text hint.
        self.hover_hint = wrapped_label(self.sidebar, theme, text="", style="Sidebar.TLabel")
        self.hover_hint.grid(row=4, column=0, sticky="ew", padx=px(SPACE.sm), pady=px((0, SPACE.sm)))
        self.hover_hint.grid_remove()
        self.collapse_button = ttk.Button(self.sidebar, text="«", style="Navigation.TButton", width=1,
                                          command=self.toggle_collapse, takefocus=True)
        self.collapse_button.grid(row=5, column=0, sticky="ew", padx=px(SPACE.sm), pady=px((0, SPACE.sm)))

        ttk.Separator(self, orient="vertical").grid(row=0, column=1, sticky="ns")
        workspace = ttk.Frame(self)
        workspace.grid(row=0, column=2, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(3, weight=1)
        self.heading = ttk.Label(workspace, style=TITLE, takefocus=True, padding=px((SPACE.xl, SPACE.lg)))
        self.heading.grid(row=0, column=0, sticky="ew")
        self.heading.bind("<FocusIn>", lambda _: self.heading.configure(style="Focused.Page.TLabel"))
        self.heading.bind("<FocusOut>", lambda _: self.heading.configure(style=TITLE))
        ttk.Separator(workspace).grid(row=1, column=0, sticky="ew")
        self.practice_notice = ttk.Button(workspace, text="有练习待处理 · 进入练习中心",
                                          command=lambda: navigate("practice"))
        self.content_host = ttk.Frame(workspace)
        self.content_host.grid(row=3, column=0, sticky="nsew")
        self.bind("<Configure>", self._resize, add="+")

    # -- page switching -------------------------------------------------------
    def show_page(self, name, frame, *, focus=None):
        if frame.master is not self.content_host:
            raise ValueError("Page must be created in content_host")
        if (self.current_frame is not None and self.current_frame is not frame
                and self.current_frame.winfo_exists()):
            self.current_frame.pack_forget()
        frame.pack(fill="both", expand=True)
        self.current_page, self.current_frame = name, frame
        self.heading.configure(text=dict(self.PAGES)[name])
        self._update_selection()
        if self.focus_timer is not None:
            self.after_cancel(self.focus_timer)
        # A restored frame is not mapped until geometry propagation finishes.
        # Match the page again so a fast second navigation cannot steal focus.
        target = focus or self.heading
        def restore_focus():
            self.focus_timer = None
            if self.current_page == name and target.winfo_exists():
                target.focus_set()
        self.focus_timer = self.after_idle(restore_focus)

    def _destroyed(self, event):
        if event.widget is self and self.focus_timer is not None:
            self.after_cancel(self.focus_timer)
            self.focus_timer = None

    def _update_selection(self):
        for key, button in self.buttons.items():
            title = dict(self.PAGES)[key]
            badge = self.badges.get(key, 0)
            selected = key == self.current_page
            color = COLORS.primary if selected else COLORS.muted
            image = make_icon(self.theme, key, color, size=20)
            button.configure(image=image)
            if self.collapsed:
                button.configure(text="", compound="image")
            else:
                suffix = f"  [{badge}]" if badge else ""
                button.configure(text=title + suffix, compound="left")
            button.configure(style="Selected.Navigation.TButton" if selected else "Navigation.TButton")

    # -- badges / notices -----------------------------------------------------
    def set_badge(self, name, count):
        """Update a navigation badge (e.g. the practice pending count)."""
        self.badges[name] = max(0, int(count))
        self._update_selection()

    def show_practice_notice(self, count):
        if count:
            self.practice_notice.configure(text=f"待答题 {count} · 点击进入练习中心")
            self.practice_notice.grid(row=2, column=0, sticky="ew", padx=self.theme.px(SPACE.lg),
                                      pady=self.theme.px(SPACE.sm))
        else:
            self.practice_notice.grid_remove()

    def lock_navigation(self, locked):
        self.practice_notice.state(["disabled"] if locked else ["!disabled"])
        self.collapse_button.state(["disabled"] if locked else ["!disabled"])
        for button in self.buttons.values():
            button.state(["disabled"] if locked else ["!disabled"])

    # -- collapse -------------------------------------------------------------
    def toggle_collapse(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed):
        self.collapsed = bool(collapsed)
        px = self.theme.px
        self.collapse_button.configure(text="»" if self.collapsed else "«")
        if self.collapsed:
            self.sidebar.configure(width=px(self.RAIL_WIDTH))
            self.navigation.grid_configure(padx=0)
            self.buttons["settings"].grid_configure(padx=0)
            self.brand.pack_forget()
            self.brand_mark.pack(side="left")
            self.global_status.grid_remove()
        else:
            self.sidebar.configure(width=px(self.SIDEBAR_WIDTH))
            self.navigation.grid_configure(padx=px(SPACE.sm))
            self.buttons["settings"].grid_configure(padx=px(SPACE.sm))
            self.brand.pack(side="left")
            self.brand_mark.pack_forget()
            self.global_status.grid(row=3, column=0, sticky="ew", padx=px(SPACE.lg), pady=px(SPACE.lg))
            self.hover_hint.grid_remove()
        self._update_selection()

    def _show_hint(self, name):
        if self.collapsed:
            badge = self.badges.get(name, 0)
            self.hover_hint.configure(text=dict(self.PAGES)[name] + (f" [{badge}]" if badge else ""))
            self.hover_hint.grid(row=4, column=0, sticky="ew", padx=self.theme.px(SPACE.sm),
                                 pady=self.theme.px((0, SPACE.sm)))

    def _clear_hint(self):
        if self.collapsed:
            self.hover_hint.grid_remove()

    # -- keyboard navigation --------------------------------------------------
    def _move_focus(self, origin, direction):
        enabled = [key for key, button in self.buttons.items() if not button.instate(["disabled"])]
        if not enabled:
            return "break"
        if direction in {"first", "last"}:
            key = enabled[0 if direction == "first" else -1]
        else:
            index = enabled.index(origin) if origin in enabled else 0
            key = enabled[(index + direction) % len(enabled)]
        button = self.buttons[key]
        if button.master is self.navigation.body:
            self.navigation.reveal(button)
        button.focus_set()
        return "break"

    @staticmethod
    def _activate(button):
        button.invoke()
        return "break"

    def _resize(self, event):
        if event.widget is self and not self.collapsed:
            width = self.theme.px(144 if event.width < self.theme.px(900) else self.SIDEBAR_WIDTH)
            if int(self.sidebar.cget("width")) != width:
                self.sidebar.configure(width=width)
