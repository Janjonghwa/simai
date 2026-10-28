"""깨진 PTS 시각선의 프레임 시계 (명세 3.9 촬영 시각 추정, S15P31A101-315). 녹화 재생(RecordingReplay)이 쓴다.

C110 4대 모두 PTS 가 13장 묶음마다 +410/−360 ms 번갈아 뛴다. 녹화 도구·노트북 중계의 촬영 시각 추정은 PTS 를 따르므로
그런 녹화의 촬영 시각은 묶음째 약 0.4 s 앞뒤로 흔들리고 뒤로도 간다. 노트북 중계·녹화 도구는 이것을 감지하면 그 epoch 의
남은 프레임을 프레임 시계로 매긴다(`gateway/recorder/src/simai_recorder/capture_time.py` FrameClock). 이 모듈은 그
FrameClock 을 그대로 옮긴 것이다(ai 는 gateway 를 import 하지 않는다). 그래서 이미 찍힌 녹화를 재생해도 지금 LIVE 와 같은
촬영 시각이 나온다. 두 구현이 같은 값을 내는지는 같은 시험 벡터(gateway/recorder/tests/data/capture_clock_vector.json)로
확인한다. 고칠 때는 두 곳을 같이 고치고 벡터를 다시 만든다.

입력은 녹화 헤더의 원본 값(received_at_ms, source_pts)과 manifest 의 capture_time_model 뿐이다. 깨짐을 보기 전 프레임은
녹화 헤더 값을 그대로 둔다(그때까지 녹화 도구도 PTS 추정을 썼다). 공칭 fps·수신 시각을 모르면 고치지 않는다.
근거·수치는 docs/design/capture-time-broken-pts.md.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

BROKEN_STEP_MS = 150.0
BROKEN_WINDOW_FRAMES = 250
BROKEN_MIN_STEPS = 2
ENVELOPE_BIN_MS = 1000.0
ENVELOPE_MIN_SPAN_MS = 5000.0
ENVELOPE_MAX_SKEW = 0.005


@dataclass(frozen=True)
class CaptureClockModel:
    """녹화 manifest 의 capture_time_model 중 프레임 시계가 쓰는 값 (contracts/data/recording_manifest capture_time_model)."""

    latency_ms: float
    uncertainty_ms: float
    uncertainty_no_pts_ms: float
    window_ms: int

    @classmethod
    def from_manifest(cls, model: Mapping[str, Any]) -> CaptureClockModel:
        return cls(latency_ms=float(model["latency_ms"]), uncertainty_ms=float(model["uncertainty_ms"]),
                   uncertainty_no_pts_ms=float(model["uncertainty_no_pts_ms"]), window_ms=int(model["window_ms"]))


def lower_envelope_line(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """x 순서 점들 아래를 지나며 점까지 위로 거리 합이 가장 작은 직선 (기울기, 절편). 못 정하면 None.

    선형계획 풀이(Moon·Skelly·Towsley 1999, 단방향 지연의 시계 기울기 추정)와 같다: 아래 볼록 껍질에서 x 평균을 품은 변.
    """
    hull: list[tuple[float, float]] = []
    for p in points:
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) > 0:
                break
            hull.pop()
        hull.append(p)
    xm = sum(p[0] for p in points) / len(points)
    for (x1, y1), (x2, y2) in zip(hull, hull[1:]):
        if x1 <= xm <= x2 and x2 > x1:
            slope = (y2 - y1) / (x2 - x1)
            return slope, y1 - slope * x1
    return None


class FrameClock:
    """한 stream_epoch 의 깨진 PTS 감지와 프레임 시계 추정. 프레임마다 순서대로 한 번씩 update 한다.

    gateway/recorder capture_time.FrameClock 과 같은 코드다(모듈 설명). 반환은 (estimated_capture_time_ms,
    timestamp_uncertainty_ms) 이고, 아직 깨짐을 보지 못했으면 None 이다.
    """

    def __init__(self, model: CaptureClockModel, interval_ms: float) -> None:
        if not 0 < interval_ms < float("inf"):
            raise ValueError(f"interval_ms 는 양의 유한한 값이어야 한다 (받은 값 {interval_ms!r})")
        self._model = model
        self._interval = interval_ms
        self._index = -1
        self._clock: float | None = None
        self._prev_pts: float | None = None
        self._broken: deque[int] = deque()
        self._bins: deque[list[float]] = deque()
        self.broken_since: int | None = None

    def update(self, received_at_ms: int, pts_ms: float | None) -> tuple[int, float] | None:
        self._index += 1
        step = self._interval
        if self._clock is None:
            self._clock = 0.0
        elif pts_ms is not None and self._prev_pts is not None:
            d = pts_ms - self._prev_pts
            if 0.5 * self._interval <= d <= 2.5 * self._interval:
                step = d
            if abs(d - self._interval) > BROKEN_STEP_MS:
                self._broken.append(self._index)
                if self.broken_since is None and (d < self._interval or len(self._broken) >= BROKEN_MIN_STEPS):
                    self.broken_since = self._index
            self._clock += step
        else:
            self._clock += step
        self._prev_pts = pts_ms
        while self._broken and self._broken[0] <= self._index - BROKEN_WINDOW_FRAMES:
            self._broken.popleft()

        x = self._clock
        y = received_at_ms - x
        b = self._bins
        k = x // ENVELOPE_BIN_MS
        if b and b[-1][0] == k:
            if y < b[-1][2]:
                b[-1] = [k, x, y]
        else:
            b.append([k, x, y])
        while b[0][1] < x - self._model.window_ms:
            b.popleft()
        if self.broken_since is None:
            return None

        m = self._model
        line = None
        warm = b[-1][1] - b[0][1] >= ENVELOPE_MIN_SPAN_MS
        if warm:
            line = lower_envelope_line([(p[1], p[2]) for p in b])
            if line is not None and abs(line[0]) > ENVELOPE_MAX_SKEW:
                line = None
        floor = min(p[2] for p in b) if line is None else line[0] * x + line[1]
        floor = min(floor, y)
        return round(x + floor - m.latency_ms), m.uncertainty_ms if warm else m.uncertainty_no_pts_ms


def pts_to_ms(pts: int | None, timebase: str | None) -> float | None:
    """source_pts·pts_timebase("1/90000") → ms. 녹화 도구와 같은 값이 나오도록 분수로 계산한다."""
    if pts is None or timebase is None:
        return None
    return float(pts * Fraction(timebase) * 1000)


@dataclass(frozen=True)
class SegmentRepair:
    """한 세그먼트(stream_epoch)의 프레임 시계 결과. times[i] 가 None 이면 그 프레임은 녹화 헤더 그대로다."""

    broken_since: int | None  # 프레임 시계로 바꾼 세그먼트 안 프레임 위치(0 부터). None 이면 고치지 않았다
    times: tuple[tuple[int, float] | None, ...]


def repair_segment(frames: Sequence[tuple[int, int | None, str | None]], nominal_fps: float | None,
                   model: CaptureClockModel) -> SegmentRepair:
    """세그먼트 프레임(received_at_ms, source_pts, pts_timebase)을 녹화 순서대로 받아 프레임 시계로 다시 매긴다.

    지나간 프레임만 쓴다(LIVE 와 같은 인과 추정). 공칭 fps 를 모르면 아무것도 고치지 않는다.
    """
    if nominal_fps is None or not 0 < nominal_fps < float("inf"):
        return SegmentRepair(None, (None,) * len(frames))
    clock = FrameClock(model, 1000.0 / nominal_fps)
    times = tuple(clock.update(received, pts_to_ms(pts, tb)) for received, pts, tb in frames)
    return SegmentRepair(clock.broken_since, times)
