"""내부 파라미터 추정 기록과 번들 조정(-104) 초기값 넘김.

- 기록 형식(`simai.intrinsics_estimate`)은 contracts/ 의 계약이 아니라 이 모듈의 형식이다(계약화는 MR 에서 제안).
  카메라 내부 파라미터 부분(`camera`)만 계약 `camera_calibration` 의 형식(camera_id, image_size, K, distortion)을
  그대로 쓴다. 외부 파라미터(R, t)는 이 단계에서 모르므로 넣지 않는다.
- 입력 출처(`input_provenance`, 명세 20.4 규칙 2·3)를 남긴다. 시뮬레이터 영상(frames SIM)이면 is_upper_bound 가
  true 이고 그 결과 지표는 "입력이 완벽할 때의 상한"이다. 제품 성능으로 쓰지 않는다.
- 이 단계 결과는 보정 버전이 아니다. 보정 문서(provider_source G1_AUTO 등)는 MapAnything 초기 추정·번들 조정·
  검증을 거쳐 `calibration.document` 가 만든다. 그래서 provider_source 를 적지 않는다.
- created_at 은 주입받는다(UTC int64 ms).
- 0.2.0: 실행 도구(`cli`)가 쓰는 선택 절 `cameras`(카메라별 입력 설명·수집 프레임 수·시간 범위·백엔드 경로·실패 사유)와
  `run`(수집 설정·백엔드 순서·사용 불가 백엔드)을 더했다. 영상·이미지는 기록에 넣지 않는다.
- 기록 파일에서 번들 조정 초기값을 다시 만드는 것은 `bundle_init_from_record` 다(외부 파라미터 실행 도구, -214).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from simai_ai.calibration.intrinsics.estimate import (
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsUncertainty,
)
from simai_ai.calibration.intrinsics.shared import CameraDeviation, SharedLensResult
from simai_ai.providers.base import FramesProvenance
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.tracking.time_model import require_ms

INTRINSICS_RECORD_FORMAT = "simai.intrinsics_estimate"
INTRINSICS_RECORD_VERSION = "0.2.0"

# 번들 조정 사전 분포 폭의 기본값: 추정 불확실성이 없을 때 쓰는 상대 초점 표준편차와 왜곡 표준편차
DEFAULT_FOCAL_PRIOR_REL = 0.05
DEFAULT_K_PRIOR = (0.05, 0.03)
# 이상치 카메라는 사전 분포를 이 배수로 넓힌다
OUTLIER_PRIOR_SCALE = 4.0


def estimate_to_record(estimate: IntrinsicsEstimate) -> dict[str, Any]:
    """추정 한 건 → 기록 조각 (input_provenance·created_at 없이)."""
    rec: dict[str, Any] = {
        "camera_id": estimate.camera_id,
        "status": estimate.status,
        "method": estimate.method,
        "method_version": estimate.method_version,
        "image_size": [int(estimate.image_size[0]), int(estimate.image_size[1])],
        "principal_point_assumed": estimate.principal_point_assumed,
        "uncertainty": estimate.uncertainty.to_record(),
    }
    if estimate.focal_px is not None and estimate.distortion_coeffs is not None:
        rec["camera"] = estimate.to_contract_camera()
    if estimate.radial_pixel is not None:
        rec["radial_pixel"] = [float(v) for v in estimate.radial_pixel]
    if estimate.model is not None:
        rec["model"] = estimate.model.to_record()
    return rec


def build_intrinsics_record(
    *,
    created_at: int,
    frames: FramesProvenance,
    estimates: list[IntrinsicsEstimate],
    shared: SharedLensResult | None = None,
    cameras: Sequence[Mapping[str, Any]] | None = None,
    run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """카메라별 추정(과 같은 렌즈 공유 결과)의 기록 한 건. cameras·run 은 실행 도구가 넣는 선택 절이다."""
    require_ms("created_at", created_at)
    doc: dict[str, Any] = {
        "format": INTRINSICS_RECORD_FORMAT,
        "format_version": INTRINSICS_RECORD_VERSION,
        "created_at": created_at,
        "input_provenance": build_input_provenance(frames),
        "estimates": [estimate_to_record(e) for e in estimates],
    }
    if shared is not None:
        doc["shared_lens"] = {
            "common": estimate_to_record(shared.common),
            "deviations": [d.to_record() for d in shared.deviations],
            "outlier_camera_ids": list(shared.outlier_camera_ids),
            "consensus": shared.consensus,
        }
    if cameras is not None:
        doc["cameras"] = [dict(c) for c in cameras]
    if run is not None:
        doc["run"] = dict(run)
    return doc


@dataclass(frozen=True)
class CameraIntrinsicsInit:
    """번들 조정에 넘기는 카메라 한 대의 내부 파라미터 초기값과 사전 분포 폭(1σ)."""

    camera_id: str
    camera: Mapping[str, Any]  # 계약 camera_calibration 의 내부 파라미터 부분
    focal_prior_std_px: float
    k_prior_std: tuple[float, float]
    principal_point_fixed: bool
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundleAdjustmentInit:
    """번들 조정(-104, HumanCalib 구조) 입력의 내부 파라미터 부분.

    -104 는 이것을 초기값으로 받아 카메라별 내부 파라미터·왜곡 계수를 걷기 keypoint 와 실측 정물 제약으로 다듬는다
    (명세 4.3 3단계). 이 객체는 보정 결과가 아니다(외부 파라미터·스케일 없음).
    """

    cameras: tuple[CameraIntrinsicsInit, ...]
    source_record: Mapping[str, Any]

    def camera(self, camera_id: str) -> CameraIntrinsicsInit:
        for c in self.cameras:
            if c.camera_id == camera_id:
                return c
        raise KeyError(camera_id)


def to_bundle_adjustment_init(shared: SharedLensResult, source_record: Mapping[str, Any]) -> BundleAdjustmentInit:
    """같은 렌즈 공유 결과 → 번들 조정 초기값. 공통 초점이 없으면 ValueError(초기값을 지어내지 않는다).

    source_record 가 실행 도구 기록이면 직선성 확인을 하지 못한(UNVERIFIED) 카메라에 `STRAIGHTNESS_UNVERIFIED` 표시를
    붙인다. 지금 번들 조정 풀이(`calibration.bundle.solver`)는 `principal_point_fixed` 와 무관하게 주점을 초기값으로
    고정한다(초점·k1·k2 만 다듬는다).
    """
    common = shared.common
    if common.focal_px is None or common.distortion_coeffs is None:
        raise ValueError(f"공통 초점을 정하지 못해 번들 조정 초기값을 만들 수 없다 ({common.status})")
    focal_std = common.uncertainty.focal_std_px or DEFAULT_FOCAL_PRIOR_REL * common.focal_px
    # 추정 표준편차는 같은 렌즈의 개체 차이·주점 가정을 담지 못하므로 기본 폭보다 좁게 두지 않는다
    focal_std = max(focal_std, 0.01 * common.focal_px)
    k_std = (
        max(common.uncertainty.k1_std or 0.0, DEFAULT_K_PRIOR[0] / 5),
        max(common.uncertainty.k2_std or 0.0, DEFAULT_K_PRIOR[1] / 5),
    )
    deviations = {d.camera_id: d for d in shared.deviations}
    # 실행 도구 기록(cameras[].backend.straightness_check)에서 학습 결과를 직선성으로 확인하지 못한 카메라
    unverified = {
        c.get("camera_id")
        for c in source_record.get("cameras", []) or []
        if ((c.get("backend") or {}).get("straightness_check") or {}).get("status") == "UNVERIFIED"
    }
    cams: list[CameraIntrinsicsInit] = []
    for camera_id, est in shared.per_camera_initial.items():
        dev = deviations.get(camera_id)
        flags: list[str] = []
        scale = 1.0
        if dev is not None and dev.outlier:
            flags.append("OUTLIER_CAMERA")
            scale = OUTLIER_PRIOR_SCALE
        if dev is not None and dev.status != "OK":
            flags.append(f"OWN_ESTIMATE_{dev.status}")
        if common.principal_point_assumed:
            flags.append("PRINCIPAL_POINT_ASSUMED_CENTER")
        if camera_id in unverified:
            flags.append("STRAIGHTNESS_UNVERIFIED")
        cams.append(
            CameraIntrinsicsInit(
                camera_id=camera_id,
                camera=est.to_contract_camera(),
                focal_prior_std_px=focal_std * scale,
                k_prior_std=(k_std[0] * scale, k_std[1] * scale),
                principal_point_fixed=common.principal_point_assumed,
                flags=tuple(flags),
            )
        )
    return BundleAdjustmentInit(tuple(cams), dict(source_record))


def bundle_init_from_record(record: Mapping[str, Any]) -> BundleAdjustmentInit:
    """기록 JSON(`simai.intrinsics_estimate`)의 같은 렌즈 공통값 → 번들 조정 초기값 (`to_bundle_adjustment_init` 와 같은 규칙).

    공통값이 없거나(합의 없음·초점 없음) 형식이 다르면 ValueError 다(초기값을 지어내지 않는다). 카메라 목록은 기록의
    estimates 이고, 편차(이상치·자체 추정 상태)와 실행 도구 기록의 직선성 확인 결과도 그대로 표시로 옮긴다.
    """
    if record.get("format") != INTRINSICS_RECORD_FORMAT:
        raise ValueError(f"내부 파라미터 기록 형식이 아니다: {record.get('format')!r}")
    shared = record.get("shared_lens")
    if not shared or not shared.get("consensus"):
        raise ValueError("같은 렌즈 공통값이 없다(shared_lens 없음 또는 합의 없음). 번들 조정 초기값을 만들 수 없다")
    common_rec = shared["common"]
    if common_rec.get("status") != STATUS_OK or "camera" not in common_rec:
        raise ValueError(f"같은 렌즈 공통 초점·왜곡이 없다 ({common_rec.get('status')})")
    camera = common_rec["camera"]
    K = camera["K"]
    unc = common_rec.get("uncertainty") or {}
    common = IntrinsicsEstimate(
        camera_id=common_rec["camera_id"],
        image_size=(int(camera["image_size"][0]), int(camera["image_size"][1])),
        focal_px=float(K[0][0]),
        distortion_coeffs=tuple(float(c) for c in camera["distortion"]["coeffs"]),
        radial_pixel=tuple(common_rec["radial_pixel"]) if common_rec.get("radial_pixel") else None,
        uncertainty=IntrinsicsUncertainty(
            focal_std_px=unc.get("focal_std_px"), k1_std=unc.get("k1_std"), k2_std=unc.get("k2_std")
        ),
        method=common_rec["method"],
        method_version=common_rec["method_version"],
        status=STATUS_OK,
        principal_point=(float(K[0][2]), float(K[1][2])),
        principal_point_assumed=bool(common_rec.get("principal_point_assumed", True)),
    )
    if camera["distortion"]["model"] != "OPENCV_PINHOLE":
        raise ValueError(f"공통값 왜곡 모델이 OPENCV_PINHOLE 이 아니다: {camera['distortion']['model']}")
    deviations = tuple(
        CameraDeviation(
            camera_id=d["camera_id"],
            status=d["status"],
            focal_rel_dev=d.get("focal_rel_dev"),
            k1_dev=d.get("k1_dev"),
            k2_dev=d.get("k2_dev"),
            discrepancy_p95_px=d.get("discrepancy_p95_px"),
            outlier=bool(d.get("outlier")),
            used_in_common=bool(d.get("used_in_common")),
            discrepancy_rel=d.get("discrepancy_rel"),
        )
        for d in shared.get("deviations", [])
    )
    camera_ids = [e["camera_id"] for e in record.get("estimates", [])]
    if not camera_ids:
        raise ValueError("기록에 카메라별 추정(estimates)이 없다")
    per_camera = {cid: replace(common, camera_id=cid) for cid in camera_ids}
    return to_bundle_adjustment_init(SharedLensResult(common, per_camera, deviations, (), True), record)
