"""Cooperative cancellation, deadlines and structured progress for all stages."""
import math
import threading
import time
from dataclasses import dataclass, replace


class BuildError(Exception):
    def __init__(self, stage, message):
        self.stage = stage
        super().__init__(message)


class BuildCancelled(BuildError):
    pass


class BuildTimeout(BuildError):
    pass


@dataclass(frozen=True)
class SourceStatus:
    repository: str
    commit: str
    state: str = "waiting"
    stage: str = "prepare"
    attempt: int = 0
    reason: str = ""


class SourceBatchError(BuildError):
    def __init__(self, sources):
        self.sources = tuple(sources)
        self.failures = tuple(s for s in sources if s.state == "failed")
        details = "\n".join(f"{s.repository} @ {s.commit} [{s.stage}]：{s.reason}" for s in self.failures)
        stage = self.failures[0].stage if len(self.failures) == 1 else "prepare"
        super().__init__(stage, f"{len(self.failures)} 个来源准备失败，未激活部分题库。可仅重试失败来源。\n{details}")


@dataclass(frozen=True)
class BuildProgress:
    stage: str
    message: str
    completed: int = 0
    total: int = 0
    attempt: int = 1
    source: SourceStatus | None = None
    source_index: int = 0
    source_total: int = 0
    remaining_seconds: float = 0
    generation: str = ""
    repository: str = ""
    commit: str = ""


class BuildControl:
    def __init__(self, cancel=None, timeout=900, progress=None, clock=time.monotonic):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("构建超时必须是正数")
        self.cancel = cancel if cancel is not None else threading.Event()
        self.clock = clock
        self.deadline = clock() + timeout
        self.progress = progress
        self.stage = "prepare"
        self.timeout = timeout
        self.sources = {}
        self.current_source = None
        self.generation = ""

    def source_state(self, repository, state, reason=""):
        current = self.sources[repository]
        self.sources[repository] = replace(current, state=state, reason=reason)
        self.current_source = repository
        labels = {"waiting": "等待处理", "downloading": "准备来源", "completed": "来源就绪", "failed": "来源失败", "cancelled": "来源取消"}
        self.emit(current.stage, reason or labels[state])

    def interrupted_sources(self, error):
        for repository, source in list(self.sources.items()):
            if source.state in {"waiting", "downloading"}:
                state = "failed" if isinstance(error, BuildTimeout) and source.state == "downloading" else "cancelled"
                self.source_state(repository, state, str(error))
        self.current_source = None
        error.sources = tuple(self.sources.values())

    def check(self):
        if self.cancel.is_set():
            raise BuildCancelled(self.stage, "构建已取消，当前题库保持不变。")
        if self.clock() >= self.deadline:
            ready = sum(s.state == "completed" for s in self.sources.values())
            raise BuildTimeout(self.stage, f"整个任务总时限 {self.timeout:g} 秒已耗尽（来源就绪 {ready}/{len(self.sources)}），"
                               "当前题库保持不变。请增加任务总时限后重试，已完成缓存保留。")

    def _remaining_seconds(self):
        # Adding/subtracting a monotonic timestamp can round above the budget.
        return min(self.timeout, max(0, self.deadline - self.clock()))

    def remaining(self):
        self.check()
        return self._remaining_seconds()

    def emit(self, stage, message, completed=0, total=0, attempt=None):
        self.stage = stage
        source = self.sources.get(self.current_source)
        if source is not None:
            source = replace(source, stage=stage, attempt=source.attempt if attempt is None else attempt)
            self.sources[source.repository] = source
        index = list(self.sources).index(self.current_source) + 1 if source else 0
        if self.progress:
            # Observers cannot turn a committed build into a reported failure.
            try:
                self.progress(BuildProgress(stage, message, completed, total,
                    source.attempt if source else (attempt or 1), source, index, len(self.sources),
                    self._remaining_seconds(), self.generation,
                    source.repository if source else "", source.commit if source else ""))
            except Exception:
                pass

    def phase(self, stage, message, total=0):
        self.emit(stage, message, total=total)
        self.check()

    def wait(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            self.check()
            self.cancel.wait(min(0.1, end - self.clock(), self.remaining()))

    def ticker(self, stage, total):
        count = 0
        def tick():
            nonlocal count
            self.check()
            count += 1
            if count == 1 or count % 25 == 0:
                self.emit(stage, "正在处理本地条目", count, total)
                self.check()
        return tick
