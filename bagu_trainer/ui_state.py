"""UI-only lifecycle helpers; workers pass data through queues, never Tk calls."""


def cancel_timer(widget, timer):
    if timer is not None:
        import tkinter as tk
        try:
            widget.after_cancel(timer)
        except tk.TclError:
            pass


def set_text(variable, value):
    """Avoid relayout/trace churn for unchanged status text."""
    if variable.get() != value:
        variable.set(value)


def clear_variable_traces(registrations):
    """Release Tcl callbacks owning closed windows on the UI thread."""
    import tkinter as tk
    for variable, callback in registrations:
        if variable._tk is None:
            continue
        try:
            variable.trace_remove("write", callback)
        except tk.TclError:
            pass  # interpreter teardown may already have removed the callback
    registrations.clear()


def window_var(master, value=""):
    """A StringVar whose Tcl resources are released on its owner's UI thread.

    Closed variables retain a plain string snapshot for existing close-result
    callers. Later Python GC (including worker GC) cannot call destroyed Tcl.
    """
    import tkinter as tk
    class WindowStringVar(tk.StringVar):
        def __init__(self, owner, initial):
            self.detached = False
            self.snapshot = initial
            super().__init__(owner, value=initial)
            self.owner = owner
            self.destroy_binding = owner.bind("<Destroy>", self._owner_destroyed, add="+")

        def get(self):
            return self.snapshot if self.detached else super().get()

        def set(self, value):
            if self.detached:
                self.snapshot = value
            else:
                super().set(value)

        def trace_info(self):
            return [] if self.detached else super().trace_info()

        def _owner_destroyed(self, event):
            if event.widget is not self.owner or self.detached:
                return
            self.snapshot = super().get()
            super().__del__()
            self._tk = self._root = None
            self.detached = True
            owner, binding = self.owner, self.destroy_binding
            self.owner = self.destroy_binding = None
            owner.unbind("<Destroy>", binding)
    return WindowStringVar(master, value)
