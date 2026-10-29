"""Feature acceptance uses only synthetic ZIPs, SQLite and memory credentials."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure.bank_cleanup import BankCleanupService
from bagu_trainer.infrastructure.bank_storage import active_bank_dir
from bagu_trainer.infrastructure.store import Store
from bagu_trainer.domain.review import next_state
from bagu_trainer.infrastructure.source_deletion import SourceDeletion
from bagu_trainer.infrastructure.app_cleanup import AppCleanup
from bagu_trainer.infrastructure.cleanup_manifest import profile_files
from bagu_trainer.infrastructure.cleanup_manifest import register_profile
from release_tests.test_bank_batch import batch, REPOS
from release_tests.test_bank_build import installed


@pytest.fixture
def expanded(batch):
    # The legacy fixture keeps its unrelated SQLite connection open. Use an
    # explicitly configured application DB, rather than deleting its file.
    batch.install.learning = batch.install.project / "learning-application.sqlite"
    settings = json.loads(batch.install.settings.read_text())
    settings["db_path"] = str(batch.install.learning)
    batch.install.settings.write_text(json.dumps(settings))
    batch.install.legacy.unlink()
    batch.service.build(REPOS)
    store = Store(batch.install.learning)
    for number, sources in enumerate(([REPOS[0]], [REPOS[1]], [REPOS[0], REPOS[1]], [REPOS[2]])):
        snapshot = {"question": "synthetic snapshot " + str(number), "sources": [
            {"repository": r, "commit": chr(97 + REPOS.index(r)) * 40} for r in sources]}
        qid = "q" + str(number)
        store.create_quiz(qid, "shared-kp", snapshot, "ready", "fixture-project", "", 1, 9999999999)
        if number != 2:
            store.record_attempt(qid, "shared-kp", "A", "A", True, number + 10,
                next_state(True, store.concept_state("shared-kp"), number + 10))
    store.close()
    return batch


def test_all_versions_learning_multisource_and_last_source(expanded):
    batch = expanded
    import urllib.request
    original_opener = batch.service.opener
    batch.service.opener = lambda request, timeout: original_opener(
        urllib.request.Request(request.full_url.replace("d" * 40, "a" * 40)), timeout)
    batch.service.build(REPOS[:2], versions={REPOS[0]: "d" * 40})
    before_calls = list(batch.calls)
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    assert len(plan.versions) == 3
    assert set(plan.learning["affected"]) == {"q0", "q2"}
    assert plan.learning["unknown"] == []
    result = cleanup.execute(plan)
    assert not result.failures, result.message
    assert batch.calls == before_calls
    for record in (batch.install.data / "banks").glob("*/sources.json"):
        assert all(e["repository"] != REPOS[0] for e in json.loads(record.read_text())["sources"])
    with closing(sqlite3.connect(batch.install.learning)) as db:
        assert db.execute("SELECT quiz_id FROM quizzes ORDER BY quiz_id").fetchall() == [("q1",), ("q3",)]
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert db.execute("SELECT attempts FROM concept_state").fetchone()[0] == 2
        assert not db.execute("SELECT * FROM quiz_sources WHERE repository=?", (REPOS[0],)).fetchall()
    result = cleanup.execute(cleanup.plan("sources", REPOS[1:]))
    assert not result.failures and result.empty
    assert json.loads((active_bank_dir(batch.install.project) / "questions.json").read_text()) == []
    with closing(sqlite3.connect(batch.install.learning)) as db:
        assert db.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert batch.calls == before_calls


def test_unknown_requires_explicit_confirmation_and_preserves_until_then(expanded):
    store = Store(expanded.install.learning)
    store.create_quiz("unknown", "unknown-id", {"kp_name": "same title"}, "delivered", "", "", 1, 9999999999)
    store.close()
    cleanup = BankCleanupService(expanded.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    assert plan.learning["unknown"] == ["unknown"]
    pointer = (expanded.install.data / "active-bank.json").read_bytes()
    with pytest.raises(ValueError, match="额外确认"):
        cleanup.execute(plan)
    assert (expanded.install.data / "active-bank.json").read_bytes() == pointer
    assert not cleanup.execute(plan, include_unknown=True).failures


def test_colliding_legacy_ids_and_orphan_history_are_unknown_not_guessed(tmp_path):
    from bagu_trainer.infrastructure.provenance import bank_index
    from bagu_trainer.infrastructure.bank_storage import atomic_json
    versions = []
    for number, repo in enumerate(REPOS[:2]):
        directory = tmp_path / str(number)
        atomic_json(directory / "sources.json", {"sources": [{"repository": repo, "directory": "Repo" + str(number), "commit": chr(97 + number) * 40}]})
        atomic_json(directory / "questions.json", [{"id": "bank-0001", "source_paths": ["Repo" + str(number) + "/exact.md"]}])
        versions.append(directory)
    index = bank_index(versions)
    assert "bank-0001" not in index  # Same ID in different repos is ambiguous.
    store = Store(tmp_path / "legacy.db")
    try:
        store.create_quiz("unknown", "bank-0001", {"kp_name": "same title"}, "ready", "", "", 1, 99)
        store.create_quiz("exact-path", "bank-0001", {"source_paths": ["Repo1/exact.md"]}, "ready", "", "", 1, 99)
        store._conn.execute("INSERT INTO attempts (quiz_id,answer,correct,answered_at) VALUES ('orphan','legacy',0,1)")
        store._conn.execute("INSERT INTO concept_state (kp_id,attempts) VALUES ('unknown-state',1)")
        store._conn.commit()
        manifest = store.source_manifest([REPOS[0]], index)
        assert manifest["affected"] == []
        assert set(manifest["unknown"]) == {"unknown", "orphan"}
        assert manifest["unknown_states"] == ["unknown-state"]
        assert manifest["associations"]["exact-path"] == [{"repository": REPOS[1], "commit": "b" * 40}]
        store.remove_sources(manifest, include_unknown=True)
        assert store.get_quiz("exact-path") is not None and store.get_quiz("unknown") is None
        assert store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM concept_state").fetchone()[0] == 0
    finally:
        store.close()


def test_missing_local_retained_source_never_downloads_and_rolls_back(expanded, monkeypatch):
    import shutil
    for path in (expanded.install.data / "source-cache").iterdir():
        shutil.rmtree(path)
    for path in (expanded.install.data / "banks").glob("*/repos"):
        shutil.rmtree(path)
    cleanup = BankCleanupService(expanded.service)
    before = (expanded.install.data / "active-bank.json").read_bytes()
    monkeypatch.setattr(expanded.service, "opener", lambda *a, **k: pytest.fail("deletion accessed network"))
    with pytest.raises(Exception, match="本地|来源"):
        cleanup.execute(cleanup.plan("sources", [REPOS[0]]))
    assert (expanded.install.data / "active-bank.json").read_bytes() == before
    assert not (expanded.install.data / "source-deletion.json").exists()


def test_failure_leaves_retryable_journal_and_startup_recovers(expanded, monkeypatch):
    cleanup = BankCleanupService(expanded.service)
    original = SourceDeletion._finish
    def crash(self, state):
        raise RuntimeError("injected interruption after durable commit")
    monkeypatch.setattr(SourceDeletion, "_finish", crash)
    with pytest.raises(RuntimeError, match="injected"):
        cleanup.execute(cleanup.plan("sources", [REPOS[0]]))
    assert (expanded.install.data / "source-deletion.json").exists()
    monkeypatch.setattr(SourceDeletion, "_finish", original)
    result = SourceDeletion(cleanup).recover()
    assert not result.failures, result.message
    assert not (expanded.install.data / "source-deletion.json").exists()


def test_real_process_interruption_before_switch_rolls_back_and_can_delete_again(expanded, monkeypatch):
    import os
    import subprocess
    import sys
    monkeypatch.chdir(expanded.install.project)
    before = (expanded.install.data / "active-bank.json").read_bytes()
    with closing(sqlite3.connect(expanded.install.learning)) as conn:
        original = list(conn.iterdump())
    child = r'''
import os, sys
from bagu_trainer.infrastructure.bank_build.service import BankBuildService
from bagu_trainer.infrastructure.bank_cleanup import BankCleanupService
from bagu_trainer.infrastructure.source_deletion import SourceDeletion
service = BankBuildService(sys.argv[1], manifest=sys.argv[2], opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden')))
original = SourceDeletion._write
def interrupt(self, state):
    original(self, state)
    if state['phase'] == 'switching':
        os._exit(77)
SourceDeletion._write = interrupt
cleanup = BankCleanupService(service)
cleanup.execute(cleanup.plan('sources', ['fixture/first']))
'''
    environment = dict(os.environ, BAGU_USER_DATA_DIR=str(expanded.install.project), BAGU_TEST_INSTANCE="interruption-fixture",
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    process = subprocess.run([sys.executable, "-B", "-c", child, str(expanded.install.project), str(expanded.install.manifest)],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, timeout=15)
    assert process.returncode == 77, process.stderr.decode(errors="replace")
    cleanup = BankCleanupService(expanded.service)
    assert (expanded.install.data / "source-deletion.json").exists()
    assert SourceDeletion(cleanup).recover() is None
    assert (expanded.install.data / "active-bank.json").read_bytes() == before
    with closing(sqlite3.connect(expanded.install.learning)) as conn:
        assert list(conn.iterdump()) == original
    assert not cleanup.execute(cleanup.plan("sources", [REPOS[0]])).failures


def test_stale_approval_and_build_lock_are_rejected(expanded):
    from bagu_trainer.infrastructure.bank_build.service import _build_lock
    cleanup = BankCleanupService(expanded.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    store = Store(expanded.install.learning)
    store.create_quiz("late", "late", {"sources": [{"repository": REPOS[1], "commit": "b" * 40}]}, "ready", "", "", 1, 99)
    store.close()
    with pytest.raises(ValueError, match="变化"):
        cleanup.execute(plan)
    with _build_lock(expanded.install.data / ".bank-builds"):
        with pytest.raises(Exception, match="运行"):
            cleanup.execute(plan)


def test_legacy_mixed_backups_fallback_and_concepts_are_partitioned_and_recoverable(expanded, monkeypatch):
    from bagu_trainer.infrastructure.bank_storage import atomic_json
    data = expanded.install.data
    backup = data / "questions_backup_pre_source_excerpt.json"
    a = {"id": "legacy-a", "question": "A content", "sources": [{"repository": REPOS[0], "commit": "a" * 40}]}
    b = {"id": "legacy-b", "question": "B content", "sources": [{"repository": REPOS[1], "commit": "b" * 40}]}
    unknown = {"id": "old-unknown", "question": "old content"}
    atomic_json(backup, [a, b, unknown])
    atomic_json(data / "fallback_quizzes.json", [a, b])
    current = active_bank_dir(expanded.install.project)
    concepts = json.loads((current / "question_concepts.json").read_text(encoding="utf-8"))
    concept_backup = data / "question_concepts_backup_pre_concept_rebuild_20261024_120000.json"
    atomic_json(concept_backup, concepts)
    cleanup = BankCleanupService(expanded.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    assert sum(r.unknown for r in plan.legacy) == 1
    with pytest.raises(ValueError, match="额外确认"):
        cleanup.execute(plan)
    original_finish = SourceDeletion._finish
    monkeypatch.setattr(SourceDeletion, "_finish", lambda *a: (_ for _ in ()).throw(RuntimeError("interrupt legacy cleanup")))
    with pytest.raises(RuntimeError, match="interrupt"):
        cleanup.execute(plan, include_unknown=True)
    assert json.loads(backup.read_text(encoding="utf-8")) == [b]
    monkeypatch.setattr(SourceDeletion, "_finish", original_finish)
    result = SourceDeletion(cleanup).recover()
    assert not result.failures, result.message
    assert json.loads(backup.read_text(encoding="utf-8")) == [b]
    assert json.loads((data / "fallback_quizzes.json").read_text(encoding="utf-8")) == [b]
    assert len(json.loads(concept_backup.read_text(encoding="utf-8"))["profiles"]) == 2
    assert not [p for p in (data / ".bank-builds").iterdir() if p.is_dir()]


def test_uninstall_includes_migration_receipt_staging_and_known_legacy_backups(tmp_path):
    import uuid
    from bagu_trainer.infrastructure.bank_storage import atomic_json
    profile = tmp_path / "profile"
    original_project = tmp_path / "original-source"
    original_project.mkdir()
    (original_project / "keep.py").write_text("keep source")
    atomic_json(profile / "config.json", {"project_roots": [str(original_project)], "db_path": "data/trainer.db"})
    Store(profile / "data/trainer.db").close()
    atomic_json(profile / "migration.json", {"source": str(original_project), "database": "data/trainer.db"})
    atomic_json(profile / "data/questions_backup_pre_knowledge_tags_20261024_120000.json", [{"id": "old", "question": "old"}])
    atomic_json(profile / "data/fallback_quizzes.json", [])
    temporary = profile / (".migration-" + uuid.uuid4().hex)
    atomic_json(temporary / "config.json", {"llm": {"api_key": "synthetic-interrupted-migration-key"}})
    (profile / "data/.questions.json-interrupted.tmp").write_text("partial synthetic question")
    cleaner = AppCleanup([profile], tmp_path / "uninstall.json", credential_services=[])
    result = cleaner.execute(environment=lambda: [])
    assert result["ok"], result
    assert not profile.exists()
    assert (original_project / "keep.py").read_text() == "keep source"


class Credentials:
    def __init__(self):
        self.values = {("bagu-trainer.profile." + "a" * 24, "llm_api_key"): "synthetic-not-a-real-key"}
        self.fail = False

    def get_password(self, service, user):
        if self.fail:
            raise PermissionError("injected vault failure")
        return self.values.get((service, user))

    def delete_password(self, service, user):
        self.values.pop((service, user), None)


def test_uninstall_uses_authoritative_windows_vault_not_runtime_fallback(monkeypatch):
    from bagu_trainer.infrastructure import secrets
    import keyring.backends.Windows
    vault = Credentials()
    monkeypatch.setattr(keyring.backends.Windows, "WinVaultKeyring", lambda: vault)
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    service = next(iter(vault.values))[0]
    assert secrets.remove_credential(service) == "deleted"
    assert not vault.values
    assert secrets.remove_credential(service) == "absent"
    vault.fail = True
    with pytest.raises(PermissionError):
        secrets.remove_credential(service)


def test_pending_uninstall_blocks_new_app_writes_and_reopens_retry_tool(tmp_path, monkeypatch):
    from bagu_trainer.desktop import cleanup_cli
    from bagu_trainer import desktop
    from bagu_trainer.infrastructure import desktop_instance
    from bagu_trainer.desktop import runtime
    pending = tmp_path / "pending-uninstall.json"
    pending.write_text('{"schema":1}')
    monkeypatch.setattr(cleanup_cli, "journal_path", lambda: pending)
    handoffs = []
    monkeypatch.setattr(cleanup_cli, "launch_uninstall", lambda: handoffs.append(True))
    monkeypatch.setattr(desktop_instance.OwnerLock, "acquire", lambda *a: pytest.fail("new app acquired a profile during pending uninstall"))
    monkeypatch.setattr(runtime, "configure_logs", lambda: pytest.fail("pending uninstall recreated application logs"))
    assert runtime.run([]) == 2
    assert handoffs == [True]
    assert pending.read_text() == '{"schema":1}'


def test_complete_cleanup_retry_shared_directory_external_db_and_reinstall(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    root.mkdir()
    other = root / "other-software.txt"
    other.write_text("keep")
    project = root / "code-project"
    project.mkdir()
    (project / ".git").mkdir()
    external = tmp_path / "other-shared-dir" / "learning.sqlite"
    store = Store(external)
    store.create_quiz("private", "kp", {}, "ready", str(project), "", 1, 99)
    store.close()
    (root / "config.json").write_text(json.dumps({"db_path": str(external), "project_roots": [str(project)],
        "llm": {"api_key": "synthetic-legacy-test-key"}}))
    monkeypatch.setattr(config, "USER_DATA_ROOT", root)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.json")
    register_profile()
    vault = Credentials()
    cleaner = AppCleanup([root], tmp_path / "uninstall.json", credential_services=[next(iter(vault.values))[0]])
    plan = cleaner.plan()
    vault.fail = True
    failed = cleaner.execute(plan, backend=vault, environment=lambda: [])
    assert not failed["ok"] and "凭据" in failed["failures"][0]
    assert cleaner.journal.exists() and other.read_text() == "keep" and (project / ".git").is_dir()
    vault.fail = False
    assert cleaner.execute(backend=vault, environment=lambda: [])["ok"]
    assert not external.exists() and external.parent.is_dir()
    assert not vault.values and not cleaner.journal.exists()
    assert not (root / "config.json").exists()
    register_profile()
    assert not config.read_user_config()
    assert not external.exists()


def test_cleanup_rejects_foreign_db_and_source_repository(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    db = tmp_path / "other.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
    (root / "config.json").write_text(json.dumps({"db_path": str(db)}))
    with pytest.raises(ValueError, match="归属"):
        profile_files(root)
    assert db.exists()
    (root / "config.json").unlink()
    repo = root / "data" / "repos" / ".git"
    repo.mkdir(parents=True)
    with pytest.raises(ValueError, match="源码仓库"):
        profile_files(root)


def test_junction_and_replaced_external_database_are_rejected(tmp_path, monkeypatch):
    import _winapi
    import os
    root = tmp_path / "data-profile"
    root.mkdir()
    external = tmp_path / "external.db"
    Store(external).close()
    (root / "config.json").write_text(json.dumps({"db_path": str(external)}))
    monkeypatch.setattr(config, "USER_DATA_ROOT", root)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.json")
    register_profile()
    replacement = tmp_path / "replacement.db"
    Store(replacement).close()
    os.replace(replacement, external)
    with pytest.raises(ValueError, match="身份"):
        profile_files(root)
    register_profile()  # Deliberate re-registration by the application.
    outside = tmp_path / "foreign-project"
    outside.mkdir()
    (outside / "keep.txt").write_text("foreign")
    link = root / "data" / "banks"
    link.parent.mkdir()
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(ValueError, match="重解析"):
            profile_files(root)
        assert (outside / "keep.txt").read_text() == "foreign"
    finally:
        link.rmdir()


def test_cleanup_file_lock_is_reported_and_retry_verifies(tmp_path):
    import ctypes
    from ctypes import wintypes
    from bagu_trainer.infrastructure.desktop_instance import OwnerLock
    from bagu_trainer.infrastructure.cleanup_manifest import identity
    root = tmp_path / "profile"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"project_roots": []}))
    owner = OwnerLock(root / "runtime")
    assert owner.acquire()
    owner.close()
    lock_path = root / "runtime/owner.lock"
    original_identity = identity(lock_path)
    cleaner = AppCleanup([root], tmp_path / "retry.json", credential_services=[])
    plan = cleaner.plan()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(root / "config.json"), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        failed = cleaner.execute(plan, environment=lambda: [])
        assert not failed["ok"] and any("config.json" in failure for failure in failed["failures"])
        assert cleaner.journal.exists()
        assert lock_path.exists(), "Keep maintenance lock identity until data cleanup succeeds"
    finally:
        kernel.CloseHandle(handle)
    assert owner.acquire()
    owner.close()
    assert identity(lock_path) == original_identity
    assert cleaner.execute(environment=lambda: [])["ok"]
    assert not cleaner.journal.exists() and not (root / "config.json").exists() and not lock_path.exists()


def test_publishing_waits_for_short_lived_inventory_read_handle(expanded):
    import threading
    import time
    opened = []
    released = threading.Event()
    workers = []
    def progress(event):
        if event.stage == "activate" and not opened:
            path = expanded.install.data / ".bank-builds" / event.generation / "sources.json"
            handle = path.open("rb")
            opened.append(handle)
            def release():
                time.sleep(.2)
                handle.close()
                released.set()
            worker = threading.Thread(target=release)
            workers.append(worker)
            worker.start()
    try:
        result = expanded.service.build(REPOS, progress=progress)
        assert result.questions == 3 and result.directory == active_bank_dir(expanded.install.project)
        assert released.is_set()
    finally:
        for worker in workers:
            worker.join(2)
        assert all(handle.closed for handle in opened)
