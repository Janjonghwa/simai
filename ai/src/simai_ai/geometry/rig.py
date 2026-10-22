"""보정 한 버전의 카메라 묶음 (명세 4장, 20.4). 삼각측량이 쓰는 카메라 모델이다.

보정은 CalibrationProvider 로만 받는다(`contracts/providers/calibration.schema.json` 문서).
핀홀 모델은 `coordinates.PinholeCamera` 를 그대로 쓰고, 이 모듈은 그 위에 렌즈 왜곡 제거와
numpy 행렬 캐시만 더한다. 왜곡 식은 OpenCV 와 같고, 왜곡 제거는 `distortion.undistort`(구간 보호 뉴턴법)로 푼다.

- 왜곡 제거가 실패한 픽셀(왜곡 식이 접히는 영역, 미수렴, 왕복 오차가 허용치 초과, 유한하지 않은 입력)은 지어내지 않고
  NaN 으로 돌려준다(모르면 비움). 호출자는 그 관측을 빼야 한다. 실패 마스크가 필요하면 `normalize_with_mask` 를 쓴다.
- 왜곡 제거는 점마다 독립이라 여러 점을 한 번에 넘겨도 점별 결과는 같다. 호출 한 번의 고정 비용이 크므로 view 하나의
  keypoint 는 `undistort_keypoints` 로 한 번에 풀고, 그 결과(`UndistortedKeypoints`)를 삼각측량·관절 게이트·Cross-view 가
  다시 쓴다(S15P31A101-168, docs/design/tier0-geometry-budget.md).

- 픽셀 좌표는 원본 해상도 기준 (u, v) 다. 보정의 `image_size` 와 다른 해상도의 keypoint 는 받지 않는다(호출자 책임).
- 재투영 오차는 왜곡을 제거한 이상적 핀홀 픽셀 공간에서 잰다. 왜곡이 작은 렌즈에서는 원본 픽셀 오차와 거의 같다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.geometry import distortion
from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.providers.base import CalibrationProvenance, CalibrationProvider

# 왜곡 제거 왕복 오차 허용치 기본값(원본 픽셀). 다시 왜곡한 픽셀이 입력 픽셀과 이만큼 넘게 다르면 그 점은 NaN 이다
DEFAULT_UNDISTORT_TOLERANCE_PX = 0.01


@dataclass(frozen=True)
class RigCamera:
    """보정된 카메라 한 대. x_cam = R · x_world + t (OpenCV, World → 카메라)."""

    camera_id: str
    pinhole: PinholeCamera
    image_size: tuple[int, int]
    distortion_model: str = "NONE"
    distortion_coeffs: tuple[float, ...] = ()
    undistort_tolerance_px: float = DEFAULT_UNDISTORT_TOLERANCE_PX
    """왜곡 제거 왕복 오차 허용치(원본 픽셀)"""
    K: np.ndarray = field(init=False, repr=False, compare=False)
    R: np.ndarray = field(init=False, repr=False, compare=False)
    t: np.ndarray = field(init=False, repr=False, compare=False)
    center: np.ndarray = field(init=False, repr=False, compare=False)
    _K_inv: np.ndarray = field(init=False, repr=False, compare=False)
    _branch: distortion.RadialBranch = field(init=False, repr=False, compare=False)
    _max_residual: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            distortion.check_model(self.distortion_model, self.distortion_coeffs)
        except ValueError as exc:
            raise ValueError(f"{self.camera_id}: {exc}") from None
        if not (self.undistort_tolerance_px > 0 and math.isfinite(self.undistort_tolerance_px)):
            raise ValueError(f"{self.camera_id}: undistort_tolerance_px 는 유한한 양수여야 한다: {self.undistort_tolerance_px}")
        K = np.array(self.pinhole.K, dtype=float)
        R = np.array(self.pinhole.R, dtype=float)
        t = np.array(self.pinhole.t, dtype=float)
        object.__setattr__(self, "K", K)
        object.__setattr__(self, "R", R)
        object.__setattr__(self, "t", t)
        object.__setattr__(self, "center", np.array(self.pinhole.center_world, dtype=float))
        object.__setattr__(self, "_K_inv", np.linalg.inv(K))
        object.__setattr__(self, "_branch", distortion.radial_branch(self.distortion_model, self.distortion_coeffs))
        # 정규화 좌표 잔차 → 픽셀 잔차의 최대 배율(K 왼쪽 위 2x2 의 스펙트럼 노름)로 나눠
        # 어느 방향으로도 픽셀 허용치를 넘지 않게 한다(비등방 초점·비대칭 계수 포함)
        object.__setattr__(self, "_max_residual", self.undistort_tolerance_px / float(np.linalg.norm(K[:2, :2], 2)))

    @classmethod
    def from_calibration(
        cls, camera: Mapping[str, Any], undistort_tolerance_px: float = DEFAULT_UNDISTORT_TOLERANCE_PX
    ) -> RigCamera:
        """보정 계약의 `cameras[]` 항목 하나로 만든다."""
        distortion = camera["distortion"]
        width, height = camera["image_size"]
        return cls(
            camera_id=camera["camera_id"],
            pinhole=PinholeCamera.from_calibration(camera),
            image_size=(int(width), int(height)),
            distortion_model=distortion["model"],
            distortion_coeffs=tuple(float(c) for c in distortion["coeffs"]),
            undistort_tolerance_px=undistort_tolerance_px,
        )

    def normalize(self, uv: np.ndarray) -> np.ndarray:
        """원본 픽셀 (N, 2) → 왜곡을 제거한 정규화 좌표 (N, 2). 카메라 좌표의 (x/z, y/z) 다.

        왜곡을 제거할 수 없는 픽셀의 행은 NaN 이다(`normalize_with_mask`).
        """
        return self.normalize_with_mask(uv)[0]

    def normalize_with_mask(self, uv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """원본 픽셀 (N, 2) → (정규화 좌표 (N, 2), 성공 마스크 (N,)). 실패한 행은 NaN 이다.

        실패: 유한하지 않은 입력, 왜곡 식이 접히는 영역(단조성이 깨지는 반지름 너머), 미수렴,
        다시 왜곡한 픽셀과 입력 픽셀의 차이가 `undistort_tolerance_px` 초과.
        """
        uv = np.asarray(uv, dtype=float).reshape(-1, 2)
        with np.errstate(invalid="ignore"):  # 유한하지 않은 입력은 아래에서 실패로 표시한다
            homo = np.column_stack([uv, np.ones(len(uv))]) @ self._K_inv.T
            xd = homo[:, :2] / homo[:, 2:3]
        return distortion.undistort(xd, self.distortion_model, self.distortion_coeffs, self._max_residual, self._branch)

    def undistort_keypoints(self, keypoints: Sequence[Sequence[float] | None]) -> UndistortedKeypoints:
        """view 하나의 keypoint `(u, v, 신뢰도)` 또는 None 목록 전체를 한 번에 왜곡 제거한다.

        행마다 `normalize_with_mask` 를 따로 부른 것과 결과가 같다. 값이 없거나(None) (u, v, 신뢰도) 중 유한하지 않은
        값이 있는 keypoint 는 정규화 좌표·신뢰도를 NaN 으로 둔다(관측으로 세지 않는다).
        """
        return self.undistort_many([keypoints])[0]

    def undistort_many(self, views: Sequence[Sequence[Sequence[float] | None]]) -> list[UndistortedKeypoints]:
        """이 카메라의 여러 view(사람마다 keypoint 목록)를 왜곡 제거 한 번으로 푼다. 결과는 view 순서다.

        같은 시각 이 카메라에 보인 사람 전체를 묶어 넘기면 호출 고정 비용을 한 번만 낸다. 점마다 독립이라
        view 하나씩 `undistort_keypoints` 로 푼 것과 결과가 같다.
        """
        sizes = [len(kps) for kps in views]
        n = sum(sizes)
        pixels = np.full((n, 2), np.nan)
        confidence = np.full(n, np.nan)
        row = 0
        for kps in views:
            for kp in kps:
                if kp is not None:
                    u, v, c = float(kp[0]), float(kp[1]), float(kp[2])
                    if math.isfinite(u) and math.isfinite(v) and math.isfinite(c):
                        pixels[row] = (u, v)
                        confidence[row] = c
                row += 1
        normalized, ok = self.normalize_with_mask(pixels)
        ideal = np.full((n, 2), np.nan)
        if ok.any():
            ideal[ok] = self.ideal_pixels(normalized[ok])
        out = []
        row = 0
        for kps, size in zip(views, sizes):
            parts = [a[row : row + size].copy() for a in (normalized, ok, confidence, ideal)]
            for array in parts:
                array.flags.writeable = False
            out.append(UndistortedKeypoints(self.camera_id, tuple(kps), *parts, camera=self))
            row += size
        return out

    def ideal_pixels(self, normalized: np.ndarray) -> np.ndarray:
        """정규화 좌표 (N, 2) → 왜곡 없는 이상적 핀홀 픽셀 (N, 2)."""
        normalized = np.asarray(normalized, dtype=float).reshape(-1, 2)
        homo = np.column_stack([normalized, np.ones(len(normalized))]) @ self.K.T
        return homo[:, :2] / homo[:, 2:3]

    def to_camera(self, points: np.ndarray) -> np.ndarray:
        """World 점 (N, 3) → 카메라 좌표 (N, 3)."""
        return np.asarray(points, dtype=float).reshape(-1, 3) @ self.R.T + self.t


@dataclass(frozen=True, eq=False)
class UndistortedKeypoints:
    """카메라 한 대 view 하나의 keypoint 를 한 번에 왜곡 제거한 결과 (`RigCamera.undistort_keypoints`). 배열은 읽기 전용이다.

    관절 j 를 관측으로 셀 수 있는지는 `usable(min_confidence)` 로 본다: 값이 있고 (u, v, 신뢰도)가 모두 유한하고
    신뢰도가 기준 이상이며 왜곡을 제거할 수 있다. 모르는 값은 NaN 으로 비워 두고 지어내지 않는다.
    """

    camera_id: str
    keypoints: tuple[Sequence[float] | None, ...]
    """왜곡 제거에 쓴 원본 keypoint (다시 쓸 때 같은 view 인지 확인한다)"""
    normalized: np.ndarray
    """왜곡을 제거한 정규화 좌표 (J, 2). 값이 없거나 유한하지 않거나 왜곡을 제거할 수 없으면 NaN"""
    undistorted: np.ndarray
    """(J,) 값이 있고 왜곡 제거에 성공했는지"""
    confidence: np.ndarray
    """(J,) keypoint 신뢰도. 값이 없거나 (u, v, 신뢰도) 중 유한하지 않은 값이 있으면 NaN"""
    ideal_pixels: np.ndarray
    """(J, 2) 왜곡 없는 이상적 핀홀 픽셀 (`RigCamera.ideal_pixels(normalized)`). 재투영 오차의 기준점이다"""
    camera: RigCamera
    """왜곡 제거에 쓴 카메라 모델 (다시 쓸 때 같은 보정인지 확인한다)"""

    def usable(self, min_confidence: float) -> np.ndarray:
        """(J,) 관측으로 셀 수 있는 관절: 왜곡 제거 성공이고 신뢰도가 유한하며 기준 이상."""
        with np.errstate(invalid="ignore"):
            return self.undistorted & (self.confidence >= min_confidence)

    def matches(self, camera: RigCamera, keypoints: Sequence[Sequence[float] | None]) -> bool:
        """이 결과가 그 카메라 모델(같은 보정 값)로 그 keypoint 를 푼 것인지. 카메라·keypoint 가 같은 객체이거나 값이 같다.

        keypoint 가 만든 뒤 바뀌지 않는다고 전제한다(`InterpolatedView.keypoints` 는 tuple). 원소를 제자리에서 바꾸는
        가변 목록을 넘기면 같은 객체라서 낡은 결과를 쓸 수 있으므로, 바꿀 때는 새 tuple 을 만든다.
        """
        if camera is not self.camera and camera != self.camera:
            return False
        return keypoints is self.keypoints or tuple(keypoints) == self.keypoints


@dataclass(frozen=True)
class CameraRig:
    """CalibrationProvider 가 낸 보정 한 버전의 카메라 묶음."""

    calibration_version: str
    provider_source: str
    state: str
    cameras: Mapping[str, RigCamera]
    calibration_provenance: CalibrationProvenance

    @classmethod
    def from_provider(
        cls, provider: CalibrationProvider, undistort_tolerance_px: float = DEFAULT_UNDISTORT_TOLERANCE_PX
    ) -> CameraRig:
        """보정 Provider 의 현재 버전으로 만든다. World 좌표계·OpenCV 외부 파라미터 규약이 아니면 받지 않는다.

        undistort_tolerance_px: 카메라마다 왜곡 제거 왕복 오차 허용치(원본 픽셀).
        """
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
            cameras[cam["camera_id"]] = RigCamera.from_calibration(cam, undistort_tolerance_px)
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
