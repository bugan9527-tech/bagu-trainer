"""Window placement metadata only; independent of business configuration."""
import json
from pathlib import Path

from bagu_trainer.desktop.ui_theme import work_area


class WindowPlacement:
    def __init__(self, root, theme, path):
        self.root, self.theme, self.path = root, theme, Path(path)
        self.normal = None
        self.maximized = False
        self.restore()
        root.bind("<Configure>", self._remember, add="+")

    def restore(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            bounds = data["normal"]
            if not isinstance(bounds, list) or len(bounds) != 4 or any(type(n) is not int for n in bounds):
                return
            width, height, x, y = bounds
            if width <= 0 or height <= 0:
                return
            # Windows may restore an iconic window straight to its previous
            # maximized state. Apply normal bounds before restoring that mode.
            self.root.state("normal")
            self.maximized = False
            left, top, right, bottom = work_area(self.root)
            margin = self.theme.px(16)
            minimum = self.root.minsize()
            width = min(max(width, minimum[0]), max(1, right - left - margin * 2))
            height = min(max(height, minimum[1]), max(1, bottom - top - self.theme.px(72)))
            x = max(left + margin, min(x, right - width - margin))
            y = max(top + margin, min(y, bottom - height - self.theme.px(56)))
            self.normal = [width, height, x, y]
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            if data.get("maximized") is True:
                self.maximized = True
                self.root.state("zoomed")
        except (OSError, ValueError, KeyError, TypeError):
            pass  # missing/corrupt UI metadata must never block startup

    def _remember(self, event):
        if event.widget is not self.root:
            return
        state = self.root.state()
        if state in {"normal", "zoomed"}:
            self.maximized = state == "zoomed"
        if state == "normal":
            width, height = self.root.winfo_width(), self.root.winfo_height()
            if width > 1 and height > 1:
                self.normal = [width, height, self.root.winfo_x(), self.root.winfo_y()]

    def save(self):
        if self.normal is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            state = self.root.state()
            maximized = state == "zoomed" if state in {"normal", "zoomed"} else self.maximized
            temporary.write_text(json.dumps({"normal": self.normal, "maximized": maximized}), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass  # a read-only data directory must not prevent safe exit
