"""카메라 간 시간 보간 (명세 3.9 촬영 시각 계약, 5.2 계산 순서 1단계, 6.1, 12.7 역순·지연 도착).

카메라마다 노출 순간이 달라(25 fps 에서 40 ms 간격) 삼각측량 전에 각 카메라의 2D keypoint 를 공통 시각 t 로
선형 보간한다. 규칙:

- 촬영 시각 불확실성(`timestamp_uncertainty_ms`)이 기준(초기값 25 ms)을 넘거나 모르는 프레임은 쓰지 않는다.
  그 프레임이 t 를 감싸는 프레임이면 그 카메라는 시각 t 의 계산에서 빠진다.
- t 앞뒤로 가장 가까운 두 프레임(같은 카메라, 같은 카메라 내 트랙)을 잇는다. 둘 중 하나가 보간 창(초기값 200 ms)
  밖이면 그 프레임은 버리고 사유를 남긴다. 외삽은 하지 않는다(t 뒤 프레임이 아직 없으면 그 카메라는 빠진다).
- 관절은 두 프레임 모두에 있을 때만 보간한다. 한쪽이라도 없으면 null 이다(지어내지 않는다).
- 늦게 도착한 프레임은 창 안이면 정렬해 쓰고, 창을 넘기면 버리고 기록한다(`KeypointBuffer`).

표준 라이브러리만 쓴다. 시각은 UTC int64 ms 다. 현재 시각을 직접 읽지 않는다.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# 명세 3.9: 이 값을 넘는 카메라는 그 시각의 삼각측량에서 뺀다 (초기값)
DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS = 25.0
# 명세 12.7: 시간 보간 창 (초기값)
DEFAULT_INTERPOLATION_WINDOW_MS = 200

# [u, v, confidence] 또는 None(검출되지 않음). joint_layout 순서.
Keypoint = tuple[float, float, float] | None


class DropReason(StrEnum):
    """프레임을 보간에 쓰지 않은 사유."""

    TIMESTAMP_UNCERTAIN = "TIMESTAMP_UNCERTAIN"
    """촬영 시각 불확실성이 기준을 넘거나 값이 없다(NaN·음수 포함)"""
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    """t 를 감싸는 프레임이 보간 창 밖이다"""
    NO_FRAME_AFTER = "NO_FRAME_AFTER"
    """t 이후 프레임이 없다 (외삽하지 않음)"""
    NO_FRAME_BEFORE = "NO_FRAME_BEFORE"
    """t 이전 프레임이 없다"""
    LATE_ARRIVAL = "LATE_ARRIVAL"
    """이미 받은 최신 촬영 시각보다 보간 창 이상 늦게 도착했다"""


@dataclass(frozen=True)
class InterpolationConfig:
    max_timestamp_uncertainty_ms: float = DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS
    window_ms: int = DEFAULT_INTERPOLATION_WINDOW_MS

    def __post_init__(self) -> None:
        if not self.max_timestamp_uncertainty_ms >= 0:
            raise ValueError(f"max_timestamp_uncertainty_ms 는 0 이상이어야 한다: {self.max_timestamp_uncertainty_ms}")
        if not self.window_ms > 0:
            raise ValueError(f"window_ms 는 양수여야 한다: {self.window_ms}")


@dataclass(frozen=True)
class KeypointSample:
    """카메라 한 대, 카메라 내 트랙 하나의 한 프레임 2D keypoint (명세 3.9 촬영 시각 계약 필드).

    frame_ref 는 `world_tracks.schema.json` camera_observation.frame_ref 와 같다(출처 보존용).
    """

    camera_id: str
    estimated_capture_time: int
    timestamp_source: str
    timestamp_uncertainty_ms: float | None
    keypoints: tuple[Keypoint, ...]
    frame_ref: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_ms("estimated_capture_time", self.estimated_capture_time)


@dataclass(frozen=True)
class FrameDrop:
    """보간에 쓰지 않은 프레임 기록. estimated_capture_time 이 None 이면 해당 프레임이 아예 없다는 뜻이다."""

    camera_id: str
    t: int
    reason: DropReason
    estimated_capture_time: int | None = None
    frame_ref: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class InterpolatedView:
    """공통 시각 t 로 보간한 카메라 한 대의 2D keypoint.

    timestamp_uncertainty_ms 는 보간에 쓴 프레임들 불확실성의 최댓값이다.
    source_times 는 보간에 쓴 프레임의 촬영 시각(1개면 t 와 같은 시각의 프레임)이다.
    """

    camera_id: str
    t: int
    keypoints: tuple[Keypoint, ...]
    timestamp_source: str
    timestamp_uncertainty_ms: float
    source_times: tuple[int, ...]


def interpolate_camera(
    samples: Sequence[KeypointSample], t: int, config: InterpolationConfig = InterpolationConfig()
) -> tuple[InterpolatedView | None, list[FrameDrop]]:
    """카메라 한 대(한 트랙)의 프레임들을 공통 시각 t 로 보간한다. 쓸 수 없으면 (None, 사유)."""
    _require_ms("t", t)
    if not samples:
        return None, []
    camera_ids = {s.camera_id for s in samples}
    if len(camera_ids) != 1:
        raise ValueError(f"한 카메라의 프레임만 넣는다: {sorted(camera_ids)}")
    camera_id = samples[0].camera_id
    ordered = sorted(samples, key=lambda s: s.estimated_capture_time)
    times = [s.estimated_capture_time for s in ordered]

    # t 를 감싸는 두 프레임: before = t 이하 중 가장 늦은 것, after = t 초과 중 가장 이른 것
    k = bisect.bisect_right(times, t)
    before = ordered[k - 1] if k > 0 else None
    after = ordered[k] if k < len(ordered) else None
    if before is not None and before.estimated_capture_time == t:
        after = None  # t 와 같은 시각의 프레임이 있으면 그것만 쓴다

    drops: list[FrameDrop] = []
    usable: list[KeypointSample] = []
    for sample in (before, after):
        if sample is None:
            continue
        reason = _sample_problem(sample, t, config)
        if reason is None:
            usable.append(sample)
        else:
            drops.append(_drop(sample, t, reason))
    if drops:
        return None, drops
    if before is None:
        return None, [FrameDrop(camera_id, t, DropReason.NO_FRAME_BEFORE)]
    if before.estimated_capture_time == t:
        return _view(camera_id, t, [before], before.keypoints), []
    if after is None:
        return None, [FrameDrop(camera_id, t, DropReason.NO_FRAME_AFTER)]

    t0, t1 = before.estimated_capture_time, after.estimated_capture_time
    alpha = (t - t0) / (t1 - t0)
    keypoints = tuple(_lerp(a, b, alpha) for a, b in zip(before.keypoints, after.keypoints, strict=True))
    return _view(camera_id, t, usable, keypoints), []


def interpolate_views(
    samples_by_camera: Mapping[str, Sequence[KeypointSample]],
    t: int,
    config: InterpolationConfig = InterpolationConfig(),
) -> tuple[dict[str, InterpolatedView], list[FrameDrop]]:
    """사람 한 명의 카메라별 프레임을 공통 시각 t 로 보간한다. 빠진 카메라는 사유와 함께 돌려준다."""
    views: dict[str, InterpolatedView] = {}
    drops: list[FrameDrop] = []
    for camera_id, samples in samples_by_camera.items():
        if any(s.camera_id != camera_id for s in samples):
            raise ValueError(f"{camera_id}: 다른 카메라의 프레임이 섞여 있다")
        view, camera_drops = interpolate_camera(samples, t, config)
        drops += camera_drops
        if view is not None:
            views[camera_id] = view
    return views, drops


class KeypointBuffer:
    """카메라 한 대(한 트랙)의 최근 프레임 버퍼 (명세 12.7 역순·지연 도착).

    촬영 시각 순서로 정렬해 두고, 받은 최신 촬영 시각보다 보간 창 이상 오래된 프레임은 받지 않고 기록만 한다.
    """

    def __init__(self, camera_id: str, config: InterpolationConfig = InterpolationConfig()) -> None:
        self.camera_id = camera_id
        self.config = config
        self._samples: list[KeypointSample] = []
        self._latest: int | None = None

    @property
    def samples(self) -> tuple[KeypointSample, ...]:
        return tuple(self._samples)

    def add(self, sample: KeypointSample) -> FrameDrop | None:
        """프레임을 넣는다. 창을 넘겨 늦게 온 프레임이면 넣지 않고 기록을 돌려준다."""
        if sample.camera_id != self.camera_id:
            raise ValueError(f"{self.camera_id} 버퍼에 {sample.camera_id} 프레임을 넣었다")
        ts = sample.estimated_capture_time
        if self._latest is not None and self._latest - ts > self.config.window_ms:
            return _drop(sample, self._latest, DropReason.LATE_ARRIVAL)
        bisect.insort(self._samples, sample, key=lambda s: s.estimated_capture_time)
        self._latest = ts if self._latest is None else max(self._latest, ts)
        return None

    def prune(self, t: int) -> None:
        """t 를 보간하는 데 필요 없는 오래된 프레임을 지운다(t 이하 중 가장 늦은 프레임은 남긴다)."""
        _require_ms("t", t)
        k = bisect.bisect_right([s.estimated_capture_time for s in self._samples], t)
        del self._samples[: max(0, k - 1)]

    def interpolate(self, t: int) -> tuple[InterpolatedView | None, list[FrameDrop]]:
        return interpolate_camera(self._samples, t, self.config)


def _require_ms(name: str, value: int) -> int:
    """UTC int64 ms 시각인지 확인한다 (tracking.time_model.require_ms 와 같은 규칙).

    tracking 이 geometry 를 쓰는 방향이라 거꾸로 import 하지 않고 같은 검사를 둔다.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}: 시각은 int ms 여야 한다 (받은 값 {value!r})")
    if value < 0:
        raise ValueError(f"{name}: 시각은 0 이상이어야 한다 (받은 값 {value})")
    return value


def _sample_problem(sample: KeypointSample, t: int, config: InterpolationConfig) -> DropReason | None:
    unc = sample.timestamp_uncertainty_ms
    # 값이 없거나 NaN·음수(계약 위반)인 불확실성도 모르는 것으로 보고 뺀다
    if unc is None or not 0.0 <= unc <= config.max_timestamp_uncertainty_ms:
        return DropReason.TIMESTAMP_UNCERTAIN
    if abs(sample.estimated_capture_time - t) > config.window_ms:
        return DropReason.OUTSIDE_WINDOW
    return None


def _drop(sample: KeypointSample, t: int, reason: DropReason) -> FrameDrop:
    return FrameDrop(
        camera_id=sample.camera_id,
        t=t,
        reason=reason,
        estimated_capture_time=sample.estimated_capture_time,
        frame_ref=sample.frame_ref,
    )


def _view(camera_id: str, t: int, used: Iterable[KeypointSample], keypoints: tuple[Keypoint, ...]) -> InterpolatedView:
    used = list(used)
    sources = {s.timestamp_source for s in used}
    return InterpolatedView(
        camera_id=camera_id,
        t=t,
        keypoints=keypoints,
        # 두 프레임의 출처가 다르면(스트림 재연결 직후 등) 더 약한 근거인 수신 추정으로 적는다.
        timestamp_source=sources.pop() if len(sources) == 1 else "RECEIVE_ESTIMATED",
        timestamp_uncertainty_ms=max(float(s.timestamp_uncertainty_ms or 0.0) for s in used),
        source_times=tuple(s.estimated_capture_time for s in used),
    )


def _lerp(a: Keypoint, b: Keypoint, alpha: float) -> Keypoint:
    if a is None or b is None:
        return None
    return (
        a[0] + (b[0] - a[0]) * alpha,
        a[1] + (b[1] - a[1]) * alpha,
        min(a[2], b[2]),
    )
