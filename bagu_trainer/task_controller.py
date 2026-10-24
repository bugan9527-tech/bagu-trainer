"""Application task layer for background build/test/save jobs.

Separates task lifecycle from page visibility.  A task (bank build, model test,
model save) is owned by the application, not by whichever page happens to show
it; hiding a page (ordinary navigation) must not cancel a running task.  Only
explicit cancel / exit stop a task.

Bank build is the long-running case: the controller holds the cancel Event and
the build result so that page switches, late results and exit-waiting all share
one source of truth.  Three distinct states are reported:
  - building: a build is in flight (cancel available)
  - built:    a build finished successfully and a newer bank is staged/ready
  - applied:  the staged bank has been reloaded into the engine
"""
import queue
import threading
from dataclasses import dataclass, field

from .bank_build.control import BuildCancelled, BuildError


@dataclass
class BankTask:
    state: str = "idle"        # idle | building | built | applied | failed | cancelled
    stage: str = ""
    message: str = ""
    progress: dict = field(default_factory=dict)
    result: object = None
    cancel: threading.Event = field(default_factory=threading.Event)
    events: queue.Queue = field(default_factory=queue.Queue)
    worker: threading.Thread = None
    error: str = ""
    sources: dict = field(default_factory=dict)
    generation: str = ""
    repositories: list = field(default_factory=list)
    versions: dict = field(default_factory=dict)
    force_download: bool = False
    download_sizes: dict = field(default_factory=dict)
    cleaning: bool = False

    @property
    def busy(self):
        return self.state == "building" or self.cleaning


class TaskController:
    """Owns the bank-build task and forwards model/test/save through the panel.

    The controller itself is UI-independent; workers post plain values into the
    provided ``ui_events`` queue, which the application drains on the Tk thread.
    """

    def __init__(self, ui_events=None):
        self.ui_events = ui_events if ui_events is not None else queue.Queue()
        self.bank = BankTask()

    # -- bank build -----------------------------------------------------------
    def start_build(self, service, repositories, *, timeout=None, versions=None, retry_failed=None, force_download=False):
        if self.bank.busy:
            return False
        self.bank.state = "building"
        self.bank.stage = "prepare"
        self.bank.message = "正在准备下载…"
        self.bank.progress = {}
        self.bank.result = None
        self.bank.error = ""
        self.bank.sources = {}
        self.bank.generation = ""
        self.bank.repositories = list(repositories)
        self.bank.versions = dict(versions or {})
        self.bank.force_download = force_download
        self.bank.download_sizes = {}
        self.bank.cancel = threading.Event()
        events = self.bank.events
        cancel = self.bank.cancel

        def work():
            try:
                result = service.build(repositories, cancel=cancel, timeout=timeout, versions=versions,
                    retry_failed=retry_failed, force_download=force_download,
                    progress=lambda event: events.put(("progress", event)))
                events.put(("result", result))
            except BuildError as error:
                events.put(("error", error))
            except Exception as error:
                events.put(("error", ("prepare", f"初始化失败（{type(error).__name__}），可重试。", False)))

        self.bank.worker = threading.Thread(target=work, daemon=True, name="bagu-bank-build")
        self.bank.worker.start()
        return True

    def cancel_build(self):
        if self.bank.state == "building" and not self.bank.cancel.is_set():
            self.bank.cancel.set()
            return True
        return False

    def drain_bank(self):
        """Consume pending bank events; returns True if the terminal state changed."""
        changed = False
        try:
            while True:
                name, value = self.bank.events.get_nowait()
                if name == "progress":
                    self.bank.stage = value.stage
                    self.bank.message = value.message
                    self.bank.progress = {"completed": value.completed, "total": value.total}
                    if value.source:
                        self.bank.sources[value.source.repository] = value.source
                        if value.stage == "download" and value.total:
                            self.bank.download_sizes[value.source.repository] = value.total
                    if value.generation:
                        self.bank.generation = value.generation
                elif name == "result":
                    self.bank.result = value
                    self.bank.state = "built"
                    for source in getattr(value, "sources", ()):
                        self.bank.sources[source.repository] = source
                    changed = True
                elif name == "error":
                    if isinstance(value, BuildError):
                        stage, message, cancelled = value.stage, str(value), isinstance(value, BuildCancelled)
                        for source in getattr(value, "sources", ()):
                            self.bank.sources[source.repository] = source
                    else:
                        stage, message, cancelled = value
                    self.bank.error = message
                    self.bank.state = "cancelled" if cancelled else "failed"
                    changed = True
        except queue.Empty:
            pass
        return changed

    def mark_applied(self):
        if self.bank.state == "built":
            self.bank.state = "applied"

    # -- application teardown -------------------------------------------------
    def request_exit(self):
        """Cancel cancellable work; report whether a non-cancellable write is active."""
        self.cancel_build()
        return False  # bank build is cancellable; no non-cancellable write here
