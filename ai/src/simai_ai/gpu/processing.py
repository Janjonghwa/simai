"""촬영·전송 시계와 분리된 Tier 0 처리 부하 창. 시계는 호출자가 주입한다."""

import threading

from simai_ai.gpu.admission import LoadWindow

DEFAULT_PROCESSING_MAX_VALUES = 256


class ProcessingWindow:
    def __init__(self, budget_ms, *, monotonic, max_values=DEFAULT_PROCESSING_MAX_VALUES):
        self._clock = monotonic
        self._lock = threading.Lock()
        self._window = LoadWindow(budget_ms, max_values=max_values)
        self._started = None

    def run(self, fn, *args, **kwargs):
        with self._lock:
            self._started = self._clock()
        ok = False
        try:
            result = fn(*args, **kwargs)
            ok = True
            return result
        finally:
            with self._lock:
                elapsed = (self._clock() - self._started) * 1000
                self._window.add(elapsed if ok else None)
                self._started = None

    def take(self):
        with self._lock:
            if self._started is not None:
                # 완료 표본만 기다리면 장시간 멈춤을 놓친다. 진행 중 경과도 창 최댓값·p95·표본 수에 넣는다.
                self._window.add((self._clock() - self._started) * 1000)
            return self._window.take()

