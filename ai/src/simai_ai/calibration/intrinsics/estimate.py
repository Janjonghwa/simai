"""무마커 내부 파라미터 추정의 공통 형식: 추정기 인터페이스, 추정 결과, 카메라 모델 계산.

- 결과의 K·왜곡은 보정 계약(`contracts/providers/calibration.schema.json` 의 `camera_calibration`)의 형식을 따른다.
  왜곡 모델은 OPENCV_PINHOLE 방사 2계수만 추정한다: coeffs = [k1, k2, 0, 0, 0] (p1·p2·k3 은 0 으로 둔다).
  OpenCV 식은 x_d = x_u (1 + k1 r² + k2 r⁴), r² = x_u² + y_u² (정규화 좌표, 왜곡 없는 쪽이 기준)이다.
- 주점은 영상 중심 (W/2, H/2) 으로 가정한다(단일 영상으로는 잘 정해지지 않는다). 시뮬레이터 카메라와 같은 규약이다.
  `principal_point_assumed` 로 표시한다. 정밀화(번들 조정, -104)가 카메라별로 다듬는다.
- 모르면 비운다: 초점을 정하지 못하면 `focal_px` 는 None 이고 계약 형식으로 내지 않는다(지어내지 않는다).
  초점을 몰라도 왜곡은 픽셀 단위로 알 수 있으므로 `radial_pixel` 에 남긴다. 같은 렌즈 공유 단계가 다른 카메라의
  초점으로 OpenCV 계수로 바꾼다.

픽셀 단위 방사 왜곡(`radial_pixel`)은 영상 중심 기준, 반대각선 D = hypot(W/2, H/2) 로 나눈 반지름 ρ 에서
ρ_d = ρ_u (1 + b1 ρ_u² + b2 ρ_u⁴) 이다. OpenCV 계수와는 k1 = b1 (f/D)², k2 = b2 (f/D)⁴ 로 정확히 같다.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from simai_ai.geometry import distortion

# 추정 방법 (이 모듈의 형식. 계약 값이 아니다)
METHOD_PLUMB_LINE_VP = "PLUMB_LINE_VP"
METHOD_GEOCALIB = "GEOCALIB"
METHOD_ANYCALIB = "ANYCALIB"
METHOD_SHARED_LENS = "SHARED_LENS"

STATUS_OK = "OK"
STATUS_NO_FOCAL = "NO_FOCAL"  # 왜곡은 구했지만 초점을 정하지 못함(소실점 부족 등)
STATUS_FAILED = "FAILED"  # 직선이 부족해 아무것도 정하지 못함
STATUS_NO_CONSENSUS = "NO_CONSENSUS"  # 같은 렌즈 공유: 카메라 다수가 맞는 값이 없어 공통값을 비움


class IntrinsicsEstimationError(ValueError):
    """입력이 잘못돼 추정을 시작할 수 없다(영상 형식, 크기 등)."""


class WeightsMismatchError(ValueError):
    """가중치 파일의 sha256 이 manifest 값과 다르다."""


@dataclass(frozen=True)
class WeightsRef:
    """학습 모델 가중치의 위치와 해시. 가중치는 커밋하지 않고 경로·해시만 둔다(AGENTS.md).

    path 는 로컬 경로(RunPod Network Volume 이면 `/workspace/simai/...`), sha256 은 `sha256:<hex64>` 형식이다.
    """

    path: str
    sha256: str

    def __post_init__(self) -> None:
        digest = self.sha256.removeprefix("sha256:")
        if (
            not self.sha256.startswith("sha256:")
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError(f"sha256 형식이 아니다(sha256:<hex64>): {self.sha256!r}")

    def verify(self) -> Path:
        """파일이 있고 해시가 맞으면 경로를 돌려준다. 다르면 WeightsMismatchError, 없으면 FileNotFoundError."""
        path = Path(self.path)
        if not path.is_file():
            raise FileNotFoundError(f"가중치 파일이 없다: {path}")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = "sha256:" + h.hexdigest()
        if actual != self.sha256:
            raise WeightsMismatchError(f"가중치 해시가 다르다: {path} ({actual} != {self.sha256})")
        return path

    def to_record(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ModelRef:
    """추정에 쓴 학습 모델(고전 방법이면 None)."""

    name: str
    version: str
    variant: str
    weights: WeightsRef
    license_code: str
    license_weights: str

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "variant": self.variant,
            "weights": self.weights.to_record(),
            "license_code": self.license_code,
            "license_weights": self.license_weights,
        }


@dataclass(frozen=True)
class IntrinsicsUncertainty:
    """추정 불확실성과 근거. 모르는 값은 None. 표준편차는 1σ 근사다(설명은 각 추정기 문서)."""

    focal_std_px: float | None = None
    k1_std: float | None = None
    k2_std: float | None = None
    # 추정한 왜곡으로 편 직선(검출한 선분)의 직선성 잔차 RMS (원본 픽셀). 왜곡을 무시했을 때 값과 함께 본다
    straightness_rms_px: float | None = None
    straightness_rms_uncorrected_px: float | None = None
    line_count: int | None = None
    vanishing_point_count: int | None = None
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        for key in (
            "focal_std_px",
            "k1_std",
            "k2_std",
            "straightness_rms_px",
            "straightness_rms_uncorrected_px",
            "line_count",
            "vanishing_point_count",
        ):
            value = getattr(self, key)
            if value is not None:
                rec[key] = value
        if self.notes:
            rec["notes"] = list(self.notes)
        return rec


@dataclass(frozen=True)
class IntrinsicsEstimate:
    """카메라 한 대의 내부 파라미터 추정 결과."""

    camera_id: str
    image_size: tuple[int, int]
    focal_px: float | None
    distortion_coeffs: tuple[float, ...] | None
    radial_pixel: tuple[float, float] | None
    uncertainty: IntrinsicsUncertainty
    method: str
    method_version: str
    status: str
    model: ModelRef | None = None
    principal_point: tuple[float, float] | None = None
    principal_point_assumed: bool = True

    def __post_init__(self) -> None:
        w, h = self.image_size
        if w < 1 or h < 1:
            raise ValueError(f"image_size 가 잘못됐다: {self.image_size}")
        if self.principal_point is None:
            object.__setattr__(self, "principal_point", (w / 2.0, h / 2.0))
        if self.focal_px is not None and not (math.isfinite(self.focal_px) and self.focal_px > 0):
            raise ValueError(f"focal_px 가 양수가 아니다: {self.focal_px}")
        if self.focal_px is None and self.status == STATUS_OK:
            raise ValueError("초점이 없는데 status 가 OK 다")

    @property
    def half_diagonal(self) -> float:
        return half_diagonal(self.image_size)

    @property
    def K(self) -> list[list[float]] | None:
        if self.focal_px is None:
            return None
        cx, cy = self.principal_point  # type: ignore[misc]
        f = float(self.focal_px)
        return [[f, 0.0, float(cx)], [0.0, f, float(cy)], [0.0, 0.0, 1.0]]

    def to_contract_camera(self) -> dict[str, Any]:
        """보정 계약 `camera_calibration` 의 내부 파라미터 부분(camera_id, image_size, K, distortion).

        외부 파라미터(R, t)는 이 단계에서 모르므로 넣지 않는다. 초점을 모르면 ValueError(지어내지 않는다).
        """
        if self.focal_px is None or self.distortion_coeffs is None:
            raise ValueError(
                f"{self.camera_id}: 초점·왜곡을 정하지 못한 추정은 계약 형식으로 낼 수 없다 ({self.status})"
            )
        return {
            "camera_id": self.camera_id,
            "image_size": [int(self.image_size[0]), int(self.image_size[1])],
            "K": self.K,
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [float(c) for c in self.distortion_coeffs]},
        }


@runtime_checkable
class IntrinsicsEstimator(Protocol):
    """단일 영상 → 내부 파라미터(K, 왜곡, 불확실성, 방법·모델 버전)."""

    method: str

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        """image 는 (H, W) 또는 (H, W, C) 배열(원본 해상도). 사람이 없는 프레임을 쓴다."""
        ...


# ---------------------------------------------------------------------------
# 카메라 모델 계산 (numpy). 모두 OpenCV 방사 모델 [k1, k2, p1, p2, k3] 를 따른다.
# ---------------------------------------------------------------------------


def half_diagonal(image_size: Sequence[int]) -> float:
    w, h = image_size
    return math.hypot(w / 2.0, h / 2.0)


def opencv_from_radial_pixel(
    radial_pixel: Sequence[float], focal_px: float, image_size: Sequence[int]
) -> tuple[float, ...]:
    """픽셀 단위 방사 계수 (b1, b2) → OpenCV coeffs [k1, k2, 0, 0, 0]."""
    s = focal_px / half_diagonal(image_size)
    b1, b2 = radial_pixel
    return (float(b1 * s**2), float(b2 * s**4), 0.0, 0.0, 0.0)


def radial_pixel_from_opencv(
    coeffs: Sequence[float], focal_px: float, image_size: Sequence[int]
) -> tuple[float, float]:
    """OpenCV coeffs 의 k1, k2 → 픽셀 단위 방사 계수 (b1, b2). p1·p2·k3 는 무시한다."""
    s = half_diagonal(image_size) / focal_px
    k1 = coeffs[0] if len(coeffs) > 0 else 0.0
    k2 = coeffs[1] if len(coeffs) > 1 else 0.0
    return (float(k1 * s**2), float(k2 * s**4))


def distort_normalized(xy: np.ndarray, coeffs: Sequence[float]) -> np.ndarray:
    """정규화 좌표(왜곡 없음) (N, 2) → 왜곡 좌표. cv2.projectPoints 의 OPENCV_PINHOLE 식(5계수까지)."""
    return distortion.distort(xy, distortion.MODEL_PINHOLE, _pinhole5(coeffs))


def undistort_radii(rho_d: np.ndarray, b1: float, b2: float) -> tuple[np.ndarray, bool]:
    """ρ_d = ρ_u (1 + b1 ρ_u² + b2 ρ_u⁴) 를 ρ_u 에 대해 푼다(`geometry.distortion.undistort_radius`, 구간 보호 뉴턴법).

    돌려주는 bool 은 모든 점이 식이 단조인 첫 가지 안에서 풀렸는지다. 아니면 모델이 영상 안에서 접히므로
    그 계수는 쓸 수 없다. 풀지 못한 점은 NaN 이다.
    """
    rho_d = np.asarray(rho_d, dtype=float)
    coeffs = (float(b1), float(b2), 0.0, 0.0, 0.0)
    r = distortion.undistort_radius(rho_d, distortion.MODEL_PINHOLE, coeffs)
    return r, bool(np.all(np.isfinite(r)))


def radial_monotonic(b1: float, b2: float, rho_u_max: float, samples: int = 64) -> bool:
    """[0, ρ_u_max] 에서 ρ_d(ρ_u) 가 단조 증가인가."""
    r2 = np.linspace(0.0, rho_u_max, samples) ** 2
    return bool(np.all(1 + 3 * b1 * r2 + 5 * b2 * r2 * r2 > 1e-6))


def undistort_pixels(
    uv: np.ndarray, focal_px: float, coeffs: Sequence[float], principal_point: Sequence[float]
) -> np.ndarray:
    """원본 픽셀 (N, 2) → 왜곡을 제거한 정규화 좌표 (N, 2). 방사 k1·k2 만 쓴다(이 패키지가 내는 형식).

    풀 수 없는 점(왜곡 식이 접히는 영역 등)은 NaN 이다(`undistort_normalized`).
    """
    uv = np.asarray(uv, dtype=float).reshape(-1, 2)
    if any(abs(c) > 0 for c in list(coeffs)[2:]):
        raise ValueError("undistort_pixels 는 방사 k1·k2 만 지원한다(p1·p2·k3 = 0)")
    xd = (uv - np.asarray(principal_point, dtype=float)) / focal_px
    return undistort_normalized(xd, coeffs)


def project_normalized(
    xy: np.ndarray, focal_px: float, coeffs: Sequence[float], principal_point: Sequence[float]
) -> np.ndarray:
    """정규화 좌표(왜곡 없음) → 원본 픽셀."""
    return distort_normalized(xy, coeffs) * focal_px + np.asarray(principal_point, dtype=float)


@dataclass(frozen=True)
class ModelDiscrepancy:
    """두 내부 파라미터 모델의 같은 광선 재투영 차이(픽셀). 외부 자세는 같다고 두고 내부 파라미터만 비교한다."""

    median_px: float
    p95_px: float
    max_px: float


def model_discrepancy_px(
    reference: Mapping[str, Any] | IntrinsicsEstimate,
    other: Mapping[str, Any] | IntrinsicsEstimate,
    grid: int = 33,
    margin_px: float = 2.0,
) -> ModelDiscrepancy:
    """reference 영상의 격자 픽셀을 reference 모델로 광선으로 되돌리고 other 모델로 다시 투영한 픽셀 차이.

    reference·other 는 계약 `camera_calibration` 의 내부 파라미터 부분(K, distortion, image_size) 또는 추정 결과다.
    자세(R)를 다시 맞추지 않으므로 자세가 일부 흡수할 수 있는 차이도 그대로 드러나는 엄격한 지표다.
    reference 가 왜곡 OPENCV_PINHOLE 5계수 전체(p1·p2·k3 포함)를 써도 된다(정답 카메라).
    reference 모델로 광선을 만들 수 없는 격자 픽셀(왜곡 식이 접히는 영역 등)이 있으면 모두 inf 다(같다고 보지 않는다).
    """
    ref = _as_contract(reference)
    oth = _as_contract(other)
    if list(ref["image_size"]) != list(oth["image_size"]):
        raise ValueError(f"영상 크기가 다르다: {ref['image_size']} != {oth['image_size']}")
    w, h = ref["image_size"]
    us = np.linspace(margin_px, w - 1 - margin_px, grid)
    vs = np.linspace(margin_px, h - 1 - margin_px, grid)
    gu, gv = np.meshgrid(us, vs)
    uv = np.column_stack([gu.ravel(), gv.ravel()])
    xn = _normalize_any(ref, uv)
    K = np.asarray(oth["K"], dtype=float)
    xd = distort_normalized(xn, oth["distortion"]["coeffs"])
    proj = np.column_stack([K[0, 0] * xd[:, 0] + K[0, 2], K[1, 1] * xd[:, 1] + K[1, 2]])
    err = np.hypot(*(proj - uv).T)
    if not np.all(np.isfinite(err)):
        return ModelDiscrepancy(math.inf, math.inf, math.inf)
    return ModelDiscrepancy(float(np.median(err)), float(np.percentile(err, 95)), float(err.max()))


def _as_contract(value: Mapping[str, Any] | IntrinsicsEstimate) -> Mapping[str, Any]:
    if isinstance(value, IntrinsicsEstimate):
        return value.to_contract_camera()
    return value


def _normalize_any(camera: Mapping[str, Any], uv: np.ndarray) -> np.ndarray:
    """계약 카메라로 픽셀 → 정규화 좌표. 왜곡 제거는 geometry 의 `RigCamera.normalize` 와 같은 함수다.

    풀 수 없는 픽셀(왜곡 식이 접히는 영역 등)은 NaN 이다.
    """
    K = np.asarray(camera["K"], dtype=float)
    distortion_ = camera["distortion"]
    uv = np.asarray(uv, dtype=float).reshape(-1, 2)
    xd = np.column_stack([(uv[:, 0] - K[0, 2]) / K[0, 0], (uv[:, 1] - K[1, 2]) / K[1, 1]])
    xn, _ = distortion.undistort(xd, distortion_["model"], [float(c) for c in distortion_["coeffs"]])
    return xn


def undistort_normalized(xd: np.ndarray, coeffs: Sequence[float]) -> np.ndarray:
    """왜곡 정규화 좌표 (N, 2) → 왜곡 없는 정규화 좌표. OPENCV_PINHOLE 5계수 이하.

    `geometry.distortion.undistort`(반지름 구간 보호 뉴턴법 + 접선 2차원 뉴턴법)와 같다. 풀 수 없는 점(왜곡 식이
    접히는 영역, 미수렴, 왕복 오차 > 1e-9)은 NaN 이다.
    """
    xn, _ = distortion.undistort(xd, distortion.MODEL_PINHOLE, _pinhole5(coeffs))
    return xn


def _pinhole5(coeffs: Sequence[float]) -> tuple[float, ...]:
    """5계수 이하 → [k1 k2 p1 p2 k3] 5개(빈 자리는 0)."""
    if len(coeffs) > 5:
        raise ValueError(f"OPENCV_PINHOLE 5계수까지만 지원한다: {len(coeffs)}개")
    return tuple(float(c) for c in coeffs) + (0.0,) * (5 - len(coeffs))


def to_gray(image: np.ndarray) -> np.ndarray:
    """(H, W) 또는 (H, W, C) → float64 (H, W). 채널 평균(색 순서와 무관). 값 범위는 그대로 둔다."""
    arr = np.asarray(image)
    if arr.ndim == 2:
        return arr.astype(float)
    if arr.ndim == 3 and arr.shape[2] >= 1:
        return arr[:, :, : min(3, arr.shape[2])].astype(float).mean(axis=2)
    raise IntrinsicsEstimationError(f"영상은 (H, W) 또는 (H, W, C) 배열이어야 한다: {arr.shape}")
