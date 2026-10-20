"""Offline HTTP/ZIP integration tests; never download third-party content."""
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from bagu_trainer import config
from bagu_trainer.bank_build.control import BuildCancelled, BuildControl, BuildError, BuildTimeout
from bagu_trainer.bank_build.downloads import ArchiveLimits, GithubRedirects, safe_extract
from bagu_trainer.bank_build.service import BankBuildService, _build_lock, load_sources
from bagu_trainer.bank_storage import active_bank_dir
from bagu_trainer.concepts import load_concept_profiles
from bagu_trainer.knowledge_base import KnowledgeBase


COMMIT = "a" * 40
REPOSITORY = "Snailclimb/JavaGuide"
MARKDOWN = ("# 什么是数据库索引？\n\n"
            "数据库索引用额外的数据结构记录键值与数据位置的对应关系。"
            "查询可以利用索引缩小扫描范围。维护索引需要存储空间，"
            "插入和更新数据也可能增加索引维护成本，因此应结合查询条件选择索引。\n")


def archive_bytes(entries=None, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as bundle:
        for name, content in entries or [("snapshot/README.md", MARKDOWN)]:
            bundle.writestr(name, content)
    result = stream.getvalue()
    for name, _ in entries or []:
        if isinstance(name, str) and "\\" in name:
            # ZipInfo normalizes OS separators when writing on Windows. Patch
            # both ZIP filename fields to exercise a hostile archive verbatim.
            result = result.replace(name.replace("\\", "/").encode(), name.encode())
    return result


@pytest.fixture
def installed(tmp_path, monkeypatch):
    project = tmp_path / "本机 安装"
    data = project / "data"
    data.mkdir(parents=True)
    manifest = project / "interview-banks" / "sources.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"sources": [{"repository": REPOSITORY,
        "directory": "JavaGuide", "audited_commit": COMMIT, "observed_license": "Apache-2.0",
        "evidence_url": f"https://github.com/{REPOSITORY}/blob/{COMMIT}/LICENSE"}]}), encoding="utf-8")
    legacy = data / "questions.json"
    legacy.write_text('[{"id":"bank-0001","question":"original"}]', encoding="utf-8")
    learning = data / "trainer.db"
    learning.write_bytes(b"old learning records")
    settings = project / "config.json"
    settings.write_text('{"db_path":"data/trainer.db","project_roots":[]}', encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    monkeypatch.setattr(config, "USER_DATA_ROOT", project)
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(project))
    monkeypatch.setattr(config, "CONFIG_PATH", settings)
    return SimpleNamespace(project=project, data=data, manifest=manifest, legacy=legacy,
                           learning=learning, settings=settings)


@pytest.fixture
def http_archive():
    state = SimpleNamespace(body=archive_bytes(), count=0, failures=0, status=503,
                            delay=0, extra_length=0, urls=[])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.count += 1
            if state.count <= state.failures:
                self.send_response(state.status)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(state.body) + state.extra_length))
            self.end_headers()
            if state.delay:
                time.sleep(state.delay)
            try:
                self.wfile.write(state.body)
            except (BrokenPipeError, ConnectionError):
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def opener(request, timeout):
        state.urls.append(request.full_url)
        return urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/archive.zip", timeout=timeout)
    state.opener = opener
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def snapshot(install):
    return {p: p.read_bytes() for p in (install.legacy, install.learning, install.settings)}


def assert_unchanged(install, original, pointer=None):
    assert {p: p.read_bytes() for p in original} == original
    active = install.data / "active-bank.json"
    if pointer is None:
        assert not active.exists()
    else:
        assert active.read_bytes() == pointer
    temporary = install.data / ".bank-builds"
    assert not temporary.exists() or not any(p.is_dir() for p in temporary.iterdir())


def test_full_http_build_is_in_process_and_switches_complete_generation(installed, http_archive, monkeypatch):
    original = snapshot(installed)
    engine_loaded = "bagu_trainer.engine" in sys.modules
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kw: pytest.fail("external process was started"))
    events = []
    service = BankBuildService(installed.project, opener=http_archive.opener)
    result = service.build([REPOSITORY], progress=events.append)
    assert result.questions == 1
    assert active_bank_dir() == result.directory
    assert {p: p.read_bytes() for p in original} == original
    assert result.previous_directory == installed.data
    assert (result.directory / "repos" / "JavaGuide" / "README.md").exists()
    assert not list(result.directory.rglob(".git"))
    assert not list(result.directory.glob("*.zip"))
    assert http_archive.urls == [f"https://api.github.com/repos/{REPOSITORY}/zipball/{COMMIT}"]
    questions = KnowledgeBase().all()
    assert questions[0]["source_status"] == "ok" and questions[0]["usable_excerpt"]
    assert questions[0]["knowledge_tags"]
    assert questions[0]["id"].startswith("source-")
    assert set(load_concept_profiles()) == {questions[0]["id"]}
    assert {event.stage for event in events} >= {"prepare", "download", "unpack", "extract", "import",
                                                "tag", "concepts", "validate", "activate", "done"}
    metadata = json.loads((result.directory / "sources.json").read_text(encoding="utf-8"))["sources"][0]
    assert metadata["archive_sha256"] == hashlib.sha256(http_archive.body).hexdigest()
    assert metadata["commit"] == COMMIT
    assert ("bagu_trainer.engine" in sys.modules) == engine_loaded


def test_successful_rebuild_retains_previous_generation_and_stable_ids(installed, http_archive):
    service = BankBuildService(installed.project, opener=http_archive.opener)
    first = service.build([REPOSITORY])
    old_files = {p.relative_to(first.directory): p.read_bytes() for p in first.directory.rglob("*") if p.is_file()}
    first_bank = KnowledgeBase()
    second = service.build([REPOSITORY])
    assert second.generation != first.generation
    assert second.previous_directory == first.directory
    assert first_bank.all() == KnowledgeBase().all()
    assert {p.relative_to(first.directory): p.read_bytes() for p in first.directory.rglob("*") if p.is_file()} == old_files
    # A bank instance's concepts remain bound to the same generation.
    assert first_bank.concepts_path.parent == first.directory
    assert load_concept_profiles(first_bank.concepts_path)


def test_download_failure_leaves_old_data_and_manual_retry_succeeds(installed, http_archive):
    original = snapshot(installed)
    http_archive.failures, http_archive.status = 1, 404
    service = BankBuildService(installed.project, opener=http_archive.opener)
    with pytest.raises(BuildError, match="HTTP 404") as failure:
        service.build([REPOSITORY])
    assert failure.value.stage == "download" and http_archive.count == 1
    assert_unchanged(installed, original)
    assert service.build([REPOSITORY]).questions == 1


def test_transient_download_failure_is_retried(installed, http_archive):
    http_archive.failures = 1
    events = []
    result = BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY], progress=events.append)
    assert result.questions == 1 and http_archive.count == 2
    assert any(e.stage == "retry" and e.attempt == 2 for e in events)


def test_partial_http_response_is_rejected(installed, http_archive):
    original = snapshot(installed)
    http_archive.extra_length = 100
    with pytest.raises(BuildError, match="IncompleteRead"):
        BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY], download_retries=0)
    assert_unchanged(installed, original)


def test_socket_timeout_and_total_deadline_preserve_old_data(installed, http_archive):
    original = snapshot(installed)
    http_archive.delay = 0.2
    service = BankBuildService(installed.project, opener=http_archive.opener)
    with pytest.raises(BuildError, match="TimeoutError"):
        service.build([REPOSITORY], request_timeout=0.03, download_retries=0)
    assert_unchanged(installed, original)
    with pytest.raises(BuildTimeout):
        service.build([REPOSITORY], timeout=0.06, request_timeout=0.02)
    assert_unchanged(installed, original)


@pytest.mark.parametrize("stage", ["download", "unpack", "extract", "import", "tag", "concepts", "validate", "activate"])
def test_cancel_at_each_stage_preserves_active_bank_and_retry_works(installed, http_archive, stage):
    service = BankBuildService(installed.project, opener=http_archive.opener)
    first = service.build([REPOSITORY])
    original, pointer = snapshot(installed), (installed.data / "active-bank.json").read_bytes()
    cancel = threading.Event()
    def progress(event):
        if event.stage == stage:
            cancel.set()
    with pytest.raises(BuildCancelled):
        service.build([REPOSITORY], cancel=cancel, progress=progress)
    assert_unchanged(installed, original, pointer)
    assert active_bank_dir() == first.directory
    assert service.build([REPOSITORY]).questions == 1


def test_cancel_during_download_chunk_preserves_old_data(installed, http_archive):
    original, cancel = snapshot(installed), threading.Event()
    def progress(event):
        if event.stage == "download" and event.completed:
            cancel.set()
    with pytest.raises(BuildCancelled):
        BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY], cancel=cancel, progress=progress)
    assert_unchanged(installed, original)


def test_build_exception_then_retry(installed, http_archive, monkeypatch):
    from bagu_trainer.bank_build import profiles
    original = snapshot(installed)
    build_profiles = profiles.build_profiles
    def failed(*args, **kwargs):
        raise RuntimeError("simulated interruption")
    monkeypatch.setattr(profiles, "build_profiles", failed)
    service = BankBuildService(installed.project, opener=http_archive.opener)
    with pytest.raises(BuildError, match="concepts"):
        service.build([REPOSITORY])
    assert_unchanged(installed, original)
    monkeypatch.setattr(profiles, "build_profiles", build_profiles)
    assert service.build([REPOSITORY]).questions == 1


def test_pointer_switch_failure_keeps_previous_bank(installed, http_archive, monkeypatch):
    service = BankBuildService(installed.project, opener=http_archive.opener)
    first = service.build([REPOSITORY])
    original, pointer = snapshot(installed), (installed.data / "active-bank.json").read_bytes()
    actual_replace = os.replace
    def fail_switch(source, destination):
        if Path(destination).name == "active-bank.json":
            raise OSError("simulated power loss before commit")
        return actual_replace(source, destination)
    monkeypatch.setattr(os, "replace", fail_switch)
    with pytest.raises(BuildError, match="activate"):
        service.build([REPOSITORY])
    assert_unchanged(installed, original, pointer)
    assert active_bank_dir() == first.directory
    monkeypatch.setattr(os, "replace", actual_replace)
    assert service.build([REPOSITORY]).questions == 1


def test_empty_source_does_not_replace_old_bank(installed, http_archive):
    original = snapshot(installed)
    http_archive.body = archive_bytes([("snapshot/README.md", "# Introduction\nNo questions.\n")])
    with pytest.raises(BuildError, match="没有可用题目"):
        BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    assert_unchanged(installed, original)


@pytest.mark.parametrize("path", ["snapshot/../../escape.txt", "/absolute.txt", "snapshot/../escape.txt",
    "snapshot/C:/escape.txt", "snapshot/a\\escape.txt", "snapshot/NUL.txt", "snapshot/CONIN$",
    "snapshot/a:stream", "snapshot/name.", "snapshot/name ", "snapshot/a?.txt"])
def test_unsafe_zip_paths_are_rejected(tmp_path, path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(archive_bytes([(path, "unsafe")]))
    with pytest.raises(BuildError, match="不安全"):
        safe_extract(archive, tmp_path / "output", BuildControl())
    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlink_and_case_collision_are_rejected(tmp_path):
    symlink = zipfile.ZipInfo("snapshot/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    for index, entries in enumerate(([(symlink, "../../outside")],
                                     [("snapshot/A.md", "a"), ("snapshot/a.md", "b")])):
        archive = tmp_path / f"bad{index}.zip"
        archive.write_bytes(archive_bytes(entries))
        with pytest.raises(BuildError):
            safe_extract(archive, tmp_path / f"out{index}", BuildControl())


@pytest.mark.parametrize("limits", [ArchiveLimits(download_bytes=10), ArchiveLimits(file_bytes=10),
    ArchiveLimits(unpack_bytes=10), ArchiveLimits(members=0), ArchiveLimits(compression_ratio=1)])
def test_download_and_unzip_limits_preserve_old_bank(installed, http_archive, limits):
    original = snapshot(installed)
    with pytest.raises(BuildError):
        BankBuildService(installed.project, opener=http_archive.opener, limits=limits).build([REPOSITORY])
    assert_unchanged(installed, original)


def test_corrupt_zip_preserves_old_bank_and_can_retry(installed, http_archive):
    original = snapshot(installed)
    http_archive.body = b"not a ZIP"
    service = BankBuildService(installed.project, opener=http_archive.opener)
    with pytest.raises(BuildError, match="ZIP"):
        service.build([REPOSITORY])
    assert_unchanged(installed, original)
    http_archive.body = archive_bytes()
    assert service.build([REPOSITORY]).questions == 1


def test_requested_version_must_be_immutable_and_known(installed, http_archive):
    service = BankBuildService(installed.project, opener=http_archive.opener)
    for repositories, versions in [([], {}), (["unknown/repo"], {}), ([REPOSITORY], {REPOSITORY: "main"})]:
        with pytest.raises(BuildError):
            service.build(repositories, versions=versions)
    assert not http_archive.urls
    service.build([REPOSITORY], versions={REPOSITORY: "b" * 40})
    assert http_archive.urls[-1].endswith("b" * 40)


def test_redirects_must_stay_on_github_https():
    handler = GithubRedirects()
    request = urllib.request.Request("https://api.github.com/test")
    for destination in ("http://codeload.github.com/test", "https://evil.example/test",
                         "https://user:secret@github.com/test"):
        with pytest.raises(BuildError):
            handler.redirect_request(request, None, 302, "Found", {}, destination)


def test_existing_build_lock_prevents_second_writer(installed, http_archive):
    with _build_lock(installed.data / ".bank-builds"):
        with pytest.raises(BuildError, match="正在运行"):
            BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    assert not http_archive.urls
    assert BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY]).questions == 1


def test_corrupt_pointer_is_preserved_and_repaired(installed, http_archive):
    pointer = installed.data / "active-bank.json"
    pointer.write_bytes(b"broken\xff")
    result = BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY])
    assert (result.directory / "previous-pointer.json").read_bytes() == b"broken\xff"
    assert active_bank_dir() == result.directory


def test_invalid_active_pointer_cannot_escape_data_directory(installed):
    (installed.data / "active-bank.json").write_text('{"generation":"../../outside"}', encoding="utf-8")
    with pytest.raises(ValueError):
        active_bank_dir()


def test_abrupt_process_exit_does_not_switch_bank_and_new_build_can_retry(installed, http_archive):
    original = snapshot(installed)
    archive = installed.project / "fixture.zip"
    archive.write_bytes(archive_bytes())
    probe = r'''
import io, os, sys
from pathlib import Path
from bagu_trainer.bank_build.service import BankBuildService
root = Path(sys.argv[1])
class Response(io.BytesIO):
    headers = {}
def opener(request, timeout):
    return Response((root / 'fixture.zip').read_bytes())
def crash(event):
    if event.stage == 'concepts':
        os._exit(73)
BankBuildService(root, opener=opener).build(['Snailclimb/JavaGuide'], progress=crash)
'''
    result = subprocess.run([sys.executable, "-B", "-c", probe, str(installed.project)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, result.stderr
    assert {p: p.read_bytes() for p in original} == original
    assert not (installed.data / "active-bank.json").exists()
    # Abandoned UUID workspaces are ignored; the crashed process's lock is released.
    assert any(p.is_dir() for p in (installed.data / ".bank-builds").iterdir())
    assert BankBuildService(installed.project, opener=http_archive.opener).build([REPOSITORY]).questions == 1


def wait_for_ui(window, predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)
    assert predicate()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_desktop_source_selection_failure_cancel_and_retry(installed, http_archive):
    from bagu_trainer.tk_runtime import prepare_tk
    prepare_tk()
    import tkinter as tk
    from bagu_trainer.bank_setup import BankSetupWindow
    parent = tk.Tk()
    parent.withdraw()
    results = []
    window = BankSetupWindow(parent, service=BankBuildService(installed.project, opener=http_archive.opener),
                             on_success=results.append)
    window.root.withdraw()
    try:
        window._start()
        assert not window.busy and not http_archive.urls
        window.source_list.selection_set(REPOSITORY)
        window.timeout.set("nan")
        window._start()
        assert not window.busy and not http_archive.urls
        window.timeout.set("10")
        http_archive.failures, http_archive.status = 1, 404
        window._start()
        wait_for_ui(window, lambda: not window.busy)
        assert "HTTP 404" in window.status.get()
        assert not window.start_button.instate(["disabled"])
        window._start()
        window._cancel()
        wait_for_ui(window, lambda: not window.busy)
        assert not (installed.data / "active-bank.json").exists()
        window._start()
        wait_for_ui(window, lambda: not window.busy)
        assert len(results) == 1 and results[0].questions == 1
        assert window.progress.cget("value") == 100
    finally:
        window._close()
        parent.destroy()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk desktop")
def test_initialization_can_launch_bank_builder_and_blocks_start_until_finished(installed, http_archive, monkeypatch):
    from bagu_trainer import startup
    from bagu_trainer.initialization import InitializationApp
    coordinator = startup.StartupCoordinator(engine_loader=lambda: pytest.fail("premature engine"))
    coordinator.prepare()
    app = InitializationApp(coordinator, bank_service=BankBuildService(installed.project, opener=http_archive.opener))
    app.root.withdraw()
    try:
        wait_for_ui(app, lambda: not app.busy)
        child = app.bank_setup
        child.source_list.selection_set(REPOSITORY)
        child._start()
        app._retry()
        assert not app.busy
        wait_for_ui(child, lambda: not child.busy)
        wait_for_ui(app, lambda: not app.busy)
        assert child.result.questions == 1
        assert next(item for item in coordinator.report.checks if item.name == "bank").ready
    finally:
        app._close()


def test_build_without_scripts_git_powershell_or_subprocesses(installed):
    package = Path(__file__).resolve().parents[1] / "bagu_trainer"
    shutil.copytree(package, installed.project / "bagu_trainer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (installed.project / "fixture.zip").write_bytes(archive_bytes())
    probe = r'''
import io, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
def forbidden(*args, **kwargs):
    raise AssertionError('external process called')
subprocess.Popen = forbidden
class Response(io.BytesIO):
    headers = {}
def opener(request, timeout):
    return Response((root / 'fixture.zip').read_bytes())
from bagu_trainer.bank_build.service import BankBuildService
from bagu_trainer.knowledge_base import KnowledgeBase
assert not (root / 'scripts').exists()
result = BankBuildService(root, opener=opener).build(['Snailclimb/JavaGuide'])
assert result.questions == 1 and KnowledgeBase().all()[0]['knowledge_tags']
assert 'bagu_trainer.engine' not in sys.modules
assert not any(name.startswith('scripts.') for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-B", "-c", probe, str(installed.project)],
        cwd=installed.project, env={**os.environ, "PATH": "", "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
