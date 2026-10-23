"""Personal state must not be distributable, even if accidentally copied."""
import json
from pathlib import Path

import pytest

from scripts.build_portable import audit


@pytest.mark.parametrize("name", ["config.json", "window.json", "data/trainer.db",
    "runtime/owner.json", "logs/session.txt", "profiles/default/settings.json",
    ".desktop-runtime/auth.json", "user-data/history.json"])
def test_personal_state_is_rejected_by_bundle_audit(tmp_path, name):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic privacy fixture", encoding="utf-8")
    with pytest.raises(RuntimeError, match="禁止收录"):
        audit(tmp_path)


def test_configuration_example_has_no_key_or_personal_projects():
    root = Path(__file__).resolve().parents[1]
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    assert not example["llm"]["api_key"]
    assert not example["project_roots"]
    assert not example["trae_home"]
