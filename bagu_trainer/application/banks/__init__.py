"""Background question-bank build and serialized application workflow."""
import threading

from bagu_trainer.application.events import public_value


class BankWorkflow:
    def __init__(self, *, executor, lock, emit, publish):
        self._executor = executor
        self._lock = lock
        self._emit = emit
        self._publish = publish
        self._cancel = threading.Event()
        self._task = {"state": "idle", "message": "选择来源后下载并构建题库。"}

    def task_snapshot(self):
        with self._lock:
            return dict(self._task)

    def is_busy(self):
        return self.task_snapshot()["state"] in {"building", "applying", "cleaning"}

    def start_build(self, values):
        from bagu_trainer.infrastructure.bank_build.control import BuildCancelled
        from bagu_trainer.infrastructure.bank_build.control import BuildError
        from bagu_trainer.infrastructure.bank_build.service import BankBuildService

        with self._lock:
            if self._task["state"] in {"building", "applying", "cleaning"}:
                raise ValueError("题库任务正在进行。")
            retry_failed = None
            repositories = values.get("repositories") or []
            if values.get("retry_failed"):
                retry_failed = [repo for repo, source in self._task.get("sources", {}).items()
                                if source["state"] == "failed"]
                repositories = self._task.get("repositories", [])
                if not retry_failed:
                    raise ValueError("没有需要重试的失败来源。")
            self._cancel = threading.Event()
            cancel = self._cancel
            self._task = {"state": "building", "stage": "prepare", "message": "正在准备题库…",
                          "repositories": repositories, "sources": {}}

        def progress(event):
            with self._lock:
                self._task.update(stage=event.stage, message=event.message,
                    completed=event.completed, total=event.total)
                if event.source is not None:
                    self._task["sources"][event.source.repository] = public_value(event.source)
                snapshot = dict(self._task)
            self._emit({"type": "bank_progress", "data": snapshot})

        def work():
            try:
                result = BankBuildService().build(repositories, retry_failed=retry_failed,
                    timeout=values.get("timeout", 1800), cancel=cancel, progress=progress,
                    force_download=bool(values.get("force_download")))
                with self._lock:
                    for source in getattr(result, "sources", ()):
                        self._task["sources"][source.repository] = public_value(source)
                    self._task.update(state="applying", message=f"已构建 {result.questions} 个知识点，正在应用。")
                # The event is consumed by the existing serialized command lane.
                self._emit({"type": "bank_built", "data": {"questions": result.questions}})
            except Exception as error:
                with self._lock:
                    for source in getattr(error, "sources", ()):
                        self._task["sources"][source.repository] = public_value(source)
                    self._task.update(state="cancelled" if isinstance(error, BuildCancelled) else "failed",
                        message=str(error) if isinstance(error, BuildError) else "题库构建失败，请检查来源及数据目录后重试。")
                self._publish()

        self._executor.submit(work)
        self._publish()
        return {"ok": True}

    def cancel_build(self):
        self._cancel.set()

    def apply_pending(self, maintenance):
        """Apply a completed bank on the caller's serialized command lane."""
        with self._lock:
            if self._task["state"] != "applying":
                return False
            maintenance.apply_bank()
            self._task.update(state="applied", message="题库已构建并应用。")
            return True

    def close(self):
        self._cancel.set()
