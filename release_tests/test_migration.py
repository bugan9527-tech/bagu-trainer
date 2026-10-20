"""Copy-only migration, real SQLite history and retry without user data."""
import json
import threading

import pytest

from bagu_trainer import config, migration, secrets
from bagu_trainer.bank_storage import atomic_json
from bagu_trainer.scheduler import next_state
from bagu_trainer.store import Store


def legacy_project(root, *, custom_db=None, key=""):
    (root / "data").mkdir(parents=True)
    (root / "code 项目").mkdir()
    atomic_json(root / "config.json", {
        "project_roots": ["code 项目"], "db_path": custom_db or "data/trainer.db",
        "llm": {"base_url": "https://example.invalid/v1", "model": "mock", "api_key": key},
    })
    atomic_json(root / "data" / "questions.json", [{
        "id": "kp-1", "knowledge_point": "缓存", "question": "如何避免缓存穿透？",
        "tags": ["cache"], "keywords": ["redis"], "difficulty": "easy",
    }])
    db = root / (custom_db or "data/trainer.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db))
    store.create_quiz("history-1", "kp-1", {"question": "中文历史题", "answer": "A"},
                      "answered", str(root / "code 项目"), "迁移测试", 1800000000, 1800003600,
                      delivered_at=1800000000, answered_at=1800000010)
    store.record_attempt("history-1", "kp-1", "B", "B", False, 1800000010,
                         next_state(False, None, 1800000010))
    store.close()
    return root


def assert_history(path):
    store = Store(str(path))
    try:
        assert store.attempt_count("history-1") == 1
        assert store.get_quiz("history-1")["snapshot"]["question"] == "中文历史题"
        assert store.concept_state("kp-1")["attempts"] == 1
        assert store.concept_state("kp-1")["next_review_at"] == 1800021610
    finally:
        store.close()


@pytest.mark.parametrize("custom", [None, "custom 学习/learning.db", "external"])
def test_copy_verified_database_and_idempotent_retry(tmp_path, custom):
    if custom == "external":
        custom = str(tmp_path / "外部 数据库" / "learning.db")
    source = legacy_project(tmp_path / "旧 安装", custom_db=custom)
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    # Only the active bank generation and allowed artifacts are copied.
    generation = "a" * 32
    atomic_json(source / "data" / "active-bank.json", {"generation": generation})
    atomic_json(source / "data" / "banks" / generation / "questions.json", [{"id": "new"}])
    atomic_json(source / "data" / ".bank-builds" / "partial.json", {"partial": True})
    atomic_json(config.data_dir() / "onboarding.json", {"schema_version": 1})
    phases = []
    result = migration.migrate_legacy(source, progress=phases.append)
    assert not result["already_migrated"] and len(phases) == 3
    assert_history(config.data_dir() / "trainer.db")
    cfg = config.read_user_config()
    assert cfg["db_path"] == "data/trainer.db"
    assert cfg["project_roots"] == [str((source / "code 项目").resolve())]
    assert (config.data_dir() / "banks" / generation / "questions.json").exists()
    assert not (config.data_dir() / ".bank-builds").exists()
    assert all((source / p).read_bytes() == value for p, value in original.items())
    cfg["custom"] = "keep new edits"
    config._write_config_atomic(cfg)
    assert migration.migrate_legacy(source)["already_migrated"]
    assert config.read_user_config()["custom"] == "keep new edits"


def test_legacy_plain_key_must_be_verified_in_vault(tmp_path, monkeypatch):
    source = legacy_project(tmp_path / "old", key="test-secret-never-publish")
    monkeypatch.setattr(secrets, "save_api_key", lambda key: "unavailable")
    with pytest.raises(migration.MigrationError, match="凭据"):
        migration.migrate_legacy(source)
    assert not config.CONFIG_PATH.exists()
    assert not (config.data_dir() / "trainer.db").exists()
    saved = []
    monkeypatch.setattr(secrets, "save_api_key", lambda key: saved.append(key) or "keyring")
    monkeypatch.setattr(secrets, "_keyring_matches", lambda key: key == saved[-1])
    migration.migrate_legacy(source)
    assert saved == ["test-secret-never-publish"]
    assert "test-secret" not in config.CONFIG_PATH.read_text(encoding="utf-8")
    assert config.read_user_config()["llm"]["api_key"] == ""


def test_cancel_and_corrupt_database_are_retryable(tmp_path):
    source = legacy_project(tmp_path / "old")
    cancel = threading.Event()
    def progress(_):
        cancel.set()
    with pytest.raises(migration.MigrationError, match="取消"):
        migration.migrate_legacy(source, cancel=cancel, progress=progress)
    assert not config.CONFIG_PATH.exists()
    assert not list(config.USER_DATA_ROOT.glob(".migration-*"))
    database = source / "data" / "trainer.db"
    good = database.read_bytes()
    database.write_bytes(b"invalid SQLite")
    with pytest.raises(migration.MigrationError, match="迁移失败"):
        migration.migrate_legacy(source)
    assert not config.CONFIG_PATH.exists()
    database.write_bytes(good)
    migration.migrate_legacy(source)
    assert_history(config.data_dir() / "trainer.db")


def test_publish_failure_restores_placeholder_and_can_retry(tmp_path, monkeypatch):
    source = legacy_project(tmp_path / "old")
    atomic_json(config.data_dir() / "onboarding.json", {"original": True})
    original_writer = config._write_config_atomic
    def fail(_):
        raise PermissionError("simulated publication failure")
    monkeypatch.setattr(config, "_write_config_atomic", fail)
    with pytest.raises(migration.MigrationError):
        migration.migrate_legacy(source)
    assert json.loads((config.data_dir() / "onboarding.json").read_text()) == {"original": True}
    assert not (config.data_dir() / "trainer.db").exists()
    monkeypatch.setattr(config, "_write_config_atomic", original_writer)
    migration.migrate_legacy(source)
    assert_history(config.data_dir() / "trainer.db")


def test_existing_data_and_wrong_source_are_never_overwritten(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(migration.MigrationError, match="旧项目"):
        migration.migrate_legacy(empty)
    source = legacy_project(tmp_path / "old")
    atomic_json(config.data_dir() / "questions.json", [{"id": "existing"}])
    with pytest.raises(migration.MigrationError, match="不会覆盖"):
        migration.migrate_legacy(source)
    assert json.loads((config.data_dir() / "questions.json").read_text()) == [{"id": "existing"}]


def test_running_legacy_owner_blocks_migration_then_allows_retry(tmp_path):
    from bagu_trainer.desktop_instance import OwnerLock
    source = legacy_project(tmp_path / "old")
    owner = OwnerLock(source / "data" / ".desktop-runtime")
    assert owner.acquire()
    try:
        with pytest.raises(migration.MigrationError, match="仍在运行"):
            migration.migrate_legacy(source)
    finally:
        owner.close()
    migration.migrate_legacy(source)
    assert_history(config.data_dir() / "trainer.db")
