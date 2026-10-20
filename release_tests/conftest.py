"""Never use the signed-in user's real application data in release tests."""
import pytest
from bagu_trainer import config, desktop_instance


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    user = tmp_path / "isolated-user"
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(user))
    monkeypatch.setattr(config, "USER_DATA_ROOT", user)
    monkeypatch.setattr(config, "CONFIG_PATH", user / "config.json")
    monkeypatch.setattr(desktop_instance, "RUNTIME", user / "runtime")
