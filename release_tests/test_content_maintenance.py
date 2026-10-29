"""Maintenance Interface exercised with isolated banks/SQLite and fake models."""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bagu_trainer.application.maintenance import ContentMaintenance
from bagu_trainer.infrastructure.bank_cleanup import BankCleanupService
from bagu_trainer.infrastructure.bank_cleanup import CleanupResult
from bagu_trainer.infrastructure.bank_storage import active_bank_dir
from bagu_trainer.bootstrap import create_engine
from bagu_trainer.application.learning.engine import EngineSession
from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase
from bagu_trainer.infrastructure.source_deletion import SourceDeletion
from bagu_trainer.desktop.startup import StartupCoordinator
from bagu_trainer.desktop.startup import StartupReport
from bagu_trainer.desktop.startup import ReadinessCheck
from bagu_trainer.infrastructure.store import Store
from release_tests.test_bank_build import installed
from release_tests.test_bank_batch import batch, REPOS
from release_tests.test_feature_deletion import expanded
from tests.test_engine_instances import Generator


class Ranker:
    def __init__(self, bank):
        self.bank = bank

    def rank(self, _context, top_n=1):
        questions = [q for q in self.bank.all()
                     if any(s['repository'] == REPOS[0] for s in q.get('sources', []))]
        return [{"question": q, "score": 5, "confidence": 1, "margin": 1, "hits": []}
                for q in questions[:top_n]]


@pytest.fixture
def live(expanded):
    root = str(expanded.install.project)
    engine = create_engine(
        cfg={"project_roots": [root], "max_quizzes_per_hit": 1,
             "quiz_ttl_seconds": 3600, "max_pending_quizzes": 20,
             "max_quizzes": 200, "seen_hash_capacity": 100},
        knowledge_base_factory=lambda: KnowledgeBase(active_bank_dir(root) / "questions.json"),
        matcher_factory=Ranker, generator=Generator(),
        session=EngineSession(database=str(expanded.install.learning)))
    engine.learning.apply_project_scope([root])
    # Hydrate any legacy pending rows before creating a deletion approval.
    engine.learning.pending_questions()
    events = []
    maintenance = ContentMaintenance(cleanup=BankCleanupService(expanded.service),
        engine=lambda: engine, before_change=lambda: events.append("reset"),
        completed=lambda: events.append("complete"),
        unavailable=lambda: events.append("unavailable"))
    yield expanded, engine, maintenance, events
    maintenance.close()
    engine.close()


def generate(engine, root):
    return engine.learning.generate_for_activity(str(root),
        {"context": "synthetic database index changed", "sources": []})


def test_approval_is_single_use_and_new_preview_retires_old(live):
    batch, engine, maintenance, events = live
    old = maintenance.plan("sources", [REPOS[0]])
    current = maintenance.plan("sources", [REPOS[0]])
    # Changing presentation data cannot change the approved DB manifest.
    current.learning.clear()
    with pytest.raises(ValueError, match="失效"):
        maintenance.clean(old.token)
    result = maintenance.clean(current.token)
    assert not result.failures
    assert events == ["reset", "reset", "complete"]
    assert engine.learning.history()["total"] == 2
    with pytest.raises(ValueError, match="失效"):
        maintenance.clean(current.token)


@pytest.mark.parametrize("close_before_result", [False, True])
def test_late_generation_cannot_restore_deleted_content_or_closed_session(live, close_before_result):
    batch, engine, maintenance, _ = live
    started, release = threading.Event(), threading.Event()
    original = engine.generator.generate
    def blocked(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    engine.generator.generate = blocked
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(generate, engine, batch.install.project)
            assert started.wait(2)
            preview = maintenance.plan("sources", [REPOS[0]])
            assert not maintenance.clean(preview.token).failures
            if close_before_result:
                maintenance.close()
                engine.close()
            release.set()
            result = future.result(3)
            assert result["result"][1] == [] and not result["current"]
    finally:
        release.set()
    assert Store.history_page(batch.install.learning)["total"] == 2
    if close_before_result:
        with pytest.raises(RuntimeError):
            maintenance.apply_bank()
        with pytest.raises(RuntimeError):
            maintenance.recover()
        with pytest.raises(RuntimeError):
            engine.learning.resume_practice()
    else:
        assert engine.learning.pending_questions() == []


def test_answer_waiting_for_cleanup_cannot_write_deleted_quiz(live):
    batch, engine, maintenance, _ = live
    quiz_id = generate(engine, batch.install.project)["result"][1][0]["quiz_id"]
    preview = maintenance.plan("sources", [REPOS[0]])
    entered, release, answering = threading.Event(), threading.Event(), threading.Event()
    def progress(message):
        if message.startswith("3/5"):
            entered.set()
            assert release.wait(10)
    def answer():
        answering.set()
        return engine.learning.submit_answer(quiz_id, "A")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(maintenance.clean, preview.token, progress=progress)
            assert entered.wait(2)
            submission = pool.submit(answer)
            assert answering.wait(2)
            assert not submission.done()
            release.set()
            assert not cleanup.result(5).failures
            assert not submission.result(3)["ok"]
    finally:
        release.set()
    assert Store.history_page(batch.install.learning)["total"] == 2
    assert Store.source_manifest_at(batch.install.learning, [REPOS[0]], {})["affected"] == []


def test_answer_committed_first_invalidates_deletion_approval(live, monkeypatch):
    batch, engine, maintenance, _ = live
    quiz_id = generate(engine, batch.install.project)["result"][1][0]["quiz_id"]
    preview = maintenance.plan("sources", [REPOS[0]])
    entered, release, cleaning = threading.Event(), threading.Event(), threading.Event()
    original = engine.scorer.grade
    def grade(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(engine.scorer, "grade", grade)
    pointer = (batch.install.data / "active-bank.json").read_bytes()
    def clean():
        cleaning.set()
        return maintenance.clean(preview.token)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(engine.learning.submit_answer, quiz_id, "A")
            assert entered.wait(2)
            cleanup = pool.submit(clean)
            assert cleaning.wait(2)
            assert not cleanup.done()
            release.set()
            assert submission.result(3)["ok"]
            with pytest.raises(ValueError, match="变化|确认"):
                cleanup.result(5)
    finally:
        release.set()
    assert (batch.install.data / "active-bank.json").read_bytes() == pointer
    maintenance.recover()
    assert engine.learning.history()["total"] == 4
    assert not engine.learning.submit_answer(quiz_id, "A")["ok"]


def test_reload_reads_files_only_after_cleanup_releases_session(live):
    batch, engine, maintenance, _ = live
    preview = maintenance.plan("sources", [REPOS[0]])
    entered, release, loading, requested = (threading.Event() for _ in range(4))
    original = engine.knowledge_base_factory
    def load():
        loading.set()
        return original()
    engine.knowledge_base_factory = load
    def progress(message):
        if message.startswith("3/5"):
            entered.set()
            assert release.wait(10)
    def reload():
        requested.set()
        engine.learning.replace_question_bank()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(maintenance.clean, preview.token, progress=progress)
            assert entered.wait(2)
            reloader = pool.submit(reload)
            assert requested.wait(2)
            assert not loading.wait(.1), "reload read old files while deletion held the session"
            release.set()
            assert not cleanup.result(5).failures
            reloader.result(3)
    finally:
        release.set()
    assert loading.is_set()
    assert generate(engine, batch.install.project)["result"][0] == []


def test_interrupted_finish_blocks_application_then_recovery_is_idempotent(live, monkeypatch):
    batch, engine, maintenance, events = live
    original = SourceDeletion._finish
    def interrupt(*_args):
        raise OSError("synthetic interrupted finish")
    monkeypatch.setattr(SourceDeletion, "_finish", interrupt)
    preview = maintenance.plan("sources", [REPOS[0]])
    with pytest.raises(OSError, match="synthetic"):
        maintenance.clean(preview.token)
    assert maintenance.recovery_pending() and "complete" not in events
    assert "unavailable" in events
    with pytest.raises(ValueError, match="恢复"):
        maintenance.apply_bank()
    monkeypatch.setattr(SourceDeletion, "_finish", original)
    assert not maintenance.recover().failures
    assert events.count("complete") == 1
    assert maintenance.recover() is None
    assert events.count("complete") == 1
    assert not maintenance.recovery_pending()
    assert engine.learning.history()["total"] == 2


@pytest.mark.parametrize("phase", ["preparing", "switching", "cleaning"])
def test_process_interruption_recovers_via_normal_startup_and_repeats_safely(expanded, phase):
    before = (expanded.install.data / "active-bank.json").read_bytes()
    child = r'''
import os, sys
from bagu_trainer.application.maintenance import ContentMaintenance
from bagu_trainer.infrastructure.bank_build.service import BankBuildService
from bagu_trainer.infrastructure.bank_cleanup import BankCleanupService
from bagu_trainer.infrastructure.source_deletion import SourceDeletion
service = BankBuildService(sys.argv[1], manifest=sys.argv[2], opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden')))
original = SourceDeletion._write
def interrupt(self, state):
    original(self, state)
    if state['phase'] == sys.argv[3]:
        os._exit(77)
SourceDeletion._write = interrupt
maintenance = ContentMaintenance(cleanup=BankCleanupService(service))
maintenance.clean(maintenance.plan('sources', ['fixture/first']).token)
'''
    environment = dict(os.environ, BAGU_USER_DATA_DIR=str(expanded.install.project),
        BAGU_TEST_INSTANCE="maintenance-interruption-fixture",
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    process = subprocess.run([sys.executable, "-B", "-c", child,
        str(expanded.install.project), str(expanded.install.manifest), phase],
        env=environment, capture_output=True, timeout=15)
    assert process.returncode == 77, process.stderr.decode(errors="replace")
    loads = []
    def load():
        assert not coordinator.maintenance.recovery_pending(), "engine loaded before recovery"
        loads.append(1)
        return object()
    coordinator = StartupCoordinator(
        checker=lambda: StartupReport((ReadinessCheck("synthetic", True, "ready"),)),
        engine_loader=load)
    maintenance = coordinator.maintenance
    assert maintenance.recovery_pending()
    assert coordinator.prepare() is not None and coordinator.report.ready
    assert loads == [1] and maintenance.recover() is None
    assert coordinator.prepare() is coordinator.engine and loads == [1]
    assert Store.history_page(expanded.install.learning)["total"] == (2 if phase == "cleaning" else 3)
    if phase != "cleaning":
        assert (expanded.install.data / "active-bank.json").read_bytes() == before
    else:
        for record in (expanded.install.data / "banks").glob("*/sources.json"):
            assert all(s["repository"] != REPOS[0] for s in json.loads(record.read_text())["sources"])


def test_bad_recovery_record_blocks_startup_without_loading_engine(expanded):
    journal = expanded.install.data / "source-deletion.json"
    journal.write_text('{"schema":999}', encoding="utf-8")
    maintenance = ContentMaintenance(cleanup=BankCleanupService(expanded.service))
    coordinator = StartupCoordinator(engine_loader=lambda: pytest.fail("engine loaded"), maintenance=maintenance)
    assert coordinator.prepare() is None
    assert coordinator.state == "failed" and "ValueError" in coordinator.report.error
    assert journal.exists()


def test_history_cleanup_and_bank_application_preserve_pending_and_submission_once(live):
    batch, engine, maintenance, events = live
    quiz_id = generate(engine, batch.install.project)["result"][1][0]["quiz_id"]
    batch.service.build(REPOS[:2])
    maintenance.apply_bank()
    preview = maintenance.plan("history")
    assert not maintenance.clean(preview.token).failures
    assert events.count("complete") == 1  # A bank build must not emit a deletion event.
    assert engine.learning.enter_question(quiz_id)["quiz_id"] == quiz_id
    assert engine.learning.submit_answer(quiz_id, "A")["ok"]
    assert not engine.learning.submit_answer(quiz_id, "A")["ok"]
    assert engine.learning.history()["total"] == 4


def test_late_bank_application_never_reopens_closed_engine(live):
    _, engine, maintenance, events = live
    engine.close()
    with pytest.raises(RuntimeError, match="关闭"):
        maintenance.apply_bank()
    assert "complete" not in events
    assert maintenance.recover() is None
    with pytest.raises(RuntimeError, match="关闭"):
        engine.learning.resume_practice()


def test_failed_reload_blocks_learning_until_public_recovery(live):
    batch, engine, maintenance, _ = live
    original = engine.knowledge_base_factory
    def fail():
        raise OSError("synthetic unreadable bank")
    engine.knowledge_base_factory = fail
    with pytest.raises(OSError):
        maintenance.apply_bank()
    assert not maintenance.recovery_pending()  # This retry needs no new journal format.
    with pytest.raises(RuntimeError, match="恢复"):
        generate(engine, batch.install.project)
    with pytest.raises(RuntimeError, match="恢复"):
        engine.learning.submit_answer("old-quiz", "A")
    engine.knowledge_base_factory = original
    maintenance.recover()
    assert generate(engine, batch.install.project)["result"][1]


def test_partial_cleanup_keeps_journal_and_learning_blocked_until_recovered(live, monkeypatch):
    _, engine, maintenance, events = live
    original = SourceDeletion._finish
    monkeypatch.setattr(SourceDeletion, "_finish", lambda *_: CleanupResult([], ["synthetic busy file"]))
    result = maintenance.clean(maintenance.plan("sources", [REPOS[0]]).token)
    assert result.failures and maintenance.recovery_pending()
    with pytest.raises(RuntimeError, match="恢复"):
        engine.learning.pending_questions()
    assert maintenance.recover().failures and maintenance.recovery_pending()
    monkeypatch.setattr(SourceDeletion, "_finish", original)
    assert not maintenance.recover().failures
    assert engine.learning.pending_questions() == []
    assert not maintenance.recovery_pending()
    assert events.count("unavailable") == 2


def test_close_waits_for_accepted_cleanup_and_rejects_late_operations(live):
    _, _, maintenance, _ = live
    preview = maintenance.plan("sources", [REPOS[0]])
    entered, release, closing = (threading.Event() for _ in range(3))
    def progress(message):
        if message.startswith("3/5"):
            entered.set()
            assert release.wait(10)
    def close():
        closing.set()
        maintenance.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleaning = pool.submit(maintenance.clean, preview.token, progress=progress)
            assert entered.wait(2)
            closed = pool.submit(close)
            assert closing.wait(2) and not closed.done()
            release.set()
            assert not cleaning.result(5).failures
            closed.result(3)
    finally:
        release.set()
    for operation in (maintenance.plan, maintenance.recover, maintenance.apply_bank):
        with pytest.raises(RuntimeError, match="退出"):
            operation()


def test_electron_commands_preserve_maintenance_response_and_completion_event(live):
    from bagu_trainer.desktop.service import DesktopService
    _, engine, _, _ = live
    events = []
    service = DesktopService(emit=events.append)
    service.monitor.engine = engine
    service.coordinator.engine = engine
    service.monitor.set_ready(True)
    try:
        preview = service.command("bank.plan", {"kind": "sources", "repositories": [REPOS[0]]})
        assert set(preview) == {"token", "kind", "repositories", "estimated_bytes", "last_source", "learning", "targets"}
        result = service.command("bank.clean", {"token": preview["token"]})
        assert set(result) == {"ok", "note"} and result["ok"]
        assert [e for e in events if e["type"] == "content_changed"] == [{"type": "content_changed", "data": {}}]
        with pytest.raises(ValueError, match="失效"):
            service.command("bank.clean", {"token": preview["token"]})
        assert engine.learning.history()["total"] == 2
    finally:
        service.close()
