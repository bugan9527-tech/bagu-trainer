"""Shared light desktop theme. Call apply_theme before creating window contents.

No Tk interpreter is created at import time. A Toplevel/embedded panel shares
its root's theme; a new root gets fresh bootstrap images and named fonts.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    background: str = "#F5F7FA"
    surface: str = "#FFFFFF"
    text: str = "#202B3C"
    muted: str = "#526176"
    border: str = "#8795A8"
    primary: str = "#2563EB"
    primary_hover: str = "#1D4ED8"
    primary_pressed: str = "#1E40AF"
    selection: str = "#E8EFFD"
    sidebar: str = "#EEF2F8"
    danger: str = "#B42318"
    success: str = "#166534"
    danger_hover: str = "#912018"
    disabled: str = "#E4E9F0"
    disabled_text: str = "#667085"


@dataclass(frozen=True)
class Spacing:
    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32


@dataclass(frozen=True)
class Metrics:
    button_width: int = 8  # text units, not pixels
    button_padding: tuple = (16, 8)
    input_padding: tuple = (10, 7)
    row_height: int = 36
    scrollbar_width: int = 14
    progress_thickness: int = 6


COLORS = Palette()
SPACE = Spacing()
METRICS = Metrics()
FONT_SIZES = {"caption": 9, "body": 10, "section": 14, "title": 18}
FONT_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Noto Sans CJK SC",
                 "Microsoft JhengHei UI", "SimHei", "SimSun")
PRIMARY = "Primary.TButton"
SECONDARY = "Secondary.TButton"
DANGER = "Danger.TButton"
TITLE = "Title.TLabel"
SECTION = "Section.TLabel"
MUTED = "Muted.TLabel"
CAPTION = "Caption.TLabel"
STATUS = "Status.TLabel"


@dataclass
class DesktopTheme:
    root: object
    style: object
    fonts: dict
    scale: float

    @property
    def compact(self):
        _, top, _, bottom = work_area(self.root)
        return (bottom - top) / self.scale < 720

    def px(self, value):
        """96-DPI layout units → Tk pixels; font sizes stay in points."""
        if isinstance(value, (tuple, list)):
            return tuple(self.px(item) for item in value)
        return round(value * self.scale)

    def window(self, window, size, minimum):
        # Use the parent's monitor work area (excluding the taskbar), rather
        # than the primary screen size. Reserve room for native window chrome.
        left, top, right, bottom = work_area(window)
        margin = self.px(16)
        limit = (max(1, right - left - margin * 2),
                 max(1, bottom - top - margin * 2 - self.px(40)))
        width, height = (min(self.px(v), cap) for v, cap in zip(size, limit))
        parent = window.master
        if parent is not None and parent.winfo_viewable() and parent.winfo_toplevel().state() != "iconic":
            parent = parent.winfo_toplevel()
            cx = parent.winfo_rootx() + parent.winfo_width() // 2
            cy = parent.winfo_rooty() + parent.winfo_height() // 2
        else:
            cx, cy = (left + right) // 2, (top + bottom) // 2
        x = max(left + margin, min(cx - width // 2, right - width - margin))
        y = max(top + margin, min(cy - height // 2, bottom - height - self.px(40) - margin))
        # Tk's +negative offset means "from the opposite edge". Resolve
        # absolute virtual-desktop coordinates via wm's signed position form.
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(*(min(self.px(v), cap) for v, cap in zip(minimum, limit)))

    def listbox(self, widget):
        widget.configure(background=COLORS.surface, foreground=COLORS.text,
                         selectbackground=COLORS.selection, selectforeground=COLORS.text,
                         highlightbackground=COLORS.border, highlightcolor=COLORS.primary,
                         highlightthickness=self.px(1), borderwidth=0,
                         activestyle="dotbox", font=self.fonts["body"])


def work_area(window):
    """Return monitor coordinates in the same DPI context as this Tk process."""
    import sys
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MonitorInfo))
        anchor = window.master.winfo_toplevel() if window.master is not None else window
        info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        monitor = user32.MonitorFromWindow(anchor.winfo_id(), 2)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.work
            return r.left, r.top, r.right, r.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def apply_theme(widget):
    """Initialize once per Tcl interpreter; safe for roots created in sequence.

    Bootstrap 1.x holds a process-wide Style singleton. Rebind it when creating
    another interpreter (wizard → dashboard, or isolated integration tests).
    The temporary default-root binding also prevents targeting an older Tk.
    """
    import tkinter as tk
    from ttkbootstrap import Style
    from ttkbootstrap.publisher import Publisher

    root = widget._root()
    existing = getattr(root, "_bagu_theme", None)
    if existing is not None:
        Style.instance = existing.style
        return existing

    previous_root = tk._default_root
    Style.instance = None
    # Theme switching publishes to global subscriptions. Each interpreter has a
    # fixed theme, so subscriptions from previous roots are unnecessary here.
    Publisher.clear_subscribers()
    tk._default_root = root
    try:
        return _create_theme(root)
    finally:
        tk._default_root = previous_root


def _create_theme(root):
    from tkinter import font
    from ttkbootstrap import Style
    from ttkbootstrap.style import ThemeDefinition

    # Bootstrap's localization initializer assumes msgcat is already loaded;
    # require it explicitly for every fresh Tcl interpreter.
    root.tk.call("package", "require", "msgcat")
    style = Style(theme="litera")
    style.register_theme(ThemeDefinition(name="bagu-light", themetype="light", colors={
        "primary": COLORS.primary, "secondary": COLORS.muted,
        "success": "#23734D", "info": COLORS.primary, "warning": "#8A5700",
        "danger": COLORS.danger, "light": COLORS.background, "dark": COLORS.text,
        "bg": COLORS.background, "fg": COLORS.text,
        "selectbg": COLORS.selection, "selectfg": COLORS.text,
        "border": COLORS.border, "inputfg": COLORS.text,
        "inputbg": COLORS.surface, "active": COLORS.selection,
    }))
    style.theme_use("bagu-light")
    available = set(font.families(root))
    family = next((name for name in FONT_FAMILIES if name in available),
                  font.nametofont("TkDefaultFont", root=root).actual("family"))
    fonts = {role: font.Font(root=root, name=f"Bagu{role.title()}Font", family=family,
                            size=size, weight="bold" if role in ("title", "section") else "normal")
             for role, size in FONT_SIZES.items()}
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
        font.nametofont(name, root=root).configure(family=family, size=FONT_SIZES["body"])
    theme = DesktopTheme(root, style, fonts, float(root.tk.call("tk", "scaling")) / (96 / 72))
    root._bagu_theme = theme  # retain named fonts/images for this interpreter
    # A wildcard *Font would override ttk Label's title/section style fonts.
    # Named Tk fonts cover classic widgets; ttk uses the semantic styles below.
    root.option_add("*Listbox.font", fonts["body"])
    root.configure(background=COLORS.background)
    px = theme.px
    style.configure(".", font=fonts["body"])
    style.configure("TFrame", background=COLORS.background)
    style.configure("Focus.TFrame", background=COLORS.primary)
    style.configure("TLabel", foreground=COLORS.text, background=COLORS.background)
    for name, role in ((TITLE, "title"), (SECTION, "section"), (CAPTION, "caption")):
        style.configure(name, font=fonts[role], foreground=COLORS.muted if role == "caption" else COLORS.text)
    style.configure(MUTED, foreground=COLORS.muted)
    style.configure("Step.TLabel", foreground=COLORS.muted,
                    padding=px((SPACE.sm, SPACE.xs if theme.compact else SPACE.sm)))
    style.configure("Current.Step.TLabel", foreground=COLORS.primary, background=COLORS.selection)
    style.configure(STATUS, background=COLORS.selection, foreground=COLORS.text, padding=px(SPACE.md))

    # Build every primitive used by our windows before applying shared metrics.
    from ttkbootstrap.style import Bootstyle
    for name in ("TButton", "TEntry", "TCombobox", "Treeview", "TRadiobutton",
                 "TMenubutton", "Horizontal.TProgressbar", "Vertical.TScrollbar"):
        Bootstyle.update_ttk_widget_style(None, name)
    for name in ("TEntry", "TCombobox"):
        style.configure(name, padding=px(METRICS.input_padding), font=fonts["body"])
    style.configure("Treeview", rowheight=px(METRICS.row_height), font=fonts["body"])
    style.configure("Treeview.Heading", font=fonts["body"], padding=px((SPACE.sm, SPACE.sm)))
    style.configure("TRadiobutton", padding=px((SPACE.xs, SPACE.sm)))
    style.configure("Quiz.TRadiobutton", wraplength=px(490))
    style.configure("TMenubutton", padding=px(METRICS.button_padding),
                    background=COLORS.surface, foreground=COLORS.text,
                    bordercolor=COLORS.border, lightcolor=COLORS.surface,
                    darkcolor=COLORS.surface, focuscolor=COLORS.primary)
    menu_states = [("disabled", COLORS.disabled), ("pressed", COLORS.disabled), ("active", COLORS.selection)]
    style.map("TMenubutton", background=menu_states, lightcolor=menu_states,
              darkcolor=menu_states, bordercolor=[("focus", COLORS.primary)],
              foreground=[("disabled", COLORS.disabled_text)])
    style.configure("Horizontal.TProgressbar", thickness=px(METRICS.progress_thickness))
    style.configure("Vertical.TScrollbar", arrowsize=px(METRICS.scrollbar_width))

    for name, color, hover, pressed, foreground in (
        (PRIMARY, COLORS.primary, COLORS.primary_hover, COLORS.primary_pressed, COLORS.surface),
        (SECONDARY, COLORS.surface, COLORS.selection, COLORS.disabled, COLORS.text),
        (DANGER, COLORS.danger, COLORS.danger_hover, COLORS.danger_hover, COLORS.surface),
    ):
        style.configure(name, background=color, foreground=foreground,
                        lightcolor=color, darkcolor=color,
                        bordercolor=COLORS.border if name == SECONDARY else color,
                        focuscolor=COLORS.primary if name == SECONDARY else COLORS.surface,
                        focusthickness=px(2), padding=px(METRICS.button_padding),
                        width=-METRICS.button_width, anchor="center")
        style.map(name,
                  background=[("disabled", COLORS.disabled), ("pressed", pressed), ("active", hover)],
                  lightcolor=[("disabled", COLORS.disabled), ("pressed", pressed), ("active", hover)],
                  darkcolor=[("disabled", COLORS.disabled), ("pressed", pressed), ("active", hover)],
                  foreground=[("disabled", COLORS.disabled_text)],
                  bordercolor=[("disabled", COLORS.disabled), ("focus", COLORS.primary)])
    # Ordinary actions are secondary unless the caller explicitly promotes one.
    style.configure("TButton", **style.configure(SECONDARY))
    style.map("TButton", **style.map(SECONDARY))

    style.configure("Sidebar.TFrame", background=COLORS.sidebar)
    style.configure("Sidebar.TLabel", background=COLORS.sidebar, foreground=COLORS.muted)
    style.configure("Brand.TLabel", background=COLORS.sidebar, font=fonts["section"])
    style.configure("Surface.TFrame", background=COLORS.surface)
    style.configure("Focused.Page.TLabel", font=fonts["title"], foreground=COLORS.primary,
                    background=COLORS.selection)
    for name, background, foreground in (("Navigation.TButton", COLORS.sidebar, COLORS.text),
                                         ("Selected.Navigation.TButton", COLORS.selection, COLORS.primary)):
        style.configure(name, **{**style.configure(SECONDARY), "background": background,
                                "foreground": foreground, "lightcolor": background,
                                "darkcolor": background, "bordercolor": background,
                                "anchor": "w", "padding": px((SPACE.sm, 10)), "width": 1})
        style.map(name, background=[("disabled", COLORS.sidebar), ("pressed", COLORS.disabled),
                                    ("active", COLORS.selection)],
                  foreground=[("disabled", COLORS.disabled_text)],
                  bordercolor=[("focus", COLORS.primary), ("!focus", background)],
                  lightcolor=[("active", COLORS.selection)], darkcolor=[("active", COLORS.selection)])

    def release_resources(event):
        if event.widget is not root:
            return
        # Font/PhotoImage destructors call Tcl. Release them on the UI thread
        # while destroying this root, never later in a worker's cyclic GC.
        from ttkbootstrap.publisher import Publisher
        Publisher.clear_subscribers()
        for builder in style._theme_objects.values():
            builder.theme_images.clear()
        style._theme_objects.clear()
        fonts.clear()
        icon_cache = getattr(theme, "_icon_cache", None)
        if icon_cache is not None:
            icon_cache.clear()
        if Style.instance is style:
            Style.instance = None
        del root._bagu_theme
        theme.root = None
        style.master = None
        # Destroy/FocusOut callbacks registered after this hook may still use
        # ttk styles while Tk is unwinding the root. Keep the Tcl handle valid;
        # all images/fonts were already released on this UI thread above.

    root.bind("<Destroy>", release_resources, add="+")
    return theme
