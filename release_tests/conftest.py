"""Never use the signed-in user's real application data in release tests."""
import gc
import sys

import pytest
from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure import desktop_instance


@pytest.fixture(autouse=True)
def isolate_tk_scaling(monkeypatch):
    """Tk scaling survives interpreters on Windows; keep each case independent.

    Logical layout cases start at 100%; explicit scale factories override it.
    No production DPI setting or layout assertion is changed.
    """
    if sys.platform != "win32":
        return
    import tkinter as tk
    import ttkbootstrap  # install constructor hooks before wrapping
    actual = tk.Tk
    scaling = 96 / 72
    def native_root(*args, **kwargs):
        root = actual(*args, **kwargs)
        root.tk.call("tk", "scaling", scaling)
        return root
    monkeypatch.setattr(tk, "Tk", native_root)


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    user = tmp_path / "isolated-user"
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(user))
    monkeypatch.setenv("BAGU_TEST_INSTANCE", "release-fixture")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setattr(config, "USER_DATA_ROOT", user)
    monkeypatch.setattr(config, "CONFIG_PATH", user / "config.json")
    monkeypatch.setattr(desktop_instance, "RUNTIME", user / "runtime")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    if "tkinter" in sys.modules:
        # Each GUI case destroys its windows, but callback cycles may survive
        # fixture teardown. Reclaim them on pytest's main/UI thread before the
        # next case starts workers; worker-triggered Tk GC can abort Windows.
        gc.collect()
