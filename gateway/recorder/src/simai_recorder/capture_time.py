"""촬영 시각 추정 (명세 3.9 촬영 시각 계약, C110 기본값 RECEIVE_ESTIMATED).

C110 은 RTCP Sender Report 의 카메라 시각이 연속 보고 사이 약 ±400ms 흔들려 쓸 수 없다(3.3). 그래서 노트북 수신 시각에서
추정 지연을 뺀다. 수신 시각을 그대로 쓰면 Wi-Fi 도착 지터(S15P31A101-9 실측 p95 약 41ms)가 그대로 들어가므로,
카메라 PTS(RTP 시계)가 있으면 PTS 간격을 믿고 원점만 수신 시각으로 맞춘다.

    offset    = 최근 window_ms 안 프레임들의 min(received_at_ms − pts_ms)   # 가장 덜 늦게 온 프레임이 기준
    estimated = pts_ms + offset − latency_ms

창 안의 최솟값을 쓰므로 카메라 RTP 시계와 노트북 시계의 느린 표류(수십 ppm)를 따라간다. PTS 가 없으면
received_at_ms − latency_ms 를 쓰고 불확실성을 더 크게 둔다. 원본 received_at_ms·source_pts 를 함께 기록하므로
G0 LED 시험으로 latency_ms 가 정해지면 사이드카를 다시 계산할 수 있다.

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


class ReceiveTimeEstimator:
    """한 stream_epoch 의 촬영 시각 추정기. 재접속하면 새로 만든다(PTS 원점이 바뀐다)."""

    def __init__(self, model: CaptureTimeModel, pts_timebase: Fraction | None) -> None:
        self._model = model
        self._tb_ms = pts_timebase * 1000 if pts_timebase is not None else None
        # (received_at_ms, offset) 단조 증가 offset 덱: 앞이 창 안의 최솟값
        self._window: deque[tuple[int, float]] = deque()

    def estimate(self, received_at_ms: int, pts: int | None) -> CaptureTime:
        m = self._model
        if pts is None or self._tb_ms is None:
            return CaptureTime(round(received_at_ms - m.latency_ms), m.uncertainty_no_pts_ms)
        pts_ms = float(pts * self._tb_ms)
        offset = received_at_ms - pts_ms
        w = self._window
        while w and w[-1][1] >= offset:
            w.pop()
        w.append((received_at_ms, offset))
        while w[0][0] < received_at_ms - m.window_ms:
            w.popleft()
        return CaptureTime(round(pts_ms + w[0][1] - m.latency_ms), m.uncertainty_ms)
