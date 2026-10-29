"""Resizable Tk layouts shared by the desktop windows (no business state)."""
import tkinter as tk
from tkinter import ttk

from bagu_trainer.desktop.ui_theme import COLORS
from bagu_trainer.desktop.ui_theme import SPACE


def wrapped_label(parent, theme, **options):
    """Use the allocated width, including when a translated/long label grows."""
    label = ttk.Label(parent, wraplength=theme.px(360), justify="left", **options)
    padding = theme.px(SPACE.xl) if options.get("style") == "Status.TLabel" else 0
    def request_layout(*_):
        ancestor = label.master
        while ancestor is not None:
            if isinstance(ancestor, ScrollableFrame):
                ancestor.request_layout()
                break
            ancestor = getattr(ancestor, "master", None)
    def resize(event):
        if event.width <= 1:
            return  # unmapped geometry must not wrap long text into one column
        width = max(1, event.width - padding)
        if int(label.cget("wraplength")) != width:
            label.configure(wraplength=width)
            request_layout()
    label.bind("<Configure>", resize)
    variable = options.get("textvariable")
    if isinstance(variable, tk.Variable):
        trace = variable.trace_add("write", request_layout)
        label.bind("<Destroy>", lambda event: variable.trace_remove("write", trace)
                   if event.widget is label else None, add="+")
    return label


class ScrollableFrame(ttk.Frame):
    """Stretch the body to the viewport, scroll overflow, keep focus reachable."""
    def __init__(self, parent, theme, *, minimum_height=None):
        super().__init__(parent, padding=theme.px(2))
        self.theme = theme
        self.minimum_height = minimum_height
        self.pending_layout = None
        self.tab_widgets = set()
        self.region = None
        self.follow_end = False
        self._reveal_target = None
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                width=1, height=1, takefocus=True)
        self.canvas.configure(background=COLORS.background)
        # Tk sends ancestor FocusIn events when an Entry inside the canvas is
        # focused. Only the reading surface itself should draw this outer ring.
        def focus_style(_event):
            self.configure(style="Focus.TFrame" if self.focus_get() is self.canvas else "TFrame")
        self.canvas.bind("<FocusIn>", focus_style)
        self.canvas.bind("<FocusOut>", focus_style)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._scroll_to)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self._scroll_state)
        self.body = ttk.Frame(self.canvas, padding=(0, 0, theme.px(SPACE.sm), 0))
        self.body.columnconfigure(0, weight=1)
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._resize)
        for key, amount, unit in (("Up", -1, "units"), ("Down", 1, "units"),
                                  ("Prior", -1, "pages"), ("Next", 1, "pages")):
            self.canvas.bind(f"<{key}>", lambda _event, n=amount, u=unit: self._key_scroll(n, u))
        self.canvas.bind("<Home>", lambda _event: self._key_position(0))
        self.canvas.bind("<End>", lambda _event: self._key_position(1))
        self.canvas.bind("<Tab>", lambda _event: self._tab(self.canvas, False))
        self.canvas.bind("<Shift-Tab>", lambda _event: self._tab(self.canvas, True))
        self.body.bind("<Configure>", self._resize)
        self.top = self.winfo_toplevel()
        self.wheel_binding = self.top.bind("<MouseWheel>", self._wheel, add="+")
        self.focus_binding = self.top.bind("<FocusIn>", self._focus, add="+")
        self.bind("<Destroy>", self._destroyed, add="+")

    def _scroll_state(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()

    def _resize(self, _event=None):
        self._wire_tab(self.body)
        if self.canvas.winfo_width() <= 1 or self.canvas.winfo_height() <= 1:
            return
        width = max(1, self.canvas.winfo_width())
        requested = max(self.body.winfo_reqheight(), self.minimum_height or 0)
        height = max(requested, self.canvas.winfo_height())
        first, last = self.canvas.yview()
        keep_end = first > 0 and last >= 1
        # Avoid generating another scroll/layout callback for unchanged geometry.
        # Height=0 lets Tk propagate the body's natural requested height. A
        # viewport-sized forced height creates feedback while wrapped labels
        # shrink/grow during maximize/restore. Only the embedded bank needs a
        # minimum body height for its stretching source table.
        body_height = max(requested, self.minimum_height) if self.minimum_height is not None else 0
        if (int(float(self.canvas.itemcget(self.item, "width"))),
                int(float(self.canvas.itemcget(self.item, "height")))) != (width, body_height):
            self.canvas.itemconfigure(self.item, width=width, height=body_height)
        region = (0, 0, width, height)
        if self.region != region:
            self.region = region
            self.canvas.configure(scrollregion=region)
            if keep_end:
                self.canvas.yview_moveto(1)
        # End is an explicit reading intention. Geometry/line-wrap changes may
        # arrive after the key event, so keep following until the reader moves.
        if self.follow_end and self.canvas.yview()[1] < 1:
            self.canvas.yview_moveto(1)
        elif not keep_end and self._reveal_target is not None:
            self._scroll_widget_into_view(self._reveal_target)

    def request_layout(self):
        # A label's requested height can change without changing the allocated
        # body size. Recalculate after wrapping/geometry propagation settles.
        if self.pending_layout is None:
            self.pending_layout = self.after_idle(self._flush_layout)

    def _flush_layout(self):
        self.pending_layout = None
        self._resize()

    def _contains(self, widget):
        while widget is not None:
            if widget in (self.body, self.canvas):
                return True
            widget = getattr(widget, "master", None)
        return False

    def _wheel(self, event):
        target = self.winfo_containing(event.x_root, event.y_root)
        if (self._contains(target) and target.winfo_class() not in ("Text", "Treeview", "Listbox", "TCombobox")
                and self.canvas.yview() != (0.0, 1.0)):
            self._reveal_target = None
            self.follow_end = False
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
            return "break"

    def _focus(self, event):
        if self.follow_end:
            return  # late native focus notifications must not undo an End key
        if event.widget is not self.canvas and event.widget is self.focus_get() and self._contains(event.widget):
            self.reveal(event.widget)

    def _wire_tab(self, parent):
        for widget in parent.winfo_children():
            if widget not in self.tab_widgets:
                if widget.winfo_class() in ("TEntry", "TCombobox", "TButton", "TRadiobutton", "Treeview", "Listbox"):
                    widget.bind("<Tab>", lambda _event, w=widget: self._tab(w, False))
                    widget.bind("<Shift-Tab>", lambda _event, w=widget: self._tab(w, True))
                self.tab_widgets.add(widget)
            self._wire_tab(widget)

    def _tab(self, widget, backwards):
        self.follow_end = False
        target = widget.tk_focusPrev() if backwards else widget.tk_focusNext()
        if target is not self.canvas and self._contains(target):
            self.reveal(target)
        target.focus_set()
        return "break"

    def _key_scroll(self, amount, unit):
        self._reveal_target = None
        self.follow_end = False
        self.canvas.yview_scroll(amount, unit)
        return "break"

    def _key_position(self, fraction):
        self._reveal_target = None
        self.follow_end = fraction >= 1
        self.update_idletasks()
        self._resize()
        self.canvas.yview_moveto(fraction)
        return "break"

    def _scroll_to(self, *arguments):
        self._reveal_target = None
        self.follow_end = False
        self.canvas.yview(*arguments)

    def reveal(self, widget):
        self.follow_end = False
        self._reveal_target = widget
        self.update_idletasks()
        self._resize()
        self.update_idletasks()
        self._scroll_widget_into_view(widget)

    def _scroll_widget_into_view(self, widget):
        if not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        y = widget.winfo_rooty() - self.body.winfo_rooty()
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        height = max(1, (self.region or (0, 0, 0, self.body.winfo_height()))[3])
        if y < top:
            self.canvas.yview_moveto(y / height)
        elif y + min(widget.winfo_height(), self.canvas.winfo_height()) > bottom:
            self.canvas.yview_moveto((y + min(widget.winfo_height(), self.canvas.winfo_height()) - self.canvas.winfo_height()) / height)

    def _destroyed(self, event):
        if event.widget is self:
            if self.pending_layout is not None:
                self.after_cancel(self.pending_layout)
            self.top.unbind("<MouseWheel>", self.wheel_binding)
            self.top.unbind("<FocusIn>", self.focus_binding)
            self.tab_widgets.clear()
            self._reveal_target = None
            self.top = self.body = self.canvas = self.scrollbar = None


class MessageArea(ttk.Frame):
    """Bounded, selectable status/path text; a long message never hides actions."""
    def __init__(self, parent, variable, theme, *, lines=3, muted=False):
        super().__init__(parent, padding=theme.px(SPACE.md))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(self, height=lines, width=1, wrap="char", borderwidth=0,
                            highlightthickness=theme.px(2), font=theme.fonts["body"], takefocus=True)
        self.text.configure(background=COLORS.background if muted else COLORS.selection,
                            foreground=COLORS.muted if muted else COLORS.text,
                            font=theme.fonts["body"],
                            borderwidth=0, highlightthickness=theme.px(2),
                            highlightbackground=COLORS.background if muted else COLORS.selection,
                            highlightcolor=COLORS.primary,
                            selectbackground=COLORS.primary, selectforeground=COLORS.surface)
        # A disabled Text still consumes Tab in Tk's class binding. Keep status
        # text selectable without trapping keyboard users or inserting tabs.
        self.text.bind("<Tab>", lambda _event: self._traverse(False))
        self.text.bind("<Shift-Tab>", lambda _event: self._traverse(True))
        self.text.bind("<Control-a>", self._select_all)
        self.text.grid(row=0, column=0, sticky="nsew")
        # A scrollbar can request more height than a one-line Text. Let the
        # Text determine height, otherwise showing the bar makes all text fit,
        # hides the bar again and starts an endless geometry feedback loop.
        self.scrollbar_host = ttk.Frame(self, height=1)
        self.scrollbar_host.grid(row=0, column=1, sticky="ns")
        self.scrollbar_host.grid_propagate(False)
        self.scrollbar_host.rowconfigure(0, weight=1)
        self.scrollbar_host.columnconfigure(0, weight=1)
        self.scrollbar = ttk.Scrollbar(self.scrollbar_host, command=self.text.yview)
        self.scrollbar.grid(row=0, column=0, sticky="nsew")
        self.scrollbar_host.configure(width=self.scrollbar.winfo_reqwidth())
        self.text.configure(yscrollcommand=self._scroll_state)
        self.variable = variable
        self.trace = variable.trace_add("write", self._update)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._update()

    def _scroll_state(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar_host.grid_remove()
        else:
            self.scrollbar_host.grid()

    def _update(self, *_):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.variable.get())
        self.text.configure(state="disabled")

    def _traverse(self, backwards):
        target = self.text.tk_focusPrev() if backwards else self.text.tk_focusNext()
        target.focus_set()
        return "break"

    def _select_all(self, _event):
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _destroyed(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)
            # Drop Tk variables on the UI thread, not in a later worker GC.
            self.variable = self.text = self.scrollbar = self.scrollbar_host = None


class FlowActions(ttk.Frame):
    """Reflow buttons by their requested text width; never crop a narrow row."""
    def __init__(self, parent, theme):
        super().__init__(parent)
        self.gap = theme.px(SPACE.sm)
        self.widgets = []
        self.bind("<Configure>", self._reflow)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget is self:
            self.widgets.clear()

    def add(self, widget):
        self.widgets.append(widget)
        self._reflow()
        return widget

    def _reflow(self, _event=None):
        width = self.winfo_width()
        columns = max(1, len(self.widgets))
        while columns > 1:
            sizes = [max(widget.winfo_reqwidth() for widget in self.widgets[column::columns])
                     for column in range(columns)]
            if sum(sizes) + self.gap * columns <= width:
                break
            columns -= 1
        for index, widget in enumerate(self.widgets):
            row, column = divmod(index, columns)
            widget.grid(row=row, column=column, sticky="w", padx=(0, self.gap), pady=(0, self.gap))
