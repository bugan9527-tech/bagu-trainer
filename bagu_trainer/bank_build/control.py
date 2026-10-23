"""Cooperative cancellation, deadlines and structured progress for all stages."""
import math
import threading
import time
from dataclasses import dataclass


class BuildError(Exception):
    def __init__(self, stage, message):
        self.stage = stage
        super().__init__(message)


class BuildCancelled(BuildError):
    pass


class BuildTimeout(BuildError):
    pass


@dataclass(frozen=True)
class BuildProgress:
    stage: str
    message: str
    completed: int = 0
    total: int = 0
    attempt: int = 1


class BuildControl:
    def __init__(self, cancel=None, timeout=900, progress=None, clock=time.monotonic):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("构建超时必须是正数")
        self.cancel = cancel if cancel is not None else threading.Event()
        self.clock = clock
        self.deadline = clock() + timeout
        self.progress = progress
        self.stage = "prepare"

    def check(self):
        if self.cancel.is_set():
            raise BuildCancelled(self.stage, "构建已取消，当前题库保持不变。")
        if self.clock() >= self.deadline:
            raise BuildTimeout(self.stage, "构建超时，当前题库保持不变；可增加超时后重试。")

    def remaining(self):
        self.check()
        return self.deadline - self.clock()

    def emit(self, stage, message, completed=0, total=0, attempt=1):
        self.stage = stage
        if self.progress:
            # Observers cannot turn a committed build into a reported failure.
            try:
                self.progress(BuildProgress(stage, message, completed, total, attempt))
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
