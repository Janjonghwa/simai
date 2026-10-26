"""판정 결과를 CalibrationProvider 계약 문서(`contracts/providers/calibration.schema.json`)로 만든다.

G1_AUTO(자동 보정)와 대체 구현(DATASET_GT·SIM·MODEL_ESTIMATE)이 같은 형식을 낸다. 보정 알고리즘(HumanCalib·번들 조정)은 이 모듈 밖이며,
그 결과인 카메라별 K·왜곡·R·t·재투영 오차를 `CameraCalibration` 으로 받는다.

- 외부 파라미터는 OpenCV World → 카메라(x_cam = R · x_world + t), World 는 미터·오른손·+Z 상방이다.
- created_at 은 주입받는다(UTC int64 ms). 현재 시각을 읽지 않는다.
- content_hash 는 보정 내용(출처·카메라·스케일 근거·검증 결과)의 sha256 이다. 버전 ID·생성 시각·상태는 넣지 않아서
  같은 내용이면 같은 값이고, 운영 중 DRIFTED 표시로 바뀌지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simai_ai.calibration.criteria import CalibrationJudgement, ScaleBasis
from simai_ai.providers.base import CalibrationDoc
from simai_ai.providers.contract import CALIBRATION_SCHEMA_VERSION, WORLD_AXES_SOURCES, camera_center
from simai_ai.tracking.time_model import require_ms

# 이 패키지가 내는 문서의 계약 버전은 providers.contract.CALIBRATION_SCHEMA_VERSION 이다(2.1.0: World 축 출처
# world_axes_source·스케일 근거 MEASURED_CAMERA_HEIGHT 추가, 2.0.0: v1.3 에서 쓰지 않는 보정 출처·스케일 근거 값 삭제,
# 1.2.0: scale_basis 값 MEASURED_OBJECT·KNOWN_PERSON_HEIGHT 와
# scale_cross_check_basis·validation.max_pairwise_position_disagreement_m 추가).

# content_hash 에 넣지 않는 키
_UNHASHED_KEYS = frozenset({"content_hash", "calibration_version", "created_at", "state", "schema_version"})


@dataclass(frozen=True)
class CameraCalibration:
    """카메라 한 대의 보정 값 (계약 camera_calibration). 재투영 오차를 모르면 None 으로 둔다."""

    camera_id: str
    image_size: tuple[int, int]
    K: Sequence[Sequence[float]]
    R: Sequence[Sequence[float]]
    t: Sequence[float]
    distortion_model: str = "NONE"
    distortion_coeffs: Sequence[float] = ()
    reprojection_median_px: float | None = None
    reprojection_p95_px: float | None = None

    def to_contract(self) -> dict[str, Any]:
        cam: dict[str, Any] = {
            "camera_id": self.camera_id,
            "image_size": [int(v) for v in self.image_size],
            "K": [[float(v) for v in row] for row in self.K],
            "distortion": {"model": self.distortion_model, "coeffs": [float(v) for v in self.distortion_coeffs]},
            "R": [[float(v) for v in row] for row in self.R],
            "t": [float(v) for v in self.t],
        }
        if len(self.R) == 3 and all(len(row) == 3 for row in self.R) and len(self.t) == 3:
            # 크기가 틀리면 높이를 계산하지 않고 그대로 옮긴다. 저장소의 형식 검사가 거절한다.
            cam["camera_height_m"] = round(camera_center(self.R, self.t)[2], 4)
        rep = {}
        if self.reprojection_median_px is not None:
            rep["median"] = float(self.reprojection_median_px)
        if self.reprojection_p95_px is not None:
            rep["p95"] = float(self.reprojection_p95_px)
        if rep:
            cam["reprojection_error_px"] = rep
        return cam


def build_calibration_doc(
    *,
    calibration_version: str,
    provider_source: str,
    provider_version: str,
    created_at: int,
    cameras: Sequence[CameraCalibration | Mapping[str, Any]],
    judgement: CalibrationJudgement,
    fallback_scale_basis: ScaleBasis | str,
    site_id: str | None = None,
    world_axes_source: str | None = None,
) -> CalibrationDoc:
    """판정 결과로 보정 문서 한 버전을 만든다.

    state 는 판정 결과(CALIBRATED/PROVISIONAL)다. scale_basis 는 판정이 고른 근거, 근거가 없으면 fallback_scale_basis
    (예: G1 초기 추정은 MODEL_ESTIMATE) 다. 주 근거와 일치한 다른 근거는 scale_cross_check_basis 에
    우선순위 순으로 적는다(명세 4.3 사용한 근거 기록). validation 은 판정이 아는 값만 넣는다.
    world_axes_source 는 World 축을 정한 근거(WALLS·WALL_AND_FLOOR·MANUAL_FLOOR_POINTS)다. 모르면 None 으로 두어 싣지 않는다(지어내지 않음).
    """
    require_ms("created_at", created_at)
    if not calibration_version:
        raise ValueError("calibration_version 이 비었다")
    frame: dict[str, Any] = {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}
    if site_id is not None:
        frame["site_id"] = site_id
    doc: CalibrationDoc = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_version": calibration_version,
        "provider_source": provider_source,
        "provider_version": provider_version,
        "state": str(judgement.state),
        "created_at": created_at,
        "coordinate_frame": frame,
        "scale_basis": str(ScaleBasis(judgement.scale.recorded_basis or fallback_scale_basis)),
    }
    if judgement.scale.cross_checks:
        doc["scale_cross_check_basis"] = [str(b) for b in judgement.scale.cross_checks]
    if world_axes_source is not None:
        if world_axes_source not in WORLD_AXES_SOURCES:
            raise ValueError(f"world_axes_source 는 {WORLD_AXES_SOURCES} 중 하나다: {world_axes_source!r}")
        doc["world_axes_source"] = world_axes_source
    doc["extrinsic_convention"] = "OPENCV_WORLD_TO_CAMERA"
    doc["cameras"] = [c.to_contract() if isinstance(c, CameraCalibration) else _copy(c) for c in cameras]
    if judgement.validation:
        doc["validation"] = dict(judgement.validation)
    doc["content_hash"] = content_hash(doc)
    return doc


def content_hash(doc: Mapping[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k not in _UNHASHED_KEYS}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))
