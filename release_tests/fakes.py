"""Public learning Interface for UI-only tests that do not open SQLite/models."""
from contextlib import nullcontext
from types import SimpleNamespace


class LearningFake:
    def __init__(self, engine):
        self.engine = engine
        self.roots = ()

    def has_question_bank(self):
        return True

    def question_count(self):
        return 0

    def apply_project_scope(self, roots):
        removed = set(self.roots) - set(roots)
        session = getattr(self.engine, 'session', None)
        if removed and session is not None:
            session.forget_projects(removed)
        self.roots = tuple(roots)

    def generation_window(self):
        return lambda *_args, **_kwargs: {'result': ([], [], 0, []), 'current': True}

    def pending_questions(self):
        return []

    def learning_stats(self):
        return self.engine.get_learning_stats()

    def replace_question_bank(self):
        self.engine.reload_bank()

    def submit_answer(self, *args):
        return self.engine.submit_answer(*args)

    def content_unavailable(self):
        pass

    def close(self):
        pass


def ui_monitor():
    """A ready UI backed by a real monitor and an explicit fake learning workflow."""
    from bagu_trainer.application.monitor import DesktopMonitor
    engine = SimpleNamespace(load_config=lambda: {'project_roots': []}, PROJECT_ROOT='',
        get_learning_stats=lambda: {'ok': False}, content_maintenance=nullcontext,
        reload_bank=lambda: None)
    engine.learning = LearningFake(engine)
    return DesktopMonitor(engine)


def make_model_panel(parent=None):
    """Mount the real embedded panel in an owned test host; no legacy facade."""
    from bagu_trainer.desktop.tk_runtime import create_root
    from bagu_trainer.desktop.ui_theme import apply_theme
    from bagu_trainer.desktop.model_panel import ModelPanel
    import tkinter as tk
    root = tk.Toplevel(parent) if parent is not None else create_root()
    theme = apply_theme(root)
    theme.window(root, (760, 760), (560, 540))
    panel = ModelPanel(root, theme)
    panel.pack(fill="both", expand=True)
    panel.root = root  # The test owns and destroys this host separately.
    root.protocol("WM_DELETE_WINDOW", panel.close)
    return panel
