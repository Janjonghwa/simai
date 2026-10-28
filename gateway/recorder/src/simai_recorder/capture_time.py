"""촬영 시각 추정 (명세 3.9 촬영 시각 계약, C110 기본값 RECEIVE_ESTIMATED).

C110 은 RTCP Sender Report 의 카메라 시각이 연속 보고 사이 약 ±400ms 흔들려 쓸 수 없다(3.3). 그래서 노트북 수신 시각에서
추정 지연을 뺀다. 수신 시각을 그대로 쓰면 Wi-Fi 도착 지터(S15P31A101-9 실측 p95 약 41ms)가 그대로 들어가므로,
카메라 PTS(RTP 시계)가 있으면 PTS 간격을 믿고 원점만 수신 시각으로 맞춘다.

    offset    = 최근 window_ms 안 프레임들의 min(received_at_ms − pts_ms)   # 가장 덜 늦게 온 프레임이 기준
    estimated = pts_ms + offset − latency_ms

창 안의 최솟값을 쓰므로 카메라 RTP 시계와 노트북 시계의 느린 표류(수십 ppm)를 따라간다. PTS 가 없으면
received_at_ms − latency_ms 를 쓰고 불확실성을 더 크게 둔다. 원본 received_at_ms·source_pts 를 함께 기록하므로
G0 LED 시험으로 latency_ms 가 정해지면 사이드카를 다시 계산할 수 있다.

깨진 PTS 시각선(S15P31A101-315). C110 4대 모두 PTS 가 13장 묶음마다 +410/−360 ms 번갈아 뛴다(묶음 안 간격 40 ms,
장기 비율은 맞음). 위 식은 PTS 를 따르므로 추정 촬영 시각이 묶음째 약 0.4 s 앞뒤로 흔들리고 뒤로도 간다. 그래서 같은 epoch
안에서 이웃 프레임의 PTS 간격이 공칭 간격에서 BROKEN_STEP_MS 넘게 벗어난 걸음이 최근 BROKEN_WINDOW_FRAMES 장 안에
BROKEN_MIN_STEPS 번 생기거나 PTS 가 그만큼 뒤로 가면, 그 epoch 의 남은 프레임은 프레임 시계(FrameClock)로 매긴다(되돌리지 않음).
PTS 가 앞으로 벌어진 걸음 중 수신 간격도 같이 벌어진 것(카메라 멈춤 뒤 재개)은 깨진 걸음으로 세지 않는다(S15P31A101-316).

    clock     = 이어 붙인 PTS: 간격이 공칭의 0.5~2.5 배면 그 간격(빠진 프레임 1장까지 센다), 아니면 공칭 간격 1장
    floor     = 최근 window_ms 의 1초 칸별 min(received_at_ms − clock) 아래를 지나는 직선(하한 포락선, 위로 거리 합 최소)
    estimated = clock + floor(clock) − latency_ms

지나간 프레임만 쓴다(인과). 직선 맞춤은 칸이 ENVELOPE_MIN_SPAN_MS 넘게 쌓인 뒤부터이고 그 전에는 최솟값(기울기 0)이며,
그동안의 불확실성은 PTS 없음 값(uncertainty_no_pts_ms)이다. 원본 source_pts·received_at_ms 는 헤더에 그대로 남는다.
PTS 가 멀쩡한 스트림(흔들림·B 프레임 재정렬·멈춤 포함)은 깨짐으로 보지 않아 위 PTS 식 결과가 그대로다. 공칭 fps 를 모르면 감지하지 않는다.

단조 보장(S15P31A101-316). 어느 식이든 한 epoch 안에서 내보내는 촬영 시각이 직전 값 + 1 ms 보다 작으면 직전 값 + 1 ms 로
올리고, 올린 양만큼 그 프레임의 불확실성을 키운다(EpochMonotonic). Tier 0 는 뒤로 간 프레임을 OUT_OF_ORDER 로, 같은 시각의
결과를 ResultGate 에서 버리기 때문이다(명세 5.1). 올린 횟수·최대 올림량은 epoch 마다 센다(raised, max_raise_ms).
ai 녹화 재생(simai_ai.io.capture_clock)이 같은 FrameClock 을 옮겨 쓴다(ai 는 gateway 를 import 하지 않음). 두 구현이 같은 값을
내는지 tests/data/capture_clock_vector.json 으로 확인한다. 근거·수치는 docs/design/capture-time-broken-pts.md.

시계는 주입받는다(이 모듈은 현재 시각을 읽지 않는다).
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from fractions import Fraction

TIMESTAMP_SOURCES = ("CAMERA_STREAM", "RECEIVE_ESTIMATED", "FILE", "SIM")


@dataclass(frozen=True)
class CaptureTimeModel:
    """촬영 시각 추정 설정. 기본값은 G0 LED 시험 전 잠정값이고 manifest 에 그대로 남긴다.

    latency_ms: 노출 → 노트북 수신의 고정 지연(인코딩·전송). G0 전에는 모르므로 0.
    uncertainty_ms: PTS 로 추정할 때 timestamp_uncertainty_ms. 잠정값(명세 12.7 예시 수준).
    uncertainty_no_pts_ms: PTS 가 없을 때. -9 실측 도착 지터 p95(약 41ms)를 덮도록 잡은 잠정값.
    window_ms: offset 최솟값을 찾는 창.
    """

    timestamp_source: str = "RECEIVE_ESTIMATED"
    latency_ms: float = 0.0
    uncertainty_ms: float = 20.0
    uncertainty_no_pts_ms: float = 45.0
    window_ms: int = 30_000

    def __post_init__(self) -> None:
        if self.timestamp_source not in TIMESTAMP_SOURCES:
            raise ValueError(f"timestamp_source {self.timestamp_source!r} 는 {TIMESTAMP_SOURCES} 중 하나여야 한다")
        for name in ("latency_ms", "uncertainty_ms", "uncertainty_no_pts_ms"):
            v = getattr(self, name)
            if not 0 <= v < float("inf"):
                raise ValueError(f"{name}: 0 이상의 유한한 값이어야 한다 (받은 값 {v!r})")
        if self.window_ms <= 0:
            raise ValueError("window_ms 는 양수여야 한다")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CaptureTime:
    estimated_capture_time_ms: int
    timestamp_uncertainty_ms: float


CLOCK_PTS = "pts"
CLOCK_FRAMES = "frame_clock"

BROKEN_STEP_MS = 150.0  # B 프레임 재정렬(80 ms 이하)·흔들림보다 크고 C110 묶음 점프(약 360~440 ms)보다 작다(-313 과 같은 기준)
BROKEN_WINDOW_FRAMES = 250  # 25 fps 에서 10 s
BROKEN_MIN_STEPS = 2  # 한 번의 앞으로 뛴 걸음(카메라 멈춤 뒤 재개)은 깨짐으로 보지 않는다
# 앞으로 벌어진 PTS 걸음에서 수신 간격도 공칭보다 BROKEN_STEP_MS 넘게, 그리고 PTS 가 벌어진 양의 절반 이상 벌어졌으면 멈춤이다
# (S15P31A101-316). C110 묶음 점프는 수신 간격이 그대로라(실녹화 최대 273 ms, 대부분 40~100 ms) 거의 걸리지 않고, 걸려도 뒤로 가는
# 점프가 바로 감지한다.
ENVELOPE_BIN_MS = 1000.0
ENVELOPE_MIN_SPAN_MS = 5000.0
ENVELOPE_MAX_SKEW = 0.005  # 하한 직선 기울기 상한(5000 ppm). 넘으면 맞춤을 믿지 않고 최솟값을 쓴다


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
    """한 stream_epoch 의 깨진 PTS 감지와 프레임 시계 추정 (모듈 설명). 프레임마다 순서대로 한 번씩 update 한다.

    interval_ms: 공칭 프레임 간격(1000 / nominal_fps). 감지·이어 붙이기에만 쓰고 실제 간격은 하한 직선 기울기가 맞춘다.
    """

    def __init__(self, model: CaptureTimeModel, interval_ms: float) -> None:
        if not 0 < interval_ms < float("inf"):
            raise ValueError(f"interval_ms 는 양의 유한한 값이어야 한다 (받은 값 {interval_ms!r})")
        self._model = model
        self._interval = interval_ms
        self._index = -1
        self._clock: float | None = None
        self._prev_pts: float | None = None
        self._prev_received: int | None = None
        self._broken: deque[int] = deque()  # 깨진 걸음이 난 프레임 번호(최근 BROKEN_WINDOW_FRAMES 장)
        self._bins: deque[list[float]] = deque()  # [칸 번호, clock, received − clock] 칸별 최솟값
        self.broken_since: int | None = None  # 프레임 시계로 바꾼 프레임 번호(epoch 안 0 부터). None 이면 PTS 그대로

    def update(self, received_at_ms: int, pts_ms: float | None) -> CaptureTime | None:
        """이 프레임까지 보고 프레임 시계 추정을 낸다. 아직 깨짐을 보지 못했으면 None(PTS 추정을 쓴다)."""
        self._index += 1
        step = self._interval
        if self._clock is None:
            self._clock = 0.0
        elif pts_ms is not None and self._prev_pts is not None:
            d = pts_ms - self._prev_pts
            if 0.5 * self._interval <= d <= 2.5 * self._interval:
                step = d
            if abs(d - self._interval) > BROKEN_STEP_MS and not self._stalled(d, received_at_ms):
                self._broken.append(self._index)
                if self.broken_since is None and (d < self._interval or len(self._broken) >= BROKEN_MIN_STEPS):
                    self.broken_since = self._index
            self._clock += step
        else:
            self._clock += step
        self._prev_pts = pts_ms
        self._prev_received = received_at_ms
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
        return CaptureTime(round(x + floor - m.latency_ms), m.uncertainty_ms if warm else m.uncertainty_no_pts_ms)

    def _stalled(self, pts_step_ms: float, received_at_ms: int) -> bool:
        """앞으로 벌어진 PTS 걸음이 카메라 멈춤 뒤 재개인지: 수신 간격도 같이 벌어졌다(STALL 기준, 모듈 상수 설명)."""
        if self._prev_received is None or pts_step_ms <= self._interval:
            return False
        gap = received_at_ms - self._prev_received - self._interval
        return gap > BROKEN_STEP_MS and 2 * gap >= pts_step_ms - self._interval


class EpochMonotonic:
    """한 epoch 에서 내보내는 촬영 시각을 앞으로만 가게 한다 (S15P31A101-316, 모듈 설명 "단조 보장").

    직전 값 + 1 ms 보다 작으면 직전 값 + 1 ms 로 올리고 올린 양(ms)만큼 불확실성을 키운다(올린 값은 추정이 아니라 순서를
    지키려고 정한 값이다). raised 는 올린 프레임 수, max_raise_ms 는 가장 크게 올린 양이다.
    """

    def __init__(self) -> None:
        self.last: int | None = None
        self.raised = 0
        self.max_raise_ms = 0

    def apply(self, time_ms: int, uncertainty_ms: float) -> tuple[int, float]:
        if self.last is not None and time_ms <= self.last:
            lift = self.last + 1 - time_ms
            self.raised += 1
            self.max_raise_ms = max(self.max_raise_ms, lift)
            time_ms, uncertainty_ms = self.last + 1, uncertainty_ms + lift
        self.last = time_ms
        return time_ms, uncertainty_ms


class ReceiveTimeEstimator:
    """한 stream_epoch 의 촬영 시각 추정기. 재접속하면 새로 만든다(PTS 원점이 바뀐다).

    nominal_fps 를 주면 깨진 PTS 시각선을 감지해 프레임 시계로 바꾼다(모듈 설명). 프레임마다 순서대로 한 번씩 estimate 한다.
    내보내는 값은 epoch 안에서 앞으로만 간다(EpochMonotonic). raised·max_raise_ms 는 이 epoch 에서 올린 횟수·최대 올림량(ms).
    """

    def __init__(self, model: CaptureTimeModel, pts_timebase: Fraction | None,
                 nominal_fps: float | None = None) -> None:
        self._model = model
        self._tb_ms = pts_timebase * 1000 if pts_timebase is not None else None
        # (received_at_ms, offset) 단조 증가 offset 덱: 앞이 창 안의 최솟값
        self._window: deque[tuple[int, float]] = deque()
        ok = nominal_fps is not None and 0 < nominal_fps < float("inf")
        self._frames = FrameClock(model, 1000.0 / nominal_fps) if ok else None
        self._monotonic = EpochMonotonic()

    @property
    def mode(self) -> str:
        return CLOCK_FRAMES if self.broken_since is not None else CLOCK_PTS

    @property
    def broken_since(self) -> int | None:
        """프레임 시계로 바꾼 프레임 번호(epoch 안 0 부터 estimate 호출 순서). 바꾸지 않았으면 None."""
        return self._frames.broken_since if self._frames is not None else None

    @property
    def raised(self) -> int:
        return self._monotonic.raised

    @property
    def max_raise_ms(self) -> int:
        return self._monotonic.max_raise_ms

    def estimate(self, received_at_ms: int, pts: int | None) -> CaptureTime:
        t = self._estimate(received_at_ms, pts)
        return CaptureTime(*self._monotonic.apply(t.estimated_capture_time_ms, t.timestamp_uncertainty_ms))

    def _estimate(self, received_at_ms: int, pts: int | None) -> CaptureTime:
        m = self._model
        pts_ms = float(pts * self._tb_ms) if pts is not None and self._tb_ms is not None else None
        if self._frames is not None:
            t = self._frames.update(received_at_ms, pts_ms)
            if t is not None:
                return t
        if pts_ms is None:
            return CaptureTime(round(received_at_ms - m.latency_ms), m.uncertainty_no_pts_ms)
        offset = received_at_ms - pts_ms
        w = self._window
        while w and w[-1][1] >= offset:
            w.pop()
        w.append((received_at_ms, offset))
        while w[0][0] < received_at_ms - m.window_ms:
            w.popleft()
        return CaptureTime(round(pts_ms + w[0][1] - m.latency_ms), m.uncertainty_ms)
