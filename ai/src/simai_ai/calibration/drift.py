"""운영 중 보정 틀어짐(DRIFTED) 감지 (명세 4.4 DRIFTED, 4.5 운영 중 재투영 오차 감시, 6.6).

DRIFTED 는 운영 중 재투영 오차 증가 또는 스트림 해상도·화각 변경이다. 틀어졌다는 근거가 있을 때만 알린다.
재투영 오차를 모르는 카메라는 틀어졌다고 보지 않는다(근거 없음). NaN·무한대·음수 관측값도 모르는 값으로 본다.
보정에 없는 카메라는 이 보정과 무관해 보지 않는다.

재투영 오차 "증가"의 기준은 카메라마다 max(설정 상한, 보정 당시 값)이다. 상한 초기값은 CALIBRATED 기준과 같다.
PROVISIONAL 보정처럼 처음부터 상한을 넘던 카메라는 보정 당시 값보다 커질 때 틀어짐으로 본다.

운영 중 사람 keypoint 로 재투영 오차를 계속 재고 지속 조건으로 확정하는 감시는 `monitor` 가 한다(스트림 근거는 이
모듈의 detect_drift 를 그대로 쓴다).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simai_ai.providers.contract import is_finite_number


class DriftReason(StrEnum):
    RESOLUTION_CHANGED = "RESOLUTION_CHANGED"
    FIELD_OF_VIEW_CHANGED = "FIELD_OF_VIEW_CHANGED"
    REPROJECTION_INCREASED = "REPROJECTION_INCREASED"


@dataclass(frozen=True)
class CameraObservation:
    """운영 중 카메라 한 대의 관측. 모르는 값은 None.

    field_of_view_changed: 줌·렌즈 교체 등 화각 변경을 상류(I0 스트림 정보 등)가 알린 경우 True.
    """

    camera_id: str
    image_size: tuple[int, int] | None = None
    reprojection_median_px: float | None = None
    reprojection_p95_px: float | None = None
    field_of_view_changed: bool = False


@dataclass(frozen=True)
class DriftConfig:
    max_reprojection_median_px: float = 2.0
    max_reprojection_p95_px: float = 5.0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not is_finite_number(value) or value < 0:
                raise ValueError(f"DriftConfig.{name} 는 유한한 0 이상의 수여야 한다: {value!r}")


@dataclass(frozen=True)
class DriftFinding:
    camera_id: str
    reason: DriftReason
    message: str


def detect_drift(
    calibration: Mapping[str, Any],
    observations: Iterable[CameraObservation],
    config: DriftConfig = DriftConfig(),
) -> tuple[DriftFinding, ...]:
    """보정 문서 하나와 운영 관측을 비교해 틀어짐 근거를 모두 돌려준다. 빈 결과면 틀어짐 없음."""
    cameras = {cam["camera_id"]: cam for cam in calibration.get("cameras", [])}
    out: list[DriftFinding] = []
    for obs in observations:
        cam = cameras.get(obs.camera_id)
        if cam is None:
            continue
        if obs.image_size is not None and list(obs.image_size) != list(cam["image_size"]):
            out.append(
                DriftFinding(
                    obs.camera_id,
                    DriftReason.RESOLUTION_CHANGED,
                    f"영상 크기 {list(obs.image_size)} 가 보정 당시 {cam['image_size']} 와 다르다",
                )
            )
        if obs.field_of_view_changed:
            out.append(DriftFinding(obs.camera_id, DriftReason.FIELD_OF_VIEW_CHANGED, "화각 변경 통지"))
        recorded = cam.get("reprojection_error_px") or {}
        for key, value, limit in (
            ("median", obs.reprojection_median_px, config.max_reprojection_median_px),
            ("p95", obs.reprojection_p95_px, config.max_reprojection_p95_px),
        ):
            if not is_finite_number(value) or value < 0:
                continue
            threshold = max(limit, recorded.get(key) or 0.0)
            if value > threshold:
                out.append(
                    DriftFinding(
                        obs.camera_id,
                        DriftReason.REPROJECTION_INCREASED,
                        f"운영 중 재투영 오차 {key} {value}px > {threshold}px",
                    )
                )
    return tuple(out)
