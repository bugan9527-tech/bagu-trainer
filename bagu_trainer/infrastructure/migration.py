"""Explicit, copy-only migration from legacy project-local data."""
import hashlib
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure import secrets
from bagu_trainer.infrastructure.bank_storage import atomic_json
from bagu_trainer.infrastructure.store import Store


class MigrationError(Exception):
    pass


def migrate_legacy(source, *, cancel=None, progress=None):
    source = Path(source).expanduser().resolve(strict=True)
    target = Path(config.USER_DATA_ROOT).resolve()
    cancel = cancel if cancel is not None else threading.Event()
    def check():
        if cancel.is_set():
            raise MigrationError("迁移已取消，旧目录保持不变，可重新尝试。")
    def report(text):
        check()
        if progress:
            progress(text)
    if not source.is_dir() or source == target or target.is_relative_to(source / "data"):
        raise MigrationError("请选择旧版项目目录，不能选择当前用户数据目录。")
    if not (source / "config.json").is_file() and not (source / "data").is_dir():
        raise MigrationError("所选目录没有旧版 config.json 或 data，请选择旧项目目录。")
    completed = target / "migration.json"
    if completed.exists():
        receipt = json.loads(completed.read_text(encoding="utf-8"))
        if receipt.get("source") == str(source):
            return {"already_migrated": True, "target": str(target)}
    # Runtime lock and logs are outside data/, so this remains usable from the wizard.
    data_target = target / "data"
    if Path(config.CONFIG_PATH).exists() or (data_target.exists() and
            any(path.name != "onboarding.json" for path in data_target.iterdir())):
        raise MigrationError("当前用户目录已有配置、题库或学习数据，迁移不会覆盖。请先保留并核对现有数据。")
    legacy_lock = source / "data" / ".desktop-runtime"
    owner = None
    if os.name == "nt" and (legacy_lock / "owner.lock").exists():
        from bagu_trainer.infrastructure.desktop_instance import OwnerLock
        owner = OwnerLock(legacy_lock)
        if not owner.acquire():
            raise MigrationError("旧版程序仍在运行，请退出旧版后重新迁移。")
    staging = target / (".migration-" + uuid.uuid4().hex)
    data = staging / "data"
    old_placeholder = staging / "previous-data"
    activated = False
    try:
        target.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        data.mkdir()
        source_config = source / "config.json"
        cfg = json.loads(source_config.read_text(encoding="utf-8-sig")) if source_config.exists() else {}
        if not isinstance(cfg, dict):
            raise MigrationError("旧 config.json 格式无效，请修复后重试。")
        report("正在复制并校验旧题库…")
        def copy_file(original, destination):
            check()
            original.resolve(strict=True).relative_to(source)
            if original.is_symlink():
                raise MigrationError("旧数据中包含链接，请先复制为普通文件再迁移。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with original.open("rb") as incoming, destination.open("xb") as outgoing:
                while True:
                    check()
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    outgoing.write(block)
            restored = hashlib.sha256()
            with destination.open("rb") as incoming:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    check()
                    restored.update(block)
            if digest.digest() != restored.digest():
                raise MigrationError("旧题库复制校验失败，请重新尝试。")
        def copy_tree(original, destination):
            original.resolve(strict=True).relative_to(source)
            if original.is_symlink():
                raise MigrationError("旧题库目录包含链接，无法安全迁移。")
            for item in original.iterdir():
                check()
                if item.is_dir():
                    copy_tree(item, destination / item.name)
                elif item.is_file():
                    copy_file(item, destination / item.name)
        old_data = source / "data"
        if old_data.is_dir():
            allowed = {"question_concepts.json", "fallback_quizzes.json", "active-bank.json", "onboarding.json",
                       "knowledge_tag_report.json", "knowledge_tag_report.csv", "title_cleanup_report.json",
                       "concept_profile_report.json"}
            for item in old_data.iterdir():
                if item.name == "banks" and item.is_dir():
                    copy_tree(item, data / "banks")
                elif item.is_file() and (item.name in allowed or
                                        (item.name.startswith("questions") and item.suffix == ".json")):
                    copy_file(item, data / item.name)
        configured_db = cfg.get("db_path") or ""
        original_db = Path(configured_db).expanduser() if configured_db else source / "data" / "trainer.db"
        if not original_db.is_absolute():
            original_db = source / original_db
        if original_db.exists():
            report("正在快照学习数据库并检查完整性…")
            # sqlite Connection.__exit__ commits but does not close the handle.
            # Close both before publishing directories (Windows denies renaming open DBs).
            if not Store.backup_database(original_db, data / "trainer.db", pages=128,
                                         progress=lambda *_: check(), verify=True):
                raise MigrationError("旧学习数据库完整性检查失败，未切换数据。")
        elif configured_db:
            raise MigrationError("配置指定的旧学习数据库不存在，请检查 db_path 后重试。")
        cfg["db_path"] = "data/trainer.db"
        roots = cfg.get("project_roots") or []
        if not isinstance(roots, list):
            raise MigrationError("旧监听项目配置不是列表，请修复后重试。")
        cfg["project_roots"] = [str((Path(root) if Path(root).is_absolute() else source / root).resolve()) for root in roots]
        llm = cfg.get("llm")
        if isinstance(llm, dict):
            key = (llm.get("api_key") or "").strip()
            cfg["llm"] = {**llm, "api_key": ""}
            if key and (secrets.save_api_key(key) != "keyring" or not secrets._keyring_matches(key)):
                raise MigrationError("旧 Key 未能确认存入 Windows 凭据管理器，请修复凭据服务后重试；未写入明文配置。")
        atomic_json(staging / "config.json", cfg)
        report("迁移校验完成，正在启用用户数据…")
        # Publish only after all copies/SQLite/key-store checks pass. Roll back
        # ordinary failures; original source files are never moved or deleted.
        if data_target.exists():
            os.replace(data_target, old_placeholder)
        try:
            os.replace(data, data_target)
            activated = True
            config._write_config_atomic(cfg)
            atomic_json(completed, {"source": str(source), "database": "data/trainer.db"})
        except Exception:
            if activated:
                data_target.resolve().relative_to(target)
                shutil.rmtree(data_target)
            if old_placeholder.exists():
                os.replace(old_placeholder, data_target)
            Path(config.CONFIG_PATH).unlink(missing_ok=True)
            raise
        return {"already_migrated": False, "target": str(target)}
    except MigrationError:
        raise
    except Exception as error:
        raise MigrationError(f"迁移失败（{type(error).__name__}），旧数据保留，请检查路径、文件格式和权限后重试。") from None
    finally:
        if owner is not None:
            owner.close()
        staging.resolve().relative_to(target)
        if staging.exists():
            shutil.rmtree(staging)
