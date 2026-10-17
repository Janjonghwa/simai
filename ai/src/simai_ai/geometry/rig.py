"""보정 한 버전의 카메라 묶음 (명세 4장, 20.4). 삼각측량이 쓰는 카메라 모델이다.

보정은 CalibrationProvider 로만 받는다(`contracts/providers/calibration.schema.json` 문서).
핀홀 모델은 `coordinates.PinholeCamera` 를 그대로 쓰고, 이 모듈은 그 위에 렌즈 왜곡 제거와
numpy 행렬 캐시만 더한다. 왜곡 제거 식은 OpenCV(`cv2.undistortPoints`, `cv2.fisheye.undistortPoints`)와 같다.

- 픽셀 좌표는 원본 해상도 기준 (u, v) 다. 보정의 `image_size` 와 다른 해상도의 keypoint 는 받지 않는다(호출자 책임).
- 재투영 오차는 왜곡을 제거한 이상적 핀홀 픽셀 공간에서 잰다. 왜곡이 작은 렌즈에서는 원본 픽셀 오차와 거의 같다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.providers.base import CalibrationProvenance, CalibrationProvider

# 반복 왜곡 제거 횟수. cv2.undistortPoints 기본(5회)보다 넉넉히 둔다(광각 가장자리).
UNDISTORT_ITERATIONS = 20
# OPENCV_PINHOLE 계수 개수: [k1 k2 p1 p2], [k1 k2 p1 p2 k3], [k1 k2 p1 p2 k3 k4 k5 k6] (유리 모델)
_PINHOLE_COEFF_COUNTS = (0, 4, 5, 8)


@dataclass(frozen=True)
class RigCamera:
    """보정된 카메라 한 대. x_cam = R · x_world + t (OpenCV, World → 카메라)."""

    camera_id: str
    pinhole: PinholeCamera
    image_size: tuple[int, int]
    distortion_model: str = "NONE"
    distortion_coeffs: tuple[float, ...] = ()
    K: np.ndarray = field(init=False, repr=False, compare=False)
    R: np.ndarray = field(init=False, repr=False, compare=False)
    t: np.ndarray = field(init=False, repr=False, compare=False)
    center: np.ndarray = field(init=False, repr=False, compare=False)
    _K_inv: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _check_distortion(self.camera_id, self.distortion_model, self.distortion_coeffs)
        K = np.array(self.pinhole.K, dtype=float)
        R = np.array(self.pinhole.R, dtype=float)
        t = np.array(self.pinhole.t, dtype=float)
        object.__setattr__(self, "K", K)
        object.__setattr__(self, "R", R)
        object.__setattr__(self, "t", t)
        object.__setattr__(self, "center", np.array(self.pinhole.center_world, dtype=float))
        object.__setattr__(self, "_K_inv", np.linalg.inv(K))

    @classmethod
    def from_calibration(cls, camera: Mapping[str, Any]) -> RigCamera:
        """보정 계약의 `cameras[]` 항목 하나로 만든다."""
        distortion = camera["distortion"]
        width, height = camera["image_size"]
        return cls(
            camera_id=camera["camera_id"],
            pinhole=PinholeCamera.from_calibration(camera),
            image_size=(int(width), int(height)),
            distortion_model=distortion["model"],
            distortion_coeffs=tuple(float(c) for c in distortion["coeffs"]),
        )

    def normalize(self, uv: np.ndarray) -> np.ndarray:
        """원본 픽셀 (N, 2) → 왜곡을 제거한 정규화 좌표 (N, 2). 카메라 좌표의 (x/z, y/z) 다."""
        uv = np.asarray(uv, dtype=float).reshape(-1, 2)
        homo = np.column_stack([uv, np.ones(len(uv))]) @ self._K_inv.T
        xd = homo[:, :2] / homo[:, 2:3]
        if self.distortion_model == "OPENCV_PINHOLE" and self.distortion_coeffs:
            return _undistort_pinhole(xd, self.distortion_coeffs)
        if self.distortion_model == "OPENCV_FISHEYE":
            return _undistort_fisheye(xd, self.distortion_coeffs)
        return xd

    def ideal_pixels(self, normalized: np.ndarray) -> np.ndarray:
        """정규화 좌표 (N, 2) → 왜곡 없는 이상적 핀홀 픽셀 (N, 2)."""
        normalized = np.asarray(normalized, dtype=float).reshape(-1, 2)
        homo = np.column_stack([normalized, np.ones(len(normalized))]) @ self.K.T
        return homo[:, :2] / homo[:, 2:3]

    def to_camera(self, points: np.ndarray) -> np.ndarray:
        """World 점 (N, 3) → 카메라 좌표 (N, 3)."""
        return np.asarray(points, dtype=float).reshape(-1, 3) @ self.R.T + self.t


@dataclass(frozen=True)
class CameraRig:
    """CalibrationProvider 가 낸 보정 한 버전의 카메라 묶음."""

    calibration_version: str
    provider_source: str
    state: str
    cameras: Mapping[str, RigCamera]
    calibration_provenance: CalibrationProvenance

    @classmethod
    def from_provider(cls, provider: CalibrationProvider) -> CameraRig:
        """보정 Provider 의 현재 버전으로 만든다. World 좌표계·OpenCV 외부 파라미터 규약이 아니면 받지 않는다."""
        doc = provider.current()
        frame = doc["coordinate_frame"]
        if (frame["frame"], frame["unit"], frame["handedness"], frame["up_axis"]) != ("WORLD", "m", "right", "+Z"):
            raise ValueError(f"보정 좌표계가 World(m, 오른손, +Z)가 아니다: {frame}")
        if doc["extrinsic_convention"] != "OPENCV_WORLD_TO_CAMERA":
            raise ValueError(f"외부 파라미터 규약이 OPENCV_WORLD_TO_CAMERA 가 아니다: {doc['extrinsic_convention']}")
        cameras: dict[str, RigCamera] = {}
        for cam in doc["cameras"]:
            if cam["camera_id"] in cameras:
                raise ValueError(f"같은 camera_id 가 두 번 있다: {cam['camera_id']}")
            cameras[cam["camera_id"]] = RigCamera.from_calibration(cam)
        return cls(
            calibration_version=doc["calibration_version"],
            provider_source=doc["provider_source"],
            state=doc["state"],
            cameras=cameras,
            calibration_provenance=provider.provenance(),
        )

    def subset(self, camera_ids: Iterable[str]) -> list[RigCamera]:
        """camera_ids 순서의 카메라 목록. 보정에 없는 카메라는 오류다(모르는 카메라로 삼각측량하지 않는다)."""
        missing = [cid for cid in camera_ids if cid not in self.cameras]
        if missing:
            raise KeyError(f"보정 {self.calibration_version} 에 없는 카메라: {missing}")
        return [self.cameras[cid] for cid in camera_ids]


def _check_distortion(camera_id: str, model: str, coeffs: tuple[float, ...]) -> None:
    if model == "NONE":
        if any(c != 0.0 for c in coeffs):
            raise ValueError(f"{camera_id}: 왜곡 모델 NONE 인데 계수가 있다: {coeffs}")
    elif model == "OPENCV_PINHOLE":
        if len(coeffs) not in _PINHOLE_COEFF_COUNTS:
            raise ValueError(f"{camera_id}: OPENCV_PINHOLE 계수는 4·5·8개만 지원한다: {len(coeffs)}개")
    elif model == "OPENCV_FISHEYE":
        if len(coeffs) != 4:
            raise ValueError(f"{camera_id}: OPENCV_FISHEYE 계수는 [k1, k2, k3, k4] 4개여야 한다: {len(coeffs)}개")
    else:
        raise ValueError(f"{camera_id}: 모르는 왜곡 모델 {model}")


def _undistort_pinhole(xd: np.ndarray, coeffs: tuple[float, ...]) -> np.ndarray:
    """OpenCV 핀홀 왜곡의 역변환 (cv2.undistortPoints 와 같은 고정점 반복)."""
    k = list(coeffs) + [0.0] * (8 - len(coeffs))
    k1, k2, p1, p2, k3, k4, k5, k6 = k
    x = xd.copy()
    for _ in range(UNDISTORT_ITERATIONS):
        r2 = (x**2).sum(axis=1)
        icdist = (1 + ((k6 * r2 + k5) * r2 + k4) * r2) / (1 + ((k3 * r2 + k2) * r2 + k1) * r2)
        dx = 2 * p1 * x[:, 0] * x[:, 1] + p2 * (r2 + 2 * x[:, 0] ** 2)
        dy = p1 * (r2 + 2 * x[:, 1] ** 2) + 2 * p2 * x[:, 0] * x[:, 1]
        x = np.column_stack([(xd[:, 0] - dx) * icdist, (xd[:, 1] - dy) * icdist])
    return x


def _undistort_fisheye(xd: np.ndarray, coeffs: tuple[float, ...]) -> np.ndarray:
    """OpenCV 어안 왜곡의 역변환. θ_d = θ(1 + k1θ² + k2θ⁴ + k3θ⁶ + k4θ⁸) 를 뉴턴법으로 푼다."""
    k1, k2, k3, k4 = coeffs
    theta_d = np.clip(np.hypot(xd[:, 0], xd[:, 1]), -math.pi / 2, math.pi / 2)
    theta = theta_d.copy()
    for _ in range(UNDISTORT_ITERATIONS):
        t2 = theta * theta
        f = theta * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4)))) - theta_d
        df = 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + 9 * t2 * k4)))
        theta = theta - f / df
    scale = np.ones_like(theta_d)
    nz = theta_d > 1e-12
    scale[nz] = np.tan(theta[nz]) / theta_d[nz]
    return xd * scale[:, None]
