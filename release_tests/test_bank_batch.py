"""Batch preparation uses synthetic ZIPs, localhost HTTP and isolated banks."""
import collections
import json
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bagu_trainer.bank_build.control import BuildCancelled, BuildTimeout, SourceBatchError
from bagu_trainer.bank_build.service import BankBuildService
from bagu_trainer.bank_storage import active_bank_dir
from bagu_trainer.knowledge_base import KnowledgeBase
from release_tests.test_bank_build import installed, archive_bytes, MARKDOWN, wait_for_ui


REPOS = ["fixture/first", "fixture/middle", "fixture/last"]
COMMITS = [c * 40 for c in "abc"]


@pytest.fixture
def batch(installed):
    sources = [{"repository": repo, "directory": f"Repo{i}", "audited_commit": COMMITS[i]}
               for i, repo in enumerate(REPOS)]
    installed.manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    state = SimpleNamespace(calls=[], failures={}, delay={}, bodies={
        repo: archive_bytes([("snapshot/README.md", MARKDOWN.replace("什么是数据库索引？", f"数据库索引的第{i}种用途？"))])
        for i, repo in enumerate(REPOS)})
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            repo = urllib.parse.unquote(self.path[1:])
            codes = state.failures.get(repo, [])
            code = codes.pop(0) if codes else 200
            time.sleep(state.delay.get(repo, 0))
            try:
                self.send_response(code)
                self.send_header("Content-Length", str(len(state.bodies[repo]) if code == 200 else 0))
                self.end_headers()
                if code == 200:
                    self.wfile.write(state.bodies[repo])
            except (BrokenPipeError, ConnectionError):
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def opener(request, timeout):
        parts = urllib.parse.urlsplit(request.full_url).path.split("/")
        repo = "/".join(parts[2:4])
        assert parts[-1] == COMMITS[REPOS.index(repo)]
        state.calls.append((repo, timeout))
        return urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/" + urllib.parse.quote(repo, safe=""), timeout=timeout)
    state.service = BankBuildService(installed.project, opener=opener)
    state.install = installed
    # Start with a real usable bank and a readable isolated SQLite history.
    old = state.service.build([REPOS[0]])
    state.old_questions = KnowledgeBase(old.directory / "questions.json").all()
    installed.learning.unlink()
    with sqlite3.connect(installed.learning) as db:
        db.execute("CREATE TABLE learning (answer TEXT)")
        db.execute("INSERT INTO learning VALUES ('previous answer')")
    state.original = {p: p.read_bytes() for p in (installed.learning, installed.legacy, installed.settings,
                                                installed.data / "active-bank.json")}
    state.old_directory = old.directory
    state.calls.clear()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def assert_previous_usable(batch):
    assert {p: p.read_bytes() for p in batch.original} == batch.original
    assert active_bank_dir(batch.install.project) == batch.old_directory
    assert KnowledgeBase(batch.old_directory / "questions.json").all() == batch.old_questions
    with sqlite3.connect(batch.install.learning) as db:
        assert db.execute("SELECT answer FROM learning").fetchall() == [("previous answer",)]


def statuses(events):
    return {e.source.repository: e.source for e in events if e.source}


def test_all_selected_success_with_independent_states_and_progress(batch):
    events = []
    result = batch.service.build(REPOS, force_download=True, progress=events.append)
    assert result.questions == 3
    assert [repo for repo, _ in batch.calls] == REPOS
    for i, repo in enumerate(REPOS, 1):
        source_events = [e for e in events if e.source and e.source.repository == repo]
        assert {e.source.state for e in source_events} == {"waiting", "downloading", "completed"}
        assert all(e.source_index == i and e.source_total == 3 for e in source_events)
        assert all(e.source.commit == COMMITS[i - 1] for e in source_events)
        assert any(e.stage == "unpack" for e in source_events)
        assert source_events[-1].source.state == "completed"
    activate = next(i for i, e in enumerate(events) if e.stage == "activate")
    assert all(e.source.state != "completed" for e in events[activate:] if e.source)
    assert all(s.state == "completed" for s in result.sources)


@pytest.mark.parametrize("failed", [[0], [1], [0, 1]])
@pytest.mark.parametrize("force_retry", [False, True])
def test_failures_continue_and_only_failed_are_retried(batch, failed, force_retry):
    for i in failed:
        batch.failures[REPOS[i]] = [404]
    events = []
    with pytest.raises(SourceBatchError) as caught:
        batch.service.build(REPOS, force_download=True, progress=events.append)
    error = caught.value
    assert [repo for repo, _ in batch.calls] == REPOS
    assert [s.repository for s in error.failures] == [REPOS[i] for i in failed]
    assert [s.state for s in error.sources] == ["failed" if i in failed else "completed" for i in range(3)]
    assert not any(e.stage in {"extract", "activate"} for e in events)
    for source in error.failures:
        assert source.commit in str(error) and source.repository in str(error)
        assert source.stage == "download" and "HTTP 404" in source.reason and "核对" in source.reason
    assert_previous_usable(batch)
    result = BankBuildService(batch.install.project, opener=batch.service.opener).build(
        REPOS, retry_failed=[REPOS[i] for i in failed], force_download=force_retry)
    assert result.questions == 3
    # The seeded first source has a still-valid previous cache if no refresh is requested.
    expected = [REPOS[i] for i in failed if force_retry or i != 0]
    assert [repo for repo, _ in batch.calls[3:]] == expected


def test_single_request_timeout_continues_next_source(batch):
    batch.delay[REPOS[1]] = .15
    with pytest.raises(SourceBatchError) as caught:
        batch.service.build(REPOS, force_download=True, request_timeout=.025, download_retries=0, timeout=10)
    assert [repo for repo, _ in batch.calls] == REPOS
    failure = caught.value.failures[0]
    assert failure.repository == REPOS[1] and failure.stage == "download"
    assert "单次网络" in failure.reason and "0.025" in failure.reason and "网络" in failure.reason
    assert_previous_usable(batch)


def test_bad_archive_continues_and_records_actionable_unpack_failure(batch):
    valid = batch.bodies[REPOS[1]]
    batch.bodies[REPOS[1]] = b"broken ZIP"
    with pytest.raises(SourceBatchError) as caught:
        batch.service.build(REPOS, force_download=True)
    assert [repo for repo, _ in batch.calls] == REPOS
    failure = caught.value.failures[0]
    assert failure.repository == REPOS[1] and failure.stage == "unpack"
    assert "ZIP" in failure.reason and "重新下载" in failure.reason
    assert_previous_usable(batch)
    batch.bodies[REPOS[1]] = valid
    assert batch.service.build(REPOS, retry_failed=[REPOS[1]]).questions == 3
    assert [repo for repo, _ in batch.calls[3:]] == [REPOS[1]]


@pytest.mark.parametrize("already_cancelled", [True, False])
def test_cancel_at_task_start_records_every_selected_source(batch, already_cancelled):
    cancel = threading.Event()
    if already_cancelled:
        cancel.set()
    def progress(event):
        if event.stage == "prepare":
            cancel.set()
    with pytest.raises(BuildCancelled) as caught:
        batch.service.build(REPOS, cancel=cancel, progress=progress)
    assert [s.repository for s in caught.value.sources] == REPOS
    assert all(s.state == "cancelled" and s.stage == "prepare" for s in caught.value.sources)
    assert not batch.calls
    assert_previous_usable(batch)


def test_total_deadline_stops_batch_and_retains_finished_cache(batch):
    events = []
    batch.delay[REPOS[1]] = .3
    with pytest.raises(BuildTimeout) as caught:
        batch.service.build(REPOS, force_download=True, request_timeout=1, timeout=.15,
                            download_retries=0, progress=events.append)
    assert "整个任务总时限" in str(caught.value) and "来源就绪 1/3" in str(caught.value)
    assert [s.state for s in caught.value.sources] == ["completed", "failed", "cancelled"]
    assert [repo for repo, _ in batch.calls] == REPOS[:2]
    # Windows may complete source one within a clock tick; subtraction can
    # exceed the configured budget by a few picoseconds due to float rounding.
    assert 0 < batch.calls[1][1] <= .15 + 1e-9
    assert_previous_usable(batch)
    batch.delay.clear()
    batch.service.build(REPOS)
    assert [repo for repo, _ in batch.calls[2:]] == REPOS[1:]


@pytest.mark.parametrize("stage", ["waiting", "download", "unpack", "completed"])
def test_cancel_preserves_current_bank_and_pending_source_states(batch, stage):
    cancel = threading.Event()
    events = []
    def progress(event):
        events.append(event)
        source = event.source
        if source and ((stage in {"waiting", "completed"} and source.state == stage)
                       or (source.repository == REPOS[1] and event.stage == stage)):
            cancel.set()
    with pytest.raises(BuildCancelled) as caught:
        batch.service.build(REPOS, force_download=True, cancel=cancel, progress=progress)
    assert all(s.state in {"completed", "cancelled"} for s in caught.value.sources)
    assert statuses(events) == {s.repository: s for s in caught.value.sources}
    assert_previous_usable(batch)
    assert batch.service.build(REPOS).questions == 3


def test_automatic_retry_count_and_reason_are_per_source(batch):
    batch.failures[REPOS[1]] = [503, 503]
    events = []
    batch.service.build(REPOS, force_download=True, progress=events.append)
    assert collections.Counter(repo for repo, _ in batch.calls) == {REPOS[0]: 1, REPOS[1]: 3, REPOS[2]: 1}
    assert statuses(events)[REPOS[1]].attempt == 3
    assert {e.attempt for e in events if e.source and e.source.repository == REPOS[1] and e.stage == "retry"} == {2, 3}


def test_default_task_budget_scales_with_selection_and_explicit_limit_is_respected(batch):
    events = []
    batch.service.build(REPOS, progress=events.append)
    assert 2690 < events[0].remaining_seconds <= 2700
    events.clear()
    batch.service.build(REPOS, timeout=42, progress=events.append)
    assert 40 < events[0].remaining_seconds <= 42


def test_retry_only_does_not_download_a_damaged_completed_source(batch):
    batch.failures[REPOS[1]] = [404]
    with pytest.raises(SourceBatchError):
        batch.service.build(REPOS, force_download=True)
    # Invalidate a previously completed source; do not silently broaden retry scope.
    from bagu_trainer.bank_build.source_cache import cache_directory
    from bagu_trainer.bank_build.service import load_sources
    source = load_sources(batch.install.manifest)[REPOS[2]]
    directory = cache_directory(batch.install.data / "source-cache", source)
    (directory / "complete.json").unlink()
    with pytest.raises(SourceBatchError) as caught:
        batch.service.build(REPOS, retry_failed=[REPOS[1]])
    assert [repo for repo, _ in batch.calls[3:]] == [REPOS[1]]
    assert caught.value.failures[0].repository == REPOS[2]
    assert caught.value.failures[0].stage == "cache"
    assert "重新构建" in caught.value.failures[0].reason
    assert_previous_usable(batch)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk batch view")
def test_batch_ui_keeps_responsive_and_retries_original_failed_selection(batch):
    from bagu_trainer.tk_runtime import create_root
    from bagu_trainer.bank_setup import BankSetupWindow
    parent = create_root()
    parent.withdraw()
    window = BankSetupWindow(parent, service=batch.service)
    window.root.withdraw()
    ui_errors = []
    parent.report_callback_exception = lambda *args: ui_errors.append(args)
    try:
        window.source_list.selection_set(REPOS)
        window.force_download.set(True)
        batch.failures[REPOS[1]] = [404]
        batch.delay[REPOS[2]] = .15
        ticks = []
        parent.after(25, lambda: ticks.append(True))
        window._start()
        assert window.retry_failed_button.instate(["disabled"])
        wait_for_ui(window, lambda: not window.busy)
        assert ticks and not ui_errors
        assert [s.state for s in window.source_statuses.values()] == ["completed", "failed", "completed"]
        assert window.source_list.set(REPOS[1], "state") == "失败"
        assert "HTTP 404" in window.source_list.set(REPOS[1], "reason")
        assert not window.retry_failed_button.instate(["disabled"])
        assert "2700" in window.budget_text.get() and "5 秒" in window.budget_text.get()
        assert_previous_usable(batch)
        window.source_list.selection_set(REPOS[0])  # Retry retains the original full selection.
        window._start(retry_only=True)
        wait_for_ui(window, lambda: not window.busy)
        assert window.result.questions == 3
        assert [repo for repo, _ in batch.calls[3:]] == [REPOS[1]]
        assert window.retry_failed_button.instate(["disabled"])
        assert not ui_errors
    finally:
        window._close()
        parent.destroy()
