"""같은 렌즈 4대 공유: 카메라별 단일 영상 추정을 강건하게 합쳐 공통 초기값을 만든다 (명세 4.3 1단계).

C110 4대는 같은 렌즈이므로 공통 초점·왜곡을 모든 카메라의 초기값으로 쓰고, 최종값은 번들 조정(3단계, -104)이
카메라별로 다듬는다.

- **합치기.** 초점은 초점을 정한 카메라(status OK)의 중앙값, 왜곡은 픽셀 단위 방사 계수 (b1, b2) 의 성분별
  중앙값이다. 픽셀 단위 왜곡은 초점과 무관하므로 초점을 못 정한 카메라(NO_FOCAL)의 왜곡도 넣는다.
- **편차(해상도 무관).** 카메라 모델과 공통 모델의 같은 광선 재투영 차이 p95 를 영상 반대각선 D 로 나눈 값
  (`discrepancy_rel`)을 편차로 본다. 해상도를 두 배로 해도 같은 렌즈·같은 산포면 같은 값이다. 대략 초점 상대
  차이의 2배다(SIM: 초점 1.5% 차이 ≈ 0.028).
- **이상치.** 편차가 한도 min(max(`agree_rel`, `median_factor` × 전체 중앙값), `hard_cap_rel`) 를 넘으면 이상치로
  표시하고 빼고 다시 합친다. `agree_rel` 0.08 은 초점 약 4% 차이에 해당해, 단일 영상 추정의 정상 산포(1~3%)는
  이상치가 아니다. 절대 상한(`hard_cap_rel`) 때문에 편차가 모두 클 때 한도가 같이 커지지 않는다.
- **합의 없음.** 이상치를 뺀 뒤 남는 카메라가 합칠 수 있는 카메라의 과반이 아니거나, 남은 카메라의 편차 중앙값이
  `agree_rel` 을 넘으면 다수가 맞는 값이 없는 것이다(예: 2대 대 2대, 1대 대 1대). 이때 공통 초점·왜곡을
  비우고 status `NO_CONSENSUS` 로 둔다(지어내지 않는다). 번들 조정 초기값도 만들지 않는다.
- 실패한 카메라(FAILED: 직선 부족, 펴지지 않음)는 합치기에 넣지 않는다.
  이상치는 렌즈·줌이 다르거나, 화면에 직선이 적거나, 영상이 잘못된 카메라다. 이상치 카메라도 공통값을 초기값으로
  받되 표시가 붙고 번들 조정 사전 분포를 넓게 둔다(`record.to_bundle_adjustment_init`).
- 영상 크기가 다른 카메라는 합치지 않는다(같은 렌즈라도 스트림 해상도가 다르면 픽셀 값이 다르다). 오류를 낸다.
- 초점을 정한 카메라가 하나도 없으면 공통 초점은 비운다(지어내지 않는다).
- **주점.** 합친 카메라 중 주점을 추정한 카메라(AnyCalib)가 있으면 그 주점의 성분별 중앙값을 공통 주점으로 쓰고
  `principal_point_assumed` 를 False 로 둔다. 모두 영상 중심 가정(고전 기준·GeoCalib)이면 영상 중심이고 True 다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from simai_ai.calibration.intrinsics.estimate import (
    METHOD_SHARED_LENS,
    STATUS_FAILED,
    STATUS_NO_CONSENSUS,
    STATUS_NO_FOCAL,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsUncertainty,
    half_diagonal,
    model_discrepancy_px,
    opencv_from_radial_pixel,
)

SHARED_LENS_VERSION = "0.1.0"
SHARED_CAMERA_ID = "shared-lens"


@dataclass(frozen=True)
class SharedLensConfig:
    # 편차(같은 광선 재투영 p95 / 반대각선) 기준. 이 값까지는 같은 렌즈의 정상 산포로 본다(초점 약 4%)
    agree_rel: float = 0.08
    median_factor: float = 3.0
    # 이상치 한도의 절대 상한(반대각선 비율). 편차가 모두 커도 한도가 이보다 커지지 않는다(초점 약 8%)
    hard_cap_rel: float = 0.15
    rounds: int = 2


@dataclass(frozen=True)
class CameraDeviation:
    """카메라 한 대의 추정과 공통값의 차이."""

    camera_id: str
    status: str
    focal_rel_dev: float | None  # (f_cam − f_common) / f_common
    k1_dev: float | None
    k2_dev: float | None
    discrepancy_p95_px: float | None
    outlier: bool
    used_in_common: bool
    reasons: tuple[str, ...] = ()
    # discrepancy_p95_px / 반대각선 (해상도 무관 편차)
    discrepancy_rel: float | None = None

    def to_record(self) -> dict:
        rec = {
            "camera_id": self.camera_id,
            "status": self.status,
            "outlier": self.outlier,
            "used_in_common": self.used_in_common,
        }
        for key in ("focal_rel_dev", "k1_dev", "k2_dev", "discrepancy_p95_px", "discrepancy_rel"):
            value = getattr(self, key)
            # 잴 수 없는 값(inf: 공통 모델로 광선을 만들 수 없는 픽셀이 있음)은 비운다. 사유는 reasons 에 있다
            if value is not None and math.isfinite(value):
                rec[key] = value
        if self.reasons:
            rec["reasons"] = list(self.reasons)
        return rec


@dataclass(frozen=True)
class SharedLensResult:
    common: IntrinsicsEstimate
    per_camera_initial: dict[str, IntrinsicsEstimate]
    deviations: tuple[CameraDeviation, ...]
    inputs: tuple[IntrinsicsEstimate, ...]
    consensus: bool = True

    @property
    def outlier_camera_ids(self) -> tuple[str, ...]:
        return tuple(d.camera_id for d in self.deviations if d.outlier)


def combine_shared_lens(
    estimates: Sequence[IntrinsicsEstimate], config: SharedLensConfig | None = None
) -> SharedLensResult:
    """카메라별 추정 → 공통 초기값, 카메라별 편차, 이상치 표시."""
    cfg = config or SharedLensConfig()
    if not estimates:
        raise ValueError("추정이 없다")
    ids = [e.camera_id for e in estimates]
    if len(set(ids)) != len(ids):
        raise ValueError(f"같은 camera_id 가 두 번 있다: {ids}")
    sizes = {tuple(e.image_size) for e in estimates}
    if len(sizes) != 1:
        raise ValueError(f"영상 크기가 다른 카메라는 합치지 않는다: {sorted(sizes)}")
    image_size = next(iter(sizes))

    failed = {e.camera_id for e in estimates if e.status == STATUS_FAILED or e.radial_pixel is None}
    usable = [e.camera_id for e in estimates if e.camera_id not in failed]
    excluded: set[str] = set(failed)
    common = _merge(estimates, excluded, image_size)
    deviations = _deviations(estimates, common, excluded, cfg)
    consensus = True
    for _ in range(cfg.rounds):
        new_excluded = failed | {d.camera_id for d in deviations if d.outlier}
        if new_excluded == excluded:
            break
        if len([c for c in usable if c not in new_excluded]) * 2 <= len(usable):
            consensus = False  # 이상치를 빼면 과반이 남지 않는다
            break
        excluded = new_excluded
        common = _merge(estimates, excluded, image_size)
        deviations = _deviations(estimates, common, excluded, cfg)
    used = [c for c in usable if c not in excluded]
    used_discs = [d.discrepancy_rel for d in deviations if d.camera_id in used and d.discrepancy_rel is not None]
    if usable and (len(used) * 2 <= len(usable) or (used_discs and float(np.median(used_discs)) > cfg.agree_rel)):
        consensus = False
    if not consensus:
        common, deviations = _no_consensus(common, deviations, usable, image_size)

    per_camera: dict[str, IntrinsicsEstimate] = {}
    if common.focal_px is not None:
        for e in estimates:
            per_camera[e.camera_id] = IntrinsicsEstimate(
                camera_id=e.camera_id,
                image_size=common.image_size,
                focal_px=common.focal_px,
                distortion_coeffs=common.distortion_coeffs,
                radial_pixel=common.radial_pixel,
                uncertainty=common.uncertainty,
                method=METHOD_SHARED_LENS,
                method_version=SHARED_LENS_VERSION,
                status=STATUS_OK,
                principal_point=common.principal_point,
                principal_point_assumed=common.principal_point_assumed,
            )
    return SharedLensResult(common, per_camera, tuple(deviations), tuple(estimates), consensus)


def _no_consensus(
    common: IntrinsicsEstimate,
    deviations: Sequence[CameraDeviation],
    usable: Sequence[str],
    image_size: tuple[int, int],
) -> tuple[IntrinsicsEstimate, list[CameraDeviation]]:
    """다수가 맞는 값이 없을 때: 공통값을 비우고 모든 카메라에 표시한다."""
    note = f"카메라 {len(usable)}대 중 과반이 맞는 초점·왜곡이 없어 공통값을 비웠다(합의 없음)"
    empty = IntrinsicsEstimate(
        camera_id=SHARED_CAMERA_ID,
        image_size=image_size,
        focal_px=None,
        distortion_coeffs=None,
        radial_pixel=None,
        uncertainty=IntrinsicsUncertainty(notes=(*common.uncertainty.notes, note)),
        method=METHOD_SHARED_LENS,
        method_version=SHARED_LENS_VERSION,
        status=STATUS_NO_CONSENSUS,
        principal_point_assumed=common.principal_point_assumed,
    )
    marked = [
        CameraDeviation(
            camera_id=d.camera_id,
            status=d.status,
            focal_rel_dev=d.focal_rel_dev,
            k1_dev=d.k1_dev,
            k2_dev=d.k2_dev,
            discrepancy_p95_px=d.discrepancy_p95_px,
            discrepancy_rel=d.discrepancy_rel,
            outlier=d.outlier,
            used_in_common=False,
            reasons=(*d.reasons, "다수 합의 없음"),
        )
        for d in deviations
    ]
    return empty, marked


def _merge(
    estimates: Sequence[IntrinsicsEstimate], excluded: set[str], image_size: tuple[int, int]
) -> IntrinsicsEstimate:
    use = [e for e in estimates if e.camera_id not in excluded]
    focal_src = [e for e in use if e.status == STATUS_OK and e.focal_px is not None]
    dist_src = [e for e in use if e.radial_pixel is not None and e.status in (STATUS_OK, STATUS_NO_FOCAL)]
    notes = [f"카메라 {len(focal_src)}대의 초점, {len(dist_src)}대의 왜곡을 합쳤다(중앙값)"]
    if excluded:
        notes.append(f"이상치로 뺀 카메라: {sorted(excluded)}")
    methods = sorted({e.method for e in use})
    notes.append(f"입력 방법: {methods}")
    b = None
    if dist_src:
        arr = np.array([e.radial_pixel for e in dist_src], dtype=float)
        b = (float(np.median(arr[:, 0])), float(np.median(arr[:, 1])))
    focal = float(np.median([e.focal_px for e in focal_src])) if focal_src else None
    focal_std = None
    if focal_src:
        n = len(focal_src)
        spread = float(np.std([e.focal_px for e in focal_src], ddof=1)) if n > 1 else 0.0
        indiv = [e.uncertainty.focal_std_px for e in focal_src if e.uncertainty.focal_std_px is not None]
        indiv_med = float(np.median(indiv)) if indiv else 0.0
        focal_std = max(spread, indiv_med) / math.sqrt(n) if (spread or indiv_med) else None
    straight = [e.uncertainty.straightness_rms_px for e in use if e.uncertainty.straightness_rms_px is not None]
    # 주점: 주점을 추정한 카메라(AnyCalib 등)가 있으면 그 중앙값, 없으면 영상 중심(가정)
    pp_src = [e.principal_point for e in focal_src if not e.principal_point_assumed and e.principal_point is not None]
    pp_assumed = not pp_src
    principal_point = None
    if pp_src:
        pp_arr = np.array(pp_src, dtype=float)
        principal_point = (float(np.median(pp_arr[:, 0])), float(np.median(pp_arr[:, 1])))
        notes.append(f"주점은 주점을 추정한 카메라 {len(pp_src)}대의 중앙값이다")
    if focal is None or b is None:
        status = STATUS_NO_FOCAL if b is not None else STATUS_FAILED
        notes.append("초점을 정한 카메라가 없어 공통 초점을 비웠다" if b is not None else "합칠 추정이 없다")
        return IntrinsicsEstimate(
            camera_id=SHARED_CAMERA_ID,
            image_size=image_size,
            focal_px=None,
            distortion_coeffs=None,
            radial_pixel=b,
            uncertainty=IntrinsicsUncertainty(notes=tuple(notes)),
            method=METHOD_SHARED_LENS,
            method_version=SHARED_LENS_VERSION,
            status=status,
        )
    coeffs = opencv_from_radial_pixel(b, focal, image_size)
    k_std = _coeff_std(dist_src, focal, image_size)
    return IntrinsicsEstimate(
        camera_id=SHARED_CAMERA_ID,
        image_size=image_size,
        focal_px=focal,
        distortion_coeffs=coeffs,
        radial_pixel=b,
        uncertainty=IntrinsicsUncertainty(
            focal_std_px=focal_std,
            k1_std=k_std[0],
            k2_std=k_std[1],
            straightness_rms_px=float(np.median(straight)) if straight else None,
            line_count=sum(e.uncertainty.line_count or 0 for e in use) or None,
            notes=tuple(notes),
        ),
        method=METHOD_SHARED_LENS,
        method_version=SHARED_LENS_VERSION,
        status=STATUS_OK,
        principal_point=principal_point,
        principal_point_assumed=pp_assumed,
    )


def _coeff_std(dist_src: Sequence[IntrinsicsEstimate], focal: float, image_size) -> tuple[float | None, float | None]:
    if len(dist_src) < 2:
        return (None, None)
    ks = np.array([opencv_from_radial_pixel(e.radial_pixel, focal, image_size)[:2] for e in dist_src])
    n = len(ks)
    return (float(np.std(ks[:, 0], ddof=1) / math.sqrt(n)), float(np.std(ks[:, 1], ddof=1) / math.sqrt(n)))


def _deviations(
    estimates: Sequence[IntrinsicsEstimate], common: IntrinsicsEstimate, excluded: set[str], cfg: SharedLensConfig
) -> list[CameraDeviation]:
    rows: list[tuple[IntrinsicsEstimate, float | None, float | None, float | None, float | None, list[str]]] = []
    for e in estimates:
        reasons: list[str] = []
        if e.status == STATUS_FAILED or e.radial_pixel is None:
            rows.append((e, None, None, None, None, ["추정 실패"]))
            continue
        if common.focal_px is None or common.radial_pixel is None:
            rows.append((e, None, None, None, None, ["공통 초점 없음"]))
            continue
        focal = e.focal_px if e.focal_px is not None else common.focal_px
        if e.focal_px is None:
            reasons.append("초점이 없어 공통 초점으로 왜곡만 비교")
        cam_coeffs = opencv_from_radial_pixel(e.radial_pixel, focal, e.image_size)
        cam = {
            "image_size": list(e.image_size),
            "K": [[focal, 0.0, e.principal_point[0]], [0.0, focal, e.principal_point[1]], [0.0, 0.0, 1.0]],
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": list(cam_coeffs)},
        }
        disc = model_discrepancy_px(common.to_contract_camera(), cam).p95_px
        focal_dev = (e.focal_px - common.focal_px) / common.focal_px if e.focal_px is not None else None
        rows.append(
            (
                e,
                focal_dev,
                cam_coeffs[0] - common.distortion_coeffs[0],
                cam_coeffs[1] - common.distortion_coeffs[1],
                disc,
                reasons,
            )
        )
    D = half_diagonal(common.image_size)
    rels = [r[4] / D for r in rows if r[4] is not None]
    limit = min(max(cfg.agree_rel, cfg.median_factor * float(np.median(rels))), cfg.hard_cap_rel) if rels else math.inf
    out: list[CameraDeviation] = []
    for e, fdev, k1d, k2d, disc, reasons in rows:
        rel = None if disc is None else disc / D
        outlier = rel is not None and rel > limit
        if outlier and not math.isfinite(rel):
            reasons = [*reasons, "공통 모델로 광선을 만들 수 없는 격자 픽셀이 있어 재투영 차이를 잴 수 없음(왜곡 식이 접힘)"]
        elif outlier:
            reasons = [
                *reasons,
                f"공통 모델과 재투영 차이 p95 {disc:.1f}px(반대각선의 {rel:.3f}) > 한도 {limit:.3f}",
            ]
        if e.status == STATUS_FAILED:
            outlier = True
        out.append(
            CameraDeviation(
                camera_id=e.camera_id,
                status=e.status,
                focal_rel_dev=fdev,
                k1_dev=k1d,
                k2_dev=k2d,
                discrepancy_p95_px=disc,
                discrepancy_rel=rel,
                outlier=outlier,
                used_in_common=e.camera_id not in excluded and e.status != STATUS_FAILED,
                reasons=tuple(reasons),
            )
        )
    return out
