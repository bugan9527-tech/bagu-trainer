# -*- coding: utf-8 -*-
"""密钥迁移回归测试：只使用 tmp_path、假配置和 FakeKeyring。"""
import json

import pytest

import bagu_trainer.config as config_mod
import bagu_trainer.secrets as secrets_mod

FAKE_KEY = "sk-fake-migration-key-7c4b2a9e8d1f"


class FakeKeyring:
    def __init__(self, stored=None):
        self.values = {}
        if stored is not None:
            self.values[(secrets_mod.KEYRING_SERVICE, secrets_mod.KEYRING_USER)] = stored

    def get_password(self, service, user):
        return self.values.get((service, user))

    def set_password(self, service, user, value):
        self.values[(service, user)] = value

    def delete_password(self, service, user):
        self.values.pop((service, user), None)


class WriteFailKeyring(FakeKeyring):
    def set_password(self, service, user, value):
        raise RuntimeError("fake keyring write failure")


def _write_fake_config(path):
    path.write_text(json.dumps({
        "project_roots": ["C:\\fake-project"],
        "llm": {
            "base_url": "https://fake.local/v1",
            "api_key": FAKE_KEY,
            "model": "fake-model",
            "timeout_sec": 12,
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _install_fake_runtime(monkeypatch, tmp_path, keyring):
    config_path = tmp_path / "config.json"
    _write_fake_config(config_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(secrets_mod, "_load_keyring", lambda: keyring)
    monkeypatch.delenv(secrets_mod.ENV_KEY, raising=False)
    return config_path


def _assert_no_key_fragment(text):
    assert FAKE_KEY not in text
    for start in range(0, len(FAKE_KEY) - 7):
        assert FAKE_KEY[start:start + 8] not in text


def test_migrate_keeps_key_in_fake_keyring_and_is_idempotent(
        tmp_path, monkeypatch, capsys):
    keyring = FakeKeyring()
    config_path = _install_fake_runtime(monkeypatch, tmp_path, keyring)

    assert secrets_mod._main(["migrate"]) == 0

    config = json.loads(config_path.read_text(encoding="utf-8"))
    keyring_has_key_after_migrate = (
        keyring.get_password(secrets_mod.KEYRING_SERVICE, secrets_mod.KEYRING_USER)
        == FAKE_KEY
    )
    assert keyring_has_key_after_migrate is True
    assert config["llm"]["api_key"] == ""
    assert config["llm"]["model"] == "fake-model"
    assert config["project_roots"] == ["C:\\fake-project"]
    assert secrets_mod.resolve_api_key() == FAKE_KEY

    first_output = "".join(capsys.readouterr())
    _assert_no_key_fragment(first_output)

    assert secrets_mod._main(["migrate"]) == 0
    assert keyring.get_password(
        secrets_mod.KEYRING_SERVICE, secrets_mod.KEYRING_USER) == FAKE_KEY
    assert json.loads(config_path.read_text(encoding="utf-8"))["llm"]["api_key"] == ""

    second_output = "".join(capsys.readouterr())
    assert "已完成" in second_output
    _assert_no_key_fragment(second_output)


@pytest.mark.parametrize("failure_mode", ["unavailable", "write_failure"])
def test_migrate_keyring_failure_keeps_config_unchanged(
        tmp_path, monkeypatch, capsys, failure_mode):
    keyring = None if failure_mode == "unavailable" else WriteFailKeyring()
    config_path = _install_fake_runtime(monkeypatch, tmp_path, keyring)
    original = config_path.read_bytes()

    assert secrets_mod._main(["migrate"]) != 0

    assert config_path.read_bytes() == original
    output = "".join(capsys.readouterr())
    assert secrets_mod.ENV_KEY in output
    _assert_no_key_fragment(output)


def test_config_only_update_does_not_clear_existing_key(tmp_path, monkeypatch):
    keyring = FakeKeyring(FAKE_KEY)
    config_path = _install_fake_runtime(monkeypatch, tmp_path, keyring)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["llm"]["api_key"] = ""
    config_path.write_text(json.dumps(config), encoding="utf-8")

    stored = config_mod.update_llm({
        "base_url": "https://fake.local/v1",
        "api_key": "",
        "model": "updated-model",
        "timeout_sec": 12,
    })

    assert stored == "unchanged"
    assert keyring.get_password(
        secrets_mod.KEYRING_SERVICE, secrets_mod.KEYRING_USER) == FAKE_KEY


def test_update_llm_uses_atomic_replace(tmp_path, monkeypatch):
    keyring = FakeKeyring()
    config_path = _install_fake_runtime(monkeypatch, tmp_path, keyring)
    replace_calls = []
    real_replace = config_mod.os.replace

    def tracking_replace(source, target):
        replace_calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(config_mod.os, "replace", tracking_replace)

    stored = config_mod.update_llm({
        "base_url": "https://fake.local/v1",
        "api_key": FAKE_KEY,
        "model": "fake-model",
        "timeout_sec": 12,
    })

    assert stored == "keyring"
    assert len(replace_calls) == 1
    source, target = map(str, replace_calls[0])
    assert source.endswith(".tmp")
    assert str(config_path) == target
    assert not list(tmp_path.glob(f".{config_path.name}.*.tmp"))
    assert FAKE_KEY not in config_path.read_text(encoding="utf-8")
