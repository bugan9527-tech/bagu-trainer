"""Selectable read-only quiz text and explicit, presentation-only copy formats."""
import tkinter as tk

from .ui_theme import COLORS


def question_text(question, options):
    return str(question or "") + "\n\n" + "\n".join(f"{letter}. {options.get(letter, '')}" for letter in "ABCD")


def solution_text(answer, analysis):
    return f"正确答案：{answer or '未提供'}\n\n解析：\n{analysis or '暂无解析。'}"


def copy_text(widget, value):
    if value:
        widget.clipboard_clear()
        widget.clipboard_append(value)


class ReadOnlyText(tk.Text):
    """Grow to wrapped display lines; scrolling belongs to the surrounding page.

    Disabled Text retains native mouse/keyboard selection. No answer data is
    stored here unless its caller has already allowed it to be displayed.
    """
    def __init__(self, parent, theme, *, text="", textvariable=None):
        super().__init__(parent, width=1, height=1, wrap="word", state="disabled", takefocus=True,
                         font=theme.fonts["body"], background=COLORS.background, foreground=COLORS.text,
                         borderwidth=0, padx=theme.px(2), pady=theme.px(2),
                         highlightthickness=theme.px(1), highlightbackground=COLORS.background,
                         highlightcolor=COLORS.primary, selectbackground=COLORS.primary,
                         selectforeground=COLORS.surface, exportselection=False, cursor="xterm", undo=False)
        self._resize_timer = None
        self._variable = textvariable
        self._trace = None
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="复制", accelerator="Ctrl+C", command=self.copy_selection)
        for sequence in ("<Control-c>", "<Control-C>", "<Control-Insert>", "<<Copy>>"):
            self.bind(sequence, self.copy_selection)
        for sequence in ("<Control-a>", "<Control-A>"):
            self.bind(sequence, self.select_all)
        self.bind("<Button-3>", self._popup)
        self.bind("<Shift-F10>", self._popup)
        self.bind("<Tab>", lambda _: self._traverse(False))
        self.bind("<Shift-Tab>", lambda _: self._traverse(True))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Configure>", self._schedule_resize)
        self.bind("<Destroy>", self._destroyed, add="+")
        if textvariable is not None:
            self._trace = textvariable.trace_add("write", lambda *_: self.set_text(textvariable.get()))
            text = textvariable.get()
        self.set_text(text)

    def set_text(self, value):
        value = str(value or "")
        if self.get("1.0", "end-1c") == value:
            return  # A selection must survive ordinary page refresh/answer changes.
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", value)
        self.configure(state="disabled")
        self._schedule_resize()

    def copy_selection(self, _event=None):
        if self.tag_ranges("sel"):
            copy_text(self, self.get("sel.first", "sel.last"))
        return "break"

    def select_all(self, _event=None):
        self.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _popup(self, event):
        self.focus_set()
        self.menu.entryconfigure(0, state="normal" if self.tag_ranges("sel") else "disabled")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def _traverse(self, backwards):
        (self.tk_focusPrev() if backwards else self.tk_focusNext()).focus_set()
        return "break"

    def _scroll_parent(self):
        from .ui_layout import ScrollableFrame
        parent = self.master
        while parent is not None:
            if isinstance(parent, ScrollableFrame):
                return parent
            parent = getattr(parent, "master", None)

    def _wheel(self, event):
        parent = self._scroll_parent()
        if parent:
            return parent._key_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")

    def _schedule_resize(self, _event=None):
        if self._resize_timer is None:
            self._resize_timer = self.after_idle(self._resize)

    def _resize(self):
        self._resize_timer = None
        if self.winfo_width() <= 1:
            return
        lines = self.count("1.0", "end", "displaylines")
        height = max(1, lines[0] if lines else 1)
        if int(self.cget("height")) != height:
            self.configure(height=height)
            parent = self._scroll_parent()
            if parent:
                parent.request_layout()

    def _destroyed(self, event):
        if event.widget is self:
            if self._resize_timer is not None:
                self.after_cancel(self._resize_timer)
            if self._trace is not None:
                self._variable.trace_remove("write", self._trace)
