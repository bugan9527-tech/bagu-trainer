"""Never use the signed-in user's real application data in release tests."""
import gc
import sys

import pytest
from bagu_trainer import config, desktop_instance


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    user = tmp_path / "isolated-user"
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(user))
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
