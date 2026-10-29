"""Desktop adapters with lazy loading for the legacy Tk window.

Importing a presenter or command handler from this package does not import
Tk. Historical ``bagu_trainer.desktop.DesktopApp`` and ``main`` access remains
available through module attribute lookup.
"""
from importlib import import_module


def __getattr__(name):
    if name in {"os", "time"}:  # Historical diagnostic harness patch targets.
        return import_module(name)
    if name not in {"DesktopApp", "main", "WorkQueue", "WorkerStop"}:
        raise AttributeError(name)
    module = import_module(".tk_app", __name__)
    try:
        return getattr(module, name)
    except AttributeError:
        raise AttributeError(name) from None
