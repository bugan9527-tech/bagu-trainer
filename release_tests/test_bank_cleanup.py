"""Every deletion target is under pytest's isolated project fixture."""
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bagu_trainer.bank_cleanup import BankCleanupService, CleanupTarget
from bagu_trainer.bank_build.service import _build_lock, load_sources
from bagu_trainer.bank_build.source_cache import cache_directory
from bagu_trainer.bank_storage import active_bank_dir
from bagu_trainer.knowledge_base import KnowledgeBase
from release_tests.test_bank_batch import batch, REPOS
from release_tests.test_bank_build import installed


def fake_engine(batch):
    engine = SimpleNamespace(_STATE_LOCK=threading.RLock(), kb=KnowledgeBase(active_bank_dir(batch.install.project) / "questions.json"))
    def reload():
        engine.kb = KnowledgeBase(active_bank_dir(batch.install.project) / "questions.json")
    engine.reload_bank = reload
    return engine


def cache(batch, index):
    source = load_sources(batch.install.manifest)[REPOS[index]]
    return cache_directory(batch.install.data / "source-cache", source)


def snapshot(batch):
    return {p: p.read_bytes() for p in batch.install.data.rglob("*") if p.is_file() and p.name != "owner.lock"}


def test_unused_cache_is_deleted_without_rebuild_or_pointer_change(batch):
    batch.service.build([REPOS[1]])
    active = batch.service.build([REPOS[0]])
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[1]])
    assert not plan.requires_switch and plan.estimated_bytes > 0
    assert all(not target.used for target in plan.targets)
    count = len(batch.calls)
    result = cleanup.execute(plan, engine=fake_engine(batch))
    assert not result.failures and not result.switched
    assert not cache(batch, 1).exists()
    assert active_bank_dir(batch.install.project) == active.directory
    assert len(batch.calls) == count
    assert batch.install.learning.read_bytes() == batch.original[batch.install.learning]


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows sharing handles")
def test_transient_windows_cache_handle_can_close_before_cleanup_retries(batch):
    import ctypes
    from ctypes import wintypes
    batch.service.build([REPOS[1]])
    active = batch.service.build([REPOS[0]])
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[1]])
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    archive = next(cache(batch, 1).glob("*.zip"))
    # Read/write sharing, but no delete sharing: a real Windows scanner pattern.
    handle = kernel.CreateFileW(str(archive), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    released = threading.Event()
    def release():
        time.sleep(.3)
        kernel.CloseHandle(handle)
        released.set()
    worker = threading.Thread(target=release)
    worker.start()
    try:
        result = cleanup.execute(plan, engine=fake_engine(batch))
        assert not result.failures and not result.switched
        assert not cache(batch, 1).exists()
        assert active_bank_dir(batch.install.project) == active.directory
    finally:
        worker.join(2)
        assert released.is_set()


def test_pending_engine_version_is_switched_before_cache_cleanup(batch):
    engine = fake_engine(batch)
    old_loaded = engine.kb.concepts_path.parent
    current = batch.service.build([REPOS[1]])
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[0]], protected=[old_loaded])
    assert any(target.used for target in plan.targets) and not plan.requires_switch
    actual = engine.reload_bank
    def reload():
        assert cache(batch, 0).exists()
        actual()
    engine.reload_bank = reload
    result = cleanup.execute(plan, engine=engine)
    assert result.switched and not result.failures
    assert engine.kb.concepts_path.parent == current.directory
    assert not cache(batch, 0).exists() and old_loaded.exists()


def test_confirmation_marks_usage_for_each_object_individually(batch):
    batch.service.build([REPOS[1]])
    batch.service.build([REPOS[0]])
    plan = BankCleanupService(batch.service).plan("sources", REPOS[:2])
    flags = {target.relative: target.used for target in plan.targets}
    assert flags[str(cache(batch, 0).relative_to(batch.install.data))] is True
    assert flags[str(cache(batch, 1).relative_to(batch.install.data))] is False
    assert plan.last_source


def test_used_source_rebuilds_validates_and_reloads_before_removing_files(batch):
    old = batch.service.build(REPOS)
    cleanup = BankCleanupService(batch.service)
    engine = fake_engine(batch)
    plan = cleanup.plan("sources", [REPOS[1]])
    assert plan.requires_switch and not plan.last_source
    assert dict(plan.remaining) == {REPOS[0]: "a" * 40, REPOS[2]: "c" * 40}
    actual = engine.reload_bank
    def reload():
        assert old.directory.exists() and cache(batch, 1).exists()
        actual()
        assert len(engine.kb.all()) == 2
    engine.reload_bank = reload
    result = cleanup.execute(plan, engine=engine)
    assert result.switched and not result.empty and not result.failures
    assert not old.directory.exists() and not cache(batch, 1).exists()
    assert len(engine.kb.all()) == 2
    provenance = json.loads((active_bank_dir(batch.install.project) / "sources.json").read_text(encoding="utf-8"))
    assert {source["repository"] for source in provenance["sources"]} == {REPOS[0], REPOS[2]}
    assert batch.install.learning.read_bytes() == batch.original[batch.install.learning]


def test_last_source_switches_explicit_empty_and_preserves_learning(batch):
    from bagu_trainer.desktop_monitor import DesktopMonitor
    engine = fake_engine(batch)
    old = active_bank_dir(batch.install.project)
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    assert plan.last_source
    result = cleanup.execute(plan, engine=engine)
    assert result.empty and engine.kb.explicit_empty and engine.kb.all() == []
    assert not old.exists() and not cache(batch, 0).exists()
    assert "重新下载" in result.message
    monitor = DesktopMonitor(engine)
    assert monitor.poll()["state"] == "bank_empty"
    assert monitor.list_pending() == [] and monitor.take_quiz() is None
    assert monitor.enter_quiz("existing") is None and monitor.resume_quiz() is None
    assert batch.install.learning.read_bytes() == batch.original[batch.install.learning]


@pytest.mark.parametrize("failure", ["build", "reload"])
def test_failed_rebuild_or_engine_switch_never_cleans_original_files(batch, monkeypatch, failure):
    old = batch.service.build(REPOS)
    cleanup, engine = BankCleanupService(batch.service), fake_engine(batch)
    plan = cleanup.plan("sources", [REPOS[1]])
    original = snapshot(batch)
    if failure == "build":
        from bagu_trainer.bank_build import profiles
        monkeypatch.setattr(profiles, "build_profiles", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("合成构建失败")))
    else:
        actual = engine.reload_bank
        def reload():
            if active_bank_dir(batch.install.project) != old.directory:
                raise OSError("合成引擎重载失败")
            actual()
        engine.reload_bank = reload
    with pytest.raises(Exception, match="失败"):
        cleanup.execute(plan, engine=engine)
    assert active_bank_dir(batch.install.project) == old.directory
    assert {p: p.read_bytes() for p in original} == original
    assert len(engine.kb.all()) == 3


def test_shared_hardlinked_cache_stays_valid_and_release_estimate_excludes_shared_blob(batch):
    batch.service.build([REPOS[1]])
    batch.service.build([REPOS[0]])
    selected = next(cache(batch, 1).glob("*.zip"))
    shared = cache(batch, 0) / "shared-copy.zip"
    os.link(selected, shared)
    original = shared.read_bytes()
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[1]])
    assert plan.estimated_bytes == (cache(batch, 1) / "complete.json").stat().st_size
    cleanup.execute(plan, engine=fake_engine(batch))
    assert shared.read_bytes() == original
    assert cache(batch, 0).exists()


def test_permission_failure_reports_partial_actual_result_without_affecting_current_bank(batch, monkeypatch):
    batch.service.build([REPOS[1]])
    batch.service.build([REPOS[0]])
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[1]])
    old = active_bank_dir(batch.install.project)
    import bagu_trainer.bank_cleanup as module
    actual = module.os.replace
    def denied(source, destination):
        if Path(source) == cache(batch, 1):
            raise PermissionError("合成权限错误")
        return actual(source, destination)
    monkeypatch.setattr(module.os, "replace", denied)
    result = cleanup.execute(plan, engine=fake_engine(batch))
    assert result.failures and "权限" in result.message and not result.removed
    assert cache(batch, 1).exists() and active_bank_dir(batch.install.project) == old


def test_history_cleanup_is_independent_and_excludes_current_and_loaded_engine(batch):
    loaded = fake_engine(batch)
    protected = loaded.kb.concepts_path.parent
    historic = batch.service.build([REPOS[1]])
    active = batch.service.build([REPOS[2]])
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("history", protected=[protected])
    assert [target.relative for target in plan.targets] == [str(historic.directory.relative_to(batch.install.data))]
    result = cleanup.execute(plan, engine=loaded)
    assert not result.failures and not historic.directory.exists()
    assert protected.exists() and active.directory.exists()
    assert batch.install.learning.read_bytes() == batch.original[batch.install.learning]


def test_lock_stale_confirmation_and_external_paths_are_rejected(batch):
    cleanup = BankCleanupService(batch.service)
    plan = cleanup.plan("sources", [REPOS[0]])
    with _build_lock(batch.install.data / ".bank-builds"):
        with pytest.raises(Exception, match="正在运行"):
            cleanup.execute(plan)
    fake = replace(plan, targets=(CleanupTarget("../project-source", "outside", False, (0, 0)),))
    with pytest.raises(ValueError, match="变化"):
        cleanup.execute(fake)
    for relative in ("..", "../code", str(batch.install.project), "banks/../../code", "trainer.db", "config.json"):
        with pytest.raises(ValueError):
            cleanup._target(relative)
    batch.service.build([REPOS[1]])
    with pytest.raises(ValueError, match="变化"):
        cleanup.execute(plan)
    assert batch.install.learning.exists()


def test_history_cleanup_refuses_database_files_and_linked_directories(batch, tmp_path):
    old = active_bank_dir(batch.install.project)
    batch.service.build([REPOS[1]])
    (old / "learning.sqlite").write_bytes(b"isolated learning snapshot")
    cleanup = BankCleanupService(batch.service)
    with pytest.raises(ValueError, match="学习数据库"):
        cleanup.plan("history")
    assert (old / "learning.sqlite").read_bytes() == b"isolated learning snapshot"
    (old / "learning.sqlite").unlink()
    outside = tmp_path / "external-project"
    outside.mkdir()
    (outside / "code.py").write_text("keep", encoding="utf-8")
    link = old / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # File/path boundary tests above still run without symlink privilege.
    with pytest.raises(ValueError, match="链接"):
        cleanup.plan("history")
    assert (outside / "code.py").read_text() == "keep"


def test_configured_learning_database_with_custom_extension_is_kept(batch):
    old = active_bank_dir(batch.install.project)
    batch.service.build([REPOS[1]])
    database = old / "answers.data"
    database.write_bytes(b"isolated learning database")
    batch.install.settings.write_text(json.dumps({"db_path": str(database)}), encoding="utf-8")
    with pytest.raises(ValueError, match="学习数据库"):
        BankCleanupService(batch.service).plan("history")
    assert database.read_bytes() == b"isolated learning database"


def test_real_matcher_and_engine_accept_explicit_empty_bank(batch, monkeypatch):
    from bagu_trainer import engine
    monkeypatch.setattr(engine, "kb", engine.kb)
    monkeypatch.setattr(engine, "matcher", engine.matcher)
    engine.reload_bank()
    cleanup = BankCleanupService(batch.service)
    cleanup.execute(cleanup.plan("sources", [REPOS[0]]), engine=engine)
    assert engine.kb.explicit_empty and engine.kb.all() == []
    assert engine.matcher.rank("数据库索引") == []
    assert not engine.repractice_attempt(1)["ok"]


from release_tests.test_answer_history import history, desktop, dispose, pump, seed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk cleanup confirmation")
def test_main_window_confirmation_cancel_last_source_and_history_preservation(history, batch, monkeypatch):
    engine, store, _ = history
    monkeypatch.setattr(engine, "matcher", engine.matcher)
    engine.reload_bank()
    attempt = seed(store)
    original = list(store._conn.iterdump())
    app, _, errors = desktop(history)
    app.monitor.poll = lambda: {"state": "bank_empty" if getattr(engine.kb, "explicit_empty", False) else "watching", "projects": 1}
    app._start_worker()
    try:
        app._navigate_page("bank")
        panel = app._page_frame("bank")
        panel.source_list.selection_set(REPOS[0])
        app.root.update()
        old = active_bank_dir(batch.install.project)
        panel.delete_button.invoke()
        pump(app, lambda: hasattr(panel, "confirmation"))
        text = panel.confirm_text.get("1.0", "end-1c")
        assert REPOS[0] in text and "当前使用：是" in text and "预计释放" in text
        assert "最后一个可用来源" in text and "重新下载" in text
        panel.confirm_cancel.invoke()
        assert active_bank_dir(batch.install.project) == old and cache(batch, 0).exists()
        panel.delete_button.invoke()
        pump(app, lambda: getattr(panel, "confirmation", None) is not None and panel.confirmation.winfo_exists())
        panel.confirm_delete.invoke()
        assert app.tasks.bank.cleaning and not app._can_submit_quiz("any")
        pump(app, lambda: not panel.deleting, timeout=8)
        assert engine.kb.explicit_empty and "无题库" in app.feedback.get()
        assert app.resume_button.instate(["disabled"])
        assert app.practice.current is None and not old.exists()
        assert list(store._conn.iterdump()) == original
        assert engine.get_answer_detail(attempt)["snapshot"]
        assert not errors
    finally:
        dispose(app)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk history cleanup")
def test_history_cleanup_entry_has_its_own_confirmation_and_keeps_current(batch):
    from bagu_trainer.bank_panel import BankPanel
    from bagu_trainer.task_controller import TaskController
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.ui_theme import apply_theme
    from release_tests.test_bank_build import wait_for_ui
    old = batch.service.build([REPOS[1]])
    current = batch.service.build([REPOS[0]])
    root = create_root()
    panel = BankPanel(root, apply_theme(root), tasks=TaskController(), service=batch.service)
    panel.pack(fill="both", expand=True)
    ui = SimpleNamespace(root=root)
    try:
        root.update()
        assert not panel.source_list.selection()
        panel.history_button.invoke()
        wait_for_ui(ui, lambda: hasattr(panel, "confirmation"))
        text = panel.confirm_text.get("1.0", "end-1c")
        assert "独立清理历史版本" in text and "当前使用：否" in text
        assert old.generation in text and current.generation not in text
        panel.confirm_delete.invoke()
        wait_for_ui(ui, lambda: not panel.deleting)
        assert not old.directory.exists() and current.directory.exists()
        assert cache(batch, 0).exists()
        assert batch.install.learning.read_bytes() == batch.original[batch.install.learning]
    finally:
        root.destroy()
