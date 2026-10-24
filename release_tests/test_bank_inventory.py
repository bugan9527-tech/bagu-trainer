"""Isolated byte accounting, legacy provenance, background refresh and layout."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from bagu_trainer.bank_build.service import BankBuildService, load_sources, _build_lock
from bagu_trainer.bank_build.source_cache import cache_directory
from bagu_trainer.bank_inventory import BankInventory, delete_source_cache
from bagu_trainer.bank_storage import atomic_json
from release_tests.test_bank_build import installed, http_archive, REPOSITORY, COMMIT
from release_tests.test_bank_batch import batch, REPOS


def size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else path.stat().st_size


def scan(installed, **kwargs):
    return BankInventory(installed.project).scan(load_sources(installed.manifest), **kwargs)


def test_exact_category_sizes_and_per_source_breakdown(installed, http_archive):
    service = BankBuildService(installed.project, opener=http_archive.opener)
    first = service.build([REPOSITORY])
    second = service.build([REPOSITORY])
    current_build = "c" * 32
    live = installed.data / ".bank-builds" / current_build
    live.mkdir()
    (live / "JavaGuide.zip").write_bytes(b"downloading")
    abandoned = installed.data / ".bank-builds" / ("d" * 32)
    abandoned.mkdir()
    (abandoned / "JavaGuide.zip").write_bytes(b"partial")
    (installed.data / "write.tmp").write_bytes(b"temp")
    result = scan(installed, generation=current_build)
    assert result.totals == {
        "cache": size(installed.data / "source-cache"),
        "current": size(second.directory) + len(b"downloading"),
        "history": size(first.directory) + installed.legacy.stat().st_size,
        "temporary": size(abandoned) + size(installed.data / ".bank-builds" / "owner.lock") + 4,
    }
    row = result.sources[REPOSITORY]
    assert row.local_state == "verified" and row.used == "当前版本使用"
    assert row.remote_bytes is None
    assert row.bytes_by_category["cache"] == result.totals["cache"]
    assert row.bytes_by_category["current"] == size(second.directory / "repos" / "JavaGuide") + len(b"downloading")
    assert row.bytes_by_category["temporary"] == len(b"partial")
    assert sum(result.totals.values()) == row.local_bytes + result.unassigned
    assert not result.warnings


def test_hardlinks_count_once_but_separate_copies_count_twice(installed, http_archive):
    built = BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    archive = next((installed.data / "source-cache").glob("*/*.zip"))
    shared = built.directory / "shared.zip"
    try:
        os.link(archive, shared)
    except OSError as error:
        pytest.skip(f"Hard links unavailable: {error}")
    copied = built.directory / "separate-copy.zip"
    copied.write_bytes(archive.read_bytes())
    result = scan(installed)
    assert result.duplicate_bytes == archive.stat().st_size
    assert result.totals["current"] == size(built.directory) - archive.stat().st_size
    assert result.totals["cache"] == size(installed.data / "source-cache")


def test_missing_and_same_length_corrupt_cache_rechecked_without_writes(installed, http_archive):
    built = BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    inventory = BankInventory(installed.project)
    sources = load_sources(installed.manifest)
    original = {p: p.read_bytes() for p in installed.data.rglob("*") if p.is_file()}
    assert inventory.scan(sources).sources[REPOSITORY].local_state == "verified"
    assert {p: p.read_bytes() for p in original} == original
    archive = next((installed.data / "source-cache").glob("*/*.zip"))
    content = archive.read_bytes()
    archive.write_bytes(bytes([content[0] ^ 255]) + content[1:])
    assert inventory.scan(sources).sources[REPOSITORY].local_state == "invalid"
    archive.unlink()
    result = inventory.scan(sources)
    assert result.sources[REPOSITORY].local_state == "invalid"
    assert result.sources[REPOSITORY].used == "当前版本使用"
    assert result.totals["current"] == size(built.directory)
    assert http_archive.count == 1


def test_legacy_uuid_snapshot_is_counted_without_trusting_old_download_digest(installed):
    directory = installed.data / "banks" / ("e" * 32)
    repo = directory / "repos" / "JavaGuide"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("旧版本来源正文", encoding="utf-8")
    (directory / "questions.json").write_text("[]", encoding="utf-8")
    atomic_json(directory / "sources.json", {"sources": [{"repository": REPOSITORY, "commit": COMMIT,
                                                         "archive_sha256": "f" * 64}]})
    atomic_json(installed.data / "active-bank.json", {"generation": directory.name})
    result = scan(installed)
    row = result.sources[REPOSITORY]
    assert row.local_state == "legacy" and row.used == "当前版本使用"
    assert row.local_bytes == size(repo)
    assert result.totals["current"] == size(directory)
    (directory / "sources.json").unlink()
    result = scan(installed)
    assert "未知" in result.sources[REPOSITORY].used
    assert result.sources[REPOSITORY].local_state == "legacy"


def test_old_commit_cache_occupancy_is_attributed_but_not_reused_as_selected_version(installed, http_archive):
    BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY], versions={REPOSITORY: "b" * 40})
    result = scan(installed)
    row = result.sources[REPOSITORY]
    assert row.local_state == "legacy" and row.used == "当前使用其他 commit"
    assert row.bytes_by_category["cache"] == result.totals["cache"] > 0
    assert row.remote_bytes is None


def test_unknown_provenance_and_bad_pointer_do_not_claim_unused_or_reusable(installed):
    result = scan(installed)
    assert "未知" in result.sources[REPOSITORY].used
    directory = cache_directory(installed.data / "source-cache", load_sources(installed.manifest)[REPOSITORY])
    directory.mkdir(parents=True)
    (directory / "part.zip").write_bytes(b"partial")
    (installed.data / "active-bank.json").write_text("broken", encoding="utf-8")
    result = scan(installed)
    assert result.sources[REPOSITORY].local_state == "incomplete"
    assert result.warnings
    assert "未知" in result.sources[REPOSITORY].used


def test_explicit_cache_delete_preserves_active_bank_and_learning_and_honors_lock(installed, http_archive):
    result = BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    sources = list(load_sources(installed.manifest).values())
    original = {p: p.read_bytes() for p in result.directory.rglob("*") if p.is_file()}
    original[installed.learning] = installed.learning.read_bytes()
    original[installed.data / "active-bank.json"] = (installed.data / "active-bank.json").read_bytes()
    with _build_lock(installed.data / ".bank-builds"):
        with pytest.raises(Exception, match="正在运行"):
            delete_source_cache(installed.project, sources)
    with pytest.raises(Exception, match="安全切换"):
        delete_source_cache(installed.project, sources)
    assert {p: p.read_bytes() for p in original} == original
    assert scan(installed).sources[REPOSITORY].local_state == "verified"


def test_incomplete_scan_reports_missing_files_instead_of_silent_zero(installed, monkeypatch):
    directory = installed.data / "banks" / ("f" * 32)
    directory.mkdir(parents=True)
    path = directory / "file"
    path.write_bytes(b"missing soon")
    actual = Path.lstat
    def removed(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError("removed during scan")
        return actual(self, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", removed)
    assert scan(installed).warnings


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk inventory")
@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_panel_background_refresh_delete_and_narrow_dpi_layout(installed, http_archive, monkeypatch, scale):
    from bagu_trainer.store import Store
    installed.learning.unlink()
    Store(installed.learning).close()
    (installed.data / "sources.json").write_text(json.dumps({"sources": [{
        "repository": REPOSITORY, "commit": COMMIT, "directory": "JavaGuide"}]}))
    from bagu_trainer.bank_panel import BankPanel
    from bagu_trainer.task_controller import TaskController
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.ui_theme import apply_theme
    from release_tests.test_bank_build import wait_for_ui
    from release_tests.test_ui_theme import assert_inside
    service = BankBuildService(installed.project, opener=http_archive.opener)
    service.build([REPOSITORY])
    main_thread = threading.get_ident()
    actual_scan = BankInventory.scan
    entered, release = threading.Event(), threading.Event()
    threads = []
    def slow_scan(self, *args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return actual_scan(self, *args, **kwargs)
    monkeypatch.setattr(BankInventory, "scan", slow_scan)
    root = create_root()
    root.tk.call("tk", "scaling", scale * 96 / 72)
    theme = apply_theme(root)
    root.maxsize(theme.px(1200), theme.px(1000))
    root.geometry(f"{theme.px(560)}x{theme.px(700)}+0+0")
    tasks = TaskController()
    panel = BankPanel(root, theme, tasks=tasks, service=service)
    panel.pack(fill="both", expand=True)
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    try:
        assert entered.wait(2)
        ticks = []
        root.after(1, lambda: ticks.append(True))
        root.update()
        assert ticks and not panel.inventory
        release.set()
        wait_for_ui(SimpleWindow(root), lambda: panel.inventory is not None)
        assert threads and all(ident != main_thread for ident in threads)
        panel.source_list.selection_set(REPOSITORY)
        root.update()
        assert "可复用" in panel.local_detail.get()
        assert "未参与" in panel.task_detail.get()
        assert "当前版本使用" in panel.used_detail.get()
        assert "未知" in panel.local_detail.get()
        for widget in (panel.start_button, panel.delete_button, panel.cancel_button, panel.local_label, panel.task_label, panel.used_label):
            assert_inside(widget, root)
        panel.delete_button.invoke()
        wait_for_ui(SimpleWindow(root), lambda: hasattr(panel, "confirmation"))
        assert "最后一个可用来源" in panel.confirm_text.get("1.0", "end")
        panel.confirm_cancel.invoke()
        wait_for_ui(SimpleWindow(root), lambda: not panel.deleting)
        assert panel.inventory.sources[REPOSITORY].local_state == "verified"
        assert "当前版本使用" in panel.used_detail.get()
        panel.pack_forget()
        root.update()
        service.build([REPOSITORY])
        panel.pack(fill="both", expand=True)
        root.update()
        wait_for_ui(SimpleWindow(root), lambda: panel.inventory.sources[REPOSITORY].local_state == "verified")
        assert not errors
    finally:
        release.set()
        root.destroy()


class SimpleWindow:
    def __init__(self, root):
        self.root = root


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk state refresh")
def test_panel_download_failure_retry_and_external_deletion_refresh(batch):
    from bagu_trainer.bank_panel import BankPanel
    from bagu_trainer.task_controller import TaskController
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.ui_theme import apply_theme
    from release_tests.test_bank_build import wait_for_ui
    from release_tests.test_bank_batch import assert_previous_usable
    root = create_root()
    root.geometry("800x750")
    tasks = TaskController()
    panel = BankPanel(root, apply_theme(root), tasks=tasks, service=batch.service)
    panel.pack(fill="both", expand=True)
    ui = SimpleWindow(root)
    try:
        wait_for_ui(ui, lambda: panel.inventory is not None)
        batch.failures[REPOS[1]] = [404]
        panel.source_list.selection_set(REPOS)
        panel._start()
        wait_for_ui(ui, lambda: tasks.bank.state == "failed")
        wait_for_ui(ui, lambda: panel.inventory.sources[REPOS[2]].local_state == "verified")
        assert [s.state for s in tasks.bank.sources.values()] == ["completed", "failed", "completed"]
        assert panel.source_list.set(REPOS[1], "task") == "失败"
        assert panel.source_list.set(REPOS[1], "local") == "未下载"
        assert panel.source_list.set(REPOS[0], "used") == "当前版本使用"
        assert panel.source_list.set(REPOS[0], "remote") == "未知"  # Cache hit; no remote request.
        assert panel.source_list.set(REPOS[2], "remote") != "未知"  # Observed Content-Length.
        assert_previous_usable(batch)
        count = len(batch.calls)
        panel.retry_button.invoke()
        wait_for_ui(ui, lambda: tasks.bank.state == "built")
        wait_for_ui(ui, lambda: panel.inventory.sources[REPOS[1]].local_state == "verified")
        assert [repo for repo, _ in batch.calls[count:]] == [REPOS[1]]
        assert all(row.used == "当前版本使用" for row in panel.inventory.sources.values())
        # File changes outside the page also invalidate the memo and refresh.
        source = load_sources(batch.install.manifest)[REPOS[1]]
        entry = cache_directory(batch.install.data / "source-cache", source)
        next(entry.glob("*.zip")).unlink()
        wait_for_ui(ui, lambda: panel.inventory.sources[REPOS[1]].local_state == "invalid")
        assert panel.source_list.set(REPOS[1], "task") == "完成"
        assert panel.source_list.set(REPOS[1], "used") == "当前版本使用"
        assert "bad" in panel.source_list.item(REPOS[1], "tags")
    finally:
        root.destroy()
