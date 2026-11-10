"""로컬 공유 채널 진단. 판정 시계와 분리된 wall 시계, 유계 표본·키만 보관한다."""

from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
import math
import threading
import time


class ChannelTiming:
    def __init__(self, *, clock: Callable[[], float] = time.perf_counter,
                 capacity: int = 256, max_series: int = 128):
        if capacity < 1 or max_series < 1:
            raise ValueError("계측 상한은 양수여야 한다")
        self.clock = clock
        self.capacity, self.max_series = capacity, max_series
        self._lock = threading.Lock()
        self._series = {}
        self._overflow = 0

    def record(self, stage: str, ms: float | None = None, *, camera: str | None = None):
        # 외부 카메라 ID도 키·길이 상한에 포함한다. 프레임·사람 ID·영상은 받지 않는다.
        key = (stage, camera)
        with self._lock:
            if len(stage) > 80 or (camera is not None and len(camera) > 128):
                self._overflow += 1
                return
            if key not in self._series:
                if len(self._series) >= self.max_series:
                    self._overflow += 1
                    return
                self._series[key] = [0, deque(maxlen=self.capacity)]
            series = self._series[key]
            series[0] += 1
            if ms is not None and math.isfinite(ms):
                series[1].append(ms)

    @contextmanager
    def measure(self, stage: str, *, camera: str | None = None):
        start = self.clock()
        try:
            yield
        finally:
            self.record(stage, (self.clock() - start) * 1000, camera=camera)

    def summary(self):
        with self._lock:
            copied = [(key, count, list(samples)) for key, (count, samples) in self._series.items()]
            overflow = self._overflow
        rows = []
        for (stage, camera), count, samples in copied:
            row = {"stage": stage, "camera": camera, "count": count, "window_samples": len(samples)}
            if samples:
                ordered = sorted(samples)
                row["ms"] = {"mean": round(sum(ordered) / len(ordered), 3), "max": round(ordered[-1], 3)}
                for p in (50, 95, 99):
                    row["ms"][f"p{p}"] = round(ordered[math.ceil(len(ordered) * p / 100) - 1], 3)
            rows.append(row)
        return {"series": rows, "overflow": overflow, "capacity": self.capacity, "max_series": self.max_series}
