"""Precise ownership inventory; never turn a configured path into rmtree(root)."""
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from .bank_storage import atomic_json


def safe_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"拒绝链接或重解析点：{parent}")
    return path


def learning_database(path):
    safe_path(path)
    try:
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
        try:
            expected = {"quizzes": {"quiz_id", "kp_id", "snapshot", "status", "project_root", "created_at", "expires_at"},
                        "attempts": {"id", "quiz_id", "answer", "normalized_answer", "correct", "answered_at"},
                        "concept_state": {"kp_id", "mastery", "attempts", "next_review_at", "ease", "streak"}}
            return all(columns <= {r[1] for r in conn.execute(f"PRAGMA table_info({table})")} for table, columns in expected.items())
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def identity(path):
    info = safe_path(path).stat()
    return [info.st_dev, info.st_ino]


def registry_path():
    from . import config
    # Isolated/test profiles keep their registry local unless the production
    # desktop explicitly registers them in the current user's location index.
    return Path(config.USER_DATA_ROOT) / ".bagu-locations.json"


def global_registry():
    from . import config
    if os.environ.get("BAGU_TEST_INSTANCE"):
        return registry_path()
    return Path(os.environ.get("LOCALAPPDATA", str(config.USER_DATA_ROOT.parent))) / "BaguTrainer.locations.json"


def register_profile(*, global_index=False):
    from . import config
    root = safe_path(config.USER_DATA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    settings = config.read_user_config()
    if settings and not {"llm", "project_roots", "db_path"}.intersection(settings):
        raise ValueError("自定义目录的配置不属于本应用，拒绝登记")
    db = safe_path(config.db_path(settings.get("db_path") or ""))
    marker = root / ".bagu-profile.json"
    safe_path(marker)
    old = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    databases = {d["path"]: d for d in old.get("databases", [])}
    if db.exists() and learning_database(db):
        databases[str(db)] = {"path": str(db), "identity": identity(db)}
    record = {"schema": 1, "application": "BaguTrainer", "root": str(root), "identity": identity(root),
              "database": str(db), "database_identity": identity(db) if db.exists() and learning_database(db) else None}
    record["databases"] = list(databases.values())
    atomic_json(root / ".bagu-profile.json", record)
    for index in {registry_path(), global_registry()} if global_index else {registry_path()}:
        safe_path(index)
        existing = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {"profiles": []}
        profiles = [p for p in existing["profiles"] if p["root"] != str(root)]
        atomic_json(index, {"schema": 1, "profiles": [*profiles, record]})


def profile_files(root):
    """Only recognized application files, validated trees and exact DB sidecars.

    Unknown files in a shared directory remain untouched. Any link, source
    repository, or unexpected file within a managed tree stops that tree.
    """
    import re
    from .bank_cleanup import LEGACY_FILES
    root = safe_path(root)
    if (root / ".git").exists():
        raise ValueError(f"数据位置是源码仓库，拒绝清理：{root}")
    files, protected = [], []
    settings_path = root / "config.json"
    safe_path(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    marker = root / ".bagu-profile.json"
    safe_path(marker)
    if marker.exists():
        owner = json.loads(marker.read_text(encoding="utf-8"))
        if (owner.get("application") != "BaguTrainer" or owner.get("root") != str(root)
                or owner.get("identity") != identity(root)):
            raise ValueError("数据位置归属标记不匹配")
    elif settings and not {"llm", "project_roots", "db_path"}.intersection(settings):
        raise ValueError("自定义配置归属未确认，已保留")
    projects = [Path(p).absolute() for p in settings.get("project_roots", []) if isinstance(p, str)]

    def add(path, tree=False):
        safe_path(path)
        if not path.exists():
            return
        if path.is_dir():
            if not tree:
                raise ValueError(f"预期文件却是目录：{path}")
            if (path / ".git").exists() or any(p == path or path in p.parents or p in path.parents for p in projects):
                raise ValueError(f"对象涉及监听项目或源码仓库，拒绝递归清理：{path}")
            for child in sorted(path.iterdir()):
                if child.name == ".git":
                    raise ValueError(f"发现源码仓库：{child}")
                add(child, tree=True)
        elif stat.S_ISREG(path.stat().st_mode):
            files.append({"path": str(path), "identity": identity(path)})
        else:
            raise ValueError(f"不是普通文件：{path}")

    for name in ("config.json", "window.json", "migration.json", ".bagu-profile.json", ".bagu-locations.json"):
        add(root / name)
    for path in root.glob(".config.json*.tmp"):
        add(path)
    # Config backups produced by this application are explicitly registered;
    # arbitrary user-named copies are not inferred by filename similarity.
    marker = root / ".bagu-profile.json"
    record = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    for value in record.get("managed_files", []):
        path = Path(value)
        if path.is_absolute() or path.drive or ".." in path.parts:
            raise ValueError("登记文件越界")
        add(root / path)
    data = root / "data"
    safe_path(data)
    from .legacy_artifacts import legacy_name
    for path in data.iterdir() if data.exists() else []:
        if legacy_name(path.name):
            add(path)
        elif path.name.endswith(".tmp") and any(path.name.startswith("." + name) for name in (
                *LEGACY_FILES, "active-bank.json", "onboarding.json", "source-deletion.json")):
            add(path)
    for path in root.iterdir() if root.exists() else []:
        if re.fullmatch(r"\.migration-[0-9a-f]{32}", path.name):
            add(path, tree=True)
    for name in (*LEGACY_FILES, "active-bank.json", "onboarding.json", "migration.json", "source-deletion.json"):
        add(data / name, tree=name in {"repos", "extracted"})
    for folder in ("banks", "source-cache", ".bank-builds"):
        directory = data / folder
        safe_path(directory)
        if directory.exists():
            for path in directory.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}", path.name):
                    add(path, tree=True)
                elif folder == ".bank-builds" and path.name == "owner.lock":
                    add(path)
                else:
                    protected.append(str(path))
    for directory, names in ((root / "logs", ("desktop.log", "desktop.log.1", "desktop.log.2", "desktop.log.3")),
                             (root / "runtime", ("owner.lock", "bridge-credentials.json", "bridge-credentials.tmp"))):
        safe_path(directory)
        for name in names:
            add(directory / name)
    value = Path(settings.get("db_path") or "data/trainer.db").expanduser()
    databases = {value if value.is_absolute() else root / value, root / "data/trainer.db"}
    if record.get("database"):
        databases.add(Path(record["database"]))
    registered = {d["path"]: d["identity"] for d in record.get("databases", [])}
    databases.update(Path(path) for path in registered)
    for db in databases:
        safe_path(db)
        if db.exists():
            if not learning_database(db):
                protected.append(str(db))
                raise ValueError(f"数据库归属未确认，已保留：{db}")
            if str(db) == record.get("database") and record.get("database_identity") and identity(db) != record["database_identity"]:
                raise ValueError(f"外置数据库文件身份已变化：{db}")
            if str(db) in registered and identity(db) != registered[str(db)]:
                raise ValueError(f"已登记数据库文件身份已变化：{db}")
            for suffix in ("", "-wal", "-shm", "-journal"):
                add(Path(str(db) + suffix))
        # Registered deletion backups are specific files, not every .db file.
        journal = data / "source-deletion.json"
        if journal.exists():
            state = json.loads(journal.read_text(encoding="utf-8"))
            for key in ("backup", "replacement"):
                backup = Path(state[key])
                if (str(backup) == str(db) + state["suffix"] + (".new" if key == "replacement" else "")
                        and re.fullmatch(r"\.bagu-delete-[0-9a-f]{32}", state["suffix"])):
                    if backup.exists() and not learning_database(backup):
                        raise ValueError("删除恢复副本归属不明")
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        add(Path(str(backup) + suffix))
    return list({f["path"]: f for f in files}.values()), protected
