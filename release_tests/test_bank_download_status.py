"""Download labels use isolated generations and synthetic in-memory HTTP only."""
import io
import json
import shutil
import threading
import time
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bagu_trainer.infrastructure.bank_build.control import BuildProgress
from bagu_trainer.infrastructure.bank_build.control import SourceStatus
from bagu_trainer.infrastructure.bank_build.service import BankBuildService
from bagu_trainer.infrastructure.bank_build.service import load_sources
from bagu_trainer.infrastructure.bank_build.source_snapshot import download_label
from bagu_trainer.infrastructure.bank_inventory import BankInventory
from release_tests.test_bank_build import installed, archive_bytes, MARKDOWN, REPOSITORY, COMMIT, wait_for_ui


@pytest.fixture
def offline(installed):
    state = SimpleNamespace(calls=[], failures=set(), gates={}, entered={})

    class Response(io.BytesIO):
        def __init__(self, content):
            super().__init__(content)
            self.headers = {"Content-Length": str(len(content))}

    def opener(request, timeout):
        repo = "/".join(request.full_url.split("/")[4:6])
        state.calls.append(repo)
        state.entered.setdefault(repo, threading.Event()).set()
        if repo in state.gates:
            assert state.gates[repo].wait(8), "test did not release simulated network"
        if repo in state.failures:
            raise urllib.error.HTTPError(request.full_url, 404, "fixture missing", {}, None)
        return Response(archive_bytes([("snapshot/README.md", MARKDOWN), ("snapshot/notes.txt", "notes")]))

    state.service = BankBuildService(installed.project, opener=opener)
    yield state
    for gate in state.gates.values():
        gate.set()


def inventory(installed):
    return BankInventory(installed.project).scan(load_sources(installed.manifest))


def remove_cache(installed):
    cache = installed.data / "source-cache"
    if cache.exists():
        shutil.rmtree(cache)


def test_saved_versions_exact_identity_and_file_integrity(installed, offline):
    assert not inventory(installed).sources[REPOSITORY].downloaded
    built = offline.service.build([REPOSITORY])
    remove_cache(installed)
    # A non-active saved generation is still downloaded, even with a broken pointer.
    (installed.data / "active-bank.json").write_text("broken", encoding="utf-8")
    assert inventory(installed).sources[REPOSITORY].downloaded
    sources = load_sources(installed.manifest)
    other = {REPOSITORY: replace(sources[REPOSITORY], commit="b" * 40)}
    assert not BankInventory(installed.project).scan(other).sources[REPOSITORY].downloaded
    upper = {REPOSITORY.upper(): replace(sources[REPOSITORY], repository=REPOSITORY.upper())}
    assert BankInventory(installed.project).scan(upper).sources[REPOSITORY.upper()].downloaded
    scanner = BankInventory(installed.project)
    assert scanner.scan(sources).sources[REPOSITORY].downloaded
    target = built.directory / "repos" / "JavaGuide" / "notes.txt"
    target.write_bytes(b"xxxxx")  # Same length corruption invalidates memoized evidence.
    assert not scanner.scan(sources).sources[REPOSITORY].downloaded
    target.unlink()
    assert not inventory(installed).sources[REPOSITORY].downloaded


def test_directories_metadata_and_staging_are_not_completion(installed, offline):
    built = offline.service.build([REPOSITORY])
    remove_cache(installed)
    metadata = built.directory / "sources.json"
    original = metadata.read_bytes()
    staging = installed.data / ".bank-builds" / ("f" * 32)
    shutil.copytree(built.directory, staging)
    shutil.rmtree(built.directory / "repos")
    assert not inventory(installed).sources[REPOSITORY].downloaded
    shutil.copytree(staging / "repos", built.directory / "repos")
    metadata.write_text("{}", encoding="utf-8")
    assert not inventory(installed).sources[REPOSITORY].downloaded
    metadata.write_bytes(original)
    data = json.loads(original)
    data["sources"][0].pop("source_files")
    metadata.write_text(json.dumps(data), encoding="utf-8")
    assert not inventory(installed).sources[REPOSITORY].downloaded
    metadata.write_bytes(original)
    assert inventory(installed).sources[REPOSITORY].downloaded


def test_structured_progress_is_backwards_compatible_and_cache_is_not_downloading(installed, offline):
    assert BuildProgress("download", "old callback payload").repository == ""
    events = []
    offline.service.build([REPOSITORY], progress=events.append)
    for event in events:
        assert (event.repository, event.commit) == ((event.source.repository, event.source.commit) if event.source else ("", ""))
    assert any(e.stage == "download" and e.repository == REPOSITORY and e.commit == COMMIT for e in events)
    events.clear()
    offline.service.build([REPOSITORY], progress=events.append)
    assert not any(e.stage == "download" for e in events)
    source = load_sources(installed.manifest)[REPOSITORY]
    for state in ("waiting", "failed", "cancelled"):
        assert download_label(source, False, SourceStatus(REPOSITORY, COMMIT, state, "download"), True) == "未下载"
    assert download_label(source, True, SourceStatus(REPOSITORY, "b" * 40, "downloading", "download"), True) == "已下载"
    assert download_label(source, False, SourceStatus(REPOSITORY, COMMIT, "downloading", "cache"), True) == "未下载"
    assert download_label(source, False, SourceStatus(REPOSITORY, COMMIT, "downloading", "download"), False) == "未下载"


@pytest.fixture(params=["setup", "panel"])
def ui(request, installed, offline, monkeypatch):
    from bagu_trainer.desktop.bank_panel import BankPanel
    from bagu_trainer.desktop.bank_setup import BankSetupWindow
    from bagu_trainer.desktop.task_controller import TaskController
    from bagu_trainer.desktop.tk_runtime import create_root
    from bagu_trainer.desktop.ui_theme import apply_theme
    repos = [REPOSITORY, "fixture/second", "fixture/third"]
    manifest = json.loads(installed.manifest.read_text(encoding="utf-8"))
    manifest["sources"] += [{"repository": repo, "directory": f"Repo{i}", "audited_commit": c * 40}
                             for i, (repo, c) in enumerate(zip(repos[1:], "bc"), 1)]
    installed.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    root = create_root()
    root.geometry("1000x800")
    theme = apply_theme(root)
    tasks = TaskController()
    errors, scan_threads, render_threads = [], [], []
    root.report_callback_exception = lambda *args: errors.append(args)
    main = threading.get_ident()
    actual_scan = BankInventory.scan

    def scan(*args, **kwargs):
        scan_threads.append(threading.get_ident())
        return actual_scan(*args, **kwargs)
    monkeypatch.setattr(BankInventory, "scan", scan)
    state = SimpleNamespace(root=root, repos=repos, kind=request.param, tasks=tasks, errors=errors)

    def create():
        if state.kind == "setup":
            view = BankSetupWindow(root, service=offline.service, embedded=True)
            view.root.pack(fill="both", expand=True)
        else:
            view = BankPanel(root, theme, tasks=tasks, service=offline.service)
            view.pack(fill="both", expand=True)
        # Instrument the actual Tk mutations, so off-thread UI writes fail.
        for method in ("set", "item"):
            actual = getattr(view.source_list, method)
            def checked(*args, _actual=actual, **kwargs):
                render_threads.append(threading.get_ident())
                assert threading.get_ident() == main
                return _actual(*args, **kwargs)
            monkeypatch.setattr(view.source_list, method, checked)
        state.view = view
        return view

    state.create = create
    state.labels = lambda: [state.view.source_list.set(repo, "download") for repo in repos]
    state.busy = lambda: state.view.busy if state.kind == "setup" else tasks.bank.busy
    state.scan_idle = lambda: not (state.view._scanning if state.kind == "setup" else state.view.scanning)
    state.refresh = lambda: state.view._request_scan() if state.kind == "setup" else state.view.refresh()
    state.destroy = lambda: state.view.root.destroy() if state.kind == "setup" else state.view.destroy()
    create()
    try:
        wait_for_ui(state, lambda: state.view.inventory is not None and state.scan_idle())
        yield state
        assert not errors
        assert scan_threads and all(t != main for t in scan_threads)
        assert render_threads and all(t == main for t in render_threads)
    finally:
        for gate in offline.gates.values():
            gate.set()
        if state.busy():
            state.view._cancel()
            wait_for_ui(state, lambda: not state.busy())
        root.destroy()


def test_ui_sequential_download_failure_cancel_navigation_and_recreate(ui, installed, offline):
    first, second, third = ui.repos
    assert ui.labels() == ["未下载"] * 3
    assert ui.view.source_list.heading("download", "text") == "下载状态"
    assert ui.view.source_list.heading("license", "text") == "观察到的声明"
    learning = installed.learning.read_bytes()
    offline.service.build([first])
    pointer = (installed.data / "active-bank.json").read_bytes()
    ui.refresh()
    wait_for_ui(ui, lambda: ui.labels() == ["已下载", "未下载", "未下载"] and ui.scan_idle())
    offline.gates[second] = threading.Event()
    offline.failures.add(second)
    ui.view.source_list.selection_set(ui.repos)
    ui.view._start()
    wait_for_ui(ui, lambda: ui.labels()[1] == "正在下载")
    assert ui.labels() == ["已下载", "正在下载", "未下载"]
    assert ui.view.source_list.selection() == tuple(ui.repos)
    # Ordinary navigation keeps the application task alive, then remaps the page.
    target = ui.view.root if ui.kind == "setup" else ui.view
    target.pack_forget()
    ui.root.update()
    assert ui.busy()
    target.pack(fill="both", expand=True)
    if ui.kind == "panel":
        # Recreating a management page also preserves the application-owned task.
        ui.destroy()
        ui.create()
        wait_for_ui(ui, lambda: ui.labels()[1] == "正在下载")
        assert ui.busy() and ui.labels() == ["已下载", "正在下载", "未下载"]
        ui.view.source_list.selection_set(ui.repos)
    offline.gates[third] = threading.Event()
    offline.gates[second].set()
    wait_for_ui(ui, lambda: ui.labels()[2] == "正在下载")
    assert ui.labels() == ["已下载", "未下载", "正在下载"]
    offline.gates[third].set()
    wait_for_ui(ui, lambda: not ui.busy() and ui.scan_idle() and ui.labels() == ["已下载", "未下载", "已下载"])
    hint = ui.view.status.get() if ui.kind == "setup" else ui.view.operation_hint.get()
    assert "404" in hint
    assert (installed.data / "active-bank.json").read_bytes() == pointer
    assert installed.learning.read_bytes() == learning
    # A second attempt is cancelled before the missing second source can publish.
    offline.failures.clear()
    offline.gates[second] = threading.Event()
    ui.view._start()
    wait_for_ui(ui, lambda: ui.labels()[1] == "正在下载")
    ui.view._cancel()
    offline.gates[second].set()
    wait_for_ui(ui, lambda: not ui.busy() and ui.scan_idle())
    assert ui.labels() == ["已下载", "未下载", "已下载"]
    assert (installed.data / "active-bank.json").read_bytes() == pointer
    ui.view._start()
    wait_for_ui(ui, lambda: not ui.busy() and ui.scan_idle() and ui.labels() == ["已下载"] * 3)
    assert installed.learning.read_bytes() == learning
    ui.destroy()
    ui.create()
    wait_for_ui(ui, lambda: ui.view.inventory is not None and ui.scan_idle())
    assert ui.labels() == ["已下载"] * 3
    ui.view.source_list.selection_set(second)
    manifest = json.loads(installed.manifest.read_text(encoding="utf-8"))
    manifest["sources"][1]["audited_commit"] = "d" * 40
    installed.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    (ui.view.reload_sources if ui.kind == "setup" else ui.view._reload)()
    wait_for_ui(ui, lambda: ui.view.inventory is not None and ui.scan_idle())
    assert ui.labels() == ["已下载", "未下载", "已下载"]
    assert ui.view.source_list.selection() == (second,)


def test_ui_stale_scan_does_not_override_download_or_terminal_files(ui, installed, offline, monkeypatch):
    view = ui.view
    scanner = view._scanner
    actual_scan = scanner.scan
    entered, release = threading.Event(), threading.Event()

    def delayed_scan(*args, **kwargs):
        result = actual_scan(*args, **kwargs)
        entered.set()
        assert release.wait(8)
        return result
    monkeypatch.setattr(scanner, "scan", delayed_scan)
    ui.refresh()
    assert entered.wait(2)
    second = ui.repos[1]
    offline.gates[second] = threading.Event()
    view.source_list.selection_set(second)
    view._start()
    try:
        wait_for_ui(ui, lambda: ui.labels()[1] == "正在下载")
        release.set()
        wait_for_ui(ui, ui.scan_idle)
        assert ui.labels()[1] == "正在下载"
        # Complete task state alone cannot survive files being cleaned at failure.
        view._cancel()
        offline.gates[second].set()
        wait_for_ui(ui, lambda: not ui.busy() and ui.scan_idle())
        assert ui.labels() == ["未下载"] * 3
        assert not any((installed.data / ".bank-builds").glob("*/repos"))
        calls = len(offline.calls)
        scans = []
        monkeypatch.setattr(scanner, "scan", lambda *a, **k: (scans.append(True), actual_scan(*a, **k))[1])
        # The 100 ms UI queue drain does not do a disk traversal every tick.
        end = time.monotonic() + .4
        while time.monotonic() < end:
            ui.root.update()
            time.sleep(.01)
        assert not scans and len(offline.calls) == calls
    finally:
        release.set()
        offline.gates[second].set()


def test_ui_failed_build_does_not_trust_completed_task_after_files_removed(ui, installed, monkeypatch):
    from bagu_trainer.infrastructure.bank_build import importer
    scan_after_cleanup = threading.Event()
    actual_scan = ui.view._scanner.scan

    def scan(*args, **kwargs):
        # Match cleanup's existing scan coordination; Windows open ZIP handles
        # must not make the simulated removal itself fail partway through.
        assert scan_after_cleanup.wait(8)
        return actual_scan(*args, **kwargs)
    monkeypatch.setattr(ui.view._scanner, "scan", scan)

    def fail_import(*args, **kwargs):
        # Simulate final retained files after cache removal and staging cleanup.
        remove_cache(installed)
        scan_after_cleanup.set()
        raise RuntimeError("fixture import failure")
    monkeypatch.setattr(importer, "build_bank", fail_import)
    ui.view.source_list.selection_set(ui.repos)
    ui.view._start()
    try:
        wait_for_ui(ui, lambda: not ui.busy() and ui.scan_idle())
    finally:
        scan_after_cleanup.set()
    statuses = ui.view.source_statuses if ui.kind == "setup" else ui.tasks.bank.sources
    assert len(statuses) == 3 and all(s.state == "completed" for s in statuses.values())
    assert ui.labels() == ["未下载"] * 3
    hint = ui.view.status.get() if ui.kind == "setup" else ui.view.operation_hint.get()
    assert "fixture import failure" in hint
    assert not (installed.data / "active-bank.json").exists()
