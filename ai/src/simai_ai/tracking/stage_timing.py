"""판정에 쓰지 않는 단계별 wall/thread CPU 계측. 시계 주입, 사람 수별 최근 256개 유계 표본.

사람 수는 World의 활성 트랙 수 또는 카메라별 검출 수의 최댓값(관측 단계)이다.
수신·디코드는 사람 수를 모르므로 unknown. 백분위는 각 구간 표본 자체에서 계산한다.
"""

from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import contextmanager
import math
import threading
import time


class StageTiming:
    def __init__(self, wall: Callable[[], float] = time.perf_counter,
                 cpu: Callable[[], float] = time.thread_time, capacity: int = 256):
        self.wall, self.cpu = wall, cpu
        self._lock = threading.Lock()
        self._samples = defaultdict(lambda: deque(maxlen=capacity))
        self._counts = defaultdict(int)

    @contextmanager
    def measure(self, stage: str, people: int | Callable[[], int] | None = None):
        w, c = self.wall(), self.cpu()
        try:
            yield
        finally:
            elapsed, cpu = (self.wall() - w) * 1000, (self.cpu() - c) * 1000
            self.record(stage, people() if callable(people) else people, elapsed, cpu)

    def record(self, stage: str, people: int | None, wall_ms: float, cpu_ms: float = 0):
        bucket = "unknown" if people is None else str(people) if people <= 6 else "7+"
        with self._lock:
            key = (stage, bucket)
            self._samples[key].append((wall_ms, cpu_ms))
            self._counts[key] += 1

    def summary(self):
        with self._lock:
            copied = [(key, list(vals), self._counts[key]) for key, vals in self._samples.items()]
        out = {}
        for (stage, people), vals, count in copied:
            metrics = {"count": count, "window_samples": len(vals)}
            for name, idx in (("wall", 0), ("thread_cpu", 1)):
                ordered = sorted(v[idx] for v in vals)
                metrics[name + "_ms"] = {
                    "mean": round(sum(ordered) / len(ordered), 3),
                    "p50": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 3),  # -662 추가(기존 키 의미 그대로)
                    "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
                    "max": round(ordered[-1], 3),
                }
            out.setdefault(stage, {})[people] = metrics
        return out
