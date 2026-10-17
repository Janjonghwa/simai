"""BEV 평면도 자동 생성 (명세 4.1 BEV 평면도, 13.1, 요구사항 FR-22).

보정(CalibrationProvider)과 카메라별 프레임으로 각 카메라 영상의 바닥(World Z = 0 평면)을 위에서 본 화면으로
펴서 하나로 잇는다.

- **보조 화면이다.** 전체 상황을 보는 간략한 2D 개요이며 좌표·스케일의 기준이 아니다. 정확도 목표가 없고
  안전 판단(Risk Engine, 구역 판정)에 쓰지 않는다. 위치의 정본은 Tier 0, 공간의 정본은 보정으로 확정한
  바닥 평면과 등록 정적 객체·위험 구역이다(명세 5.3).
- **사람은 그리지 않는다.** 뷰어가 World 좌표(world_state)로 따로 그린다. 원본 프레임에 사람이 찍혀 있으면
  바닥 위로 늘어진 얼룩이 되므로, 사람 없는 프레임을 쓰거나 `exclude_masks` 로 그 픽셀을 뺀다.
  Z = 0 이 아닌 것(책상, 벽, 사람)은 모두 바닥에 늘어져 보인다.
- **방식(역사상).** BEV 픽셀 중심의 World 점 (x, y, 0) 을 카메라마다 투영(외부 R·t → 핀홀 K → 렌즈 왜곡
  적용)해 원본 영상에서 쌍선형 보간으로 가져온다. 바닥 평면의 호모그래피 H = K·[r1 r2 t] 에 렌즈 왜곡을
  더한 것과 같다. 카메라 모델은 `geometry.rig.RigCamera`(보정 계약 그대로)를 쓴다.
- **렌즈 왜곡.** 정방향 왜곡 식은 OpenCV `cv2.projectPoints`·`cv2.fisheye.projectPoints` 와 같다. 광각
  가장자리에서 다항 왜곡 모델이 접히는 곳(fold-over)에 다른 바닥이 겹쳐 찍히지 않도록, 왜곡한 픽셀을
  `RigCamera.normalize`(geometry 의 왜곡 제거)로 되돌려 원래 점과 맞지 않으면 버린다.
- **겹침 합성.** 여러 카메라가 같은 바닥을 보면 가중 평균한다. 가중치 = sin(시선각)^a / (거리 m)^b × 영상
  가장자리 페더. 시선각은 바닥면과 시선이 이루는 각이다(누운 시선일수록 바닥 해상도가 나빠진다).
- **픽셀 규약.** 원본 영상은 OpenCV 규약(정수 (u, v) 가 픽셀 중심, 배열 인덱스 [v, u]). BEV 는 열이 +X,
  행이 −Y 방향(위에서 내려다본 지도, 위쪽이 +Y)이고 픽셀 (col, row) 중심의 World 좌표는
  (min_x + (col + 0.5)·m, max_y − (row + 0.5)·m) 이다(m = 픽셀당 미터). 메타데이터의 `pixel_to_world` 가 이 식이다.
- **버전.** 결과에 사용한 calibration_version 과 생성 시각을 기록한다. 보정 버전이 바뀌면 재생성 대상이다
  (`needs_regeneration`). 같은 보정으로 다시 만들어도 덮어쓰지 않고 새 결과로 둔다(호출자 저장 규칙).
- **방 외곽.** 대략만 추출한다(`room_outline`). 관리자 확인용이며 정확도를 주장하지 않는다.
- **정적 3D.** `StaticSceneLayer` 는 정적 3D(MapAnything 결과)를 위에서 본 층으로 함께 쓰는 확장 지점이다.
  지금은 카메라가 보지 못한 BEV 픽셀만 그 층으로 채운다. 실제 연결은 후속 작업이다.

시각은 UTC int64 ms 이고 호출자가 주입한다(현재 시각을 직접 읽지 않는다).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.providers.base import FRAME_SOURCES, CalibrationProvider, Frame
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.scene.room_outline import convex_outline_from_mask

# 결과 메타데이터 형식. contracts/ 의 계약이 아니라 이 모듈의 형식이다(계약화는 MR 에서 제안).
BEV_FORMAT = "simai.bev_floorplan"
BEV_FORMAT_VERSION = "0.1.0"

WORLD_FRAME = {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}


class BevInputError(ValueError):
    """BEV 를 만들 수 없는 입력(보정 상태, 프레임 크기, 범위 등)."""


@dataclass(frozen=True)
class BevConfig:
    """BEV 생성 설정. 길이는 미터, 각도는 라디안."""

    meters_per_pixel: float = 0.02
    # World 범위 ((min_x, min_y), (max_x, max_y)). None 이면 카메라들의 바닥 시야 범위로 자동으로 정한다.
    world_bounds: tuple[tuple[float, float], tuple[float, float]] | None = None
    # 이 보정 상태에서만 만든다. 기본은 CALIBRATED(명세 18.1: CALIBRATED 확인 → BEV 자동 생성).
    allowed_states: tuple[str, ...] = ("CALIBRATED",)
    # False 면 렌즈 왜곡을 무시하고 이상적 핀홀로 투영한다(비교·진단용). 결과 메타데이터에 남는다.
    undistort: bool = True
    # 카메라 중심에서 이 거리(수평, m)보다 먼 바닥은 쓰지 않는다.
    max_ground_distance_m: float = 15.0
    # 바닥과 시선이 이루는 각이 이보다 작은(누운) 바닥은 쓰지 않는다.
    min_elevation_rad: float = math.radians(5.0)
    elevation_power: float = 1.0
    distance_power: float = 2.0
    # 영상 가장자리에서 이 픽셀 거리까지 가중치를 0 → 1 로 올린다(이음매 완화). 0 이면 페더 없음.
    feather_px: float = 16.0
    # 왜곡 → 왜곡 제거 왕복 허용 오차(원본 픽셀). 넘으면 fold-over 로 보고 버린다.
    roundtrip_tolerance_px: float = 0.5
    # 카메라 앞 이 거리보다 가까운 점은 투영하지 않는다.
    near_m: float = 0.05
    # 자동 범위를 정할 때 원본 영상을 이 간격(픽셀)으로 표본한다.
    auto_bounds_step_px: int = 8
    max_raster_pixels: int = 16_000_000
    # 행 단위로 나눠 계산한다(메모리 상한).
    chunk_rows: int = 256
    extract_outline: bool = True
    fill_value: float = 0.0

    def __post_init__(self) -> None:
        if not self.meters_per_pixel > 0.0:
            raise ValueError(f"meters_per_pixel 은 양수여야 한다: {self.meters_per_pixel}")
        if self.world_bounds is not None:
            (x0, y0), (x1, y1) = self.world_bounds
            if not (x1 > x0 and y1 > y0):
                raise ValueError(f"world_bounds 는 (min, max) 순서여야 한다: {self.world_bounds}")
        if not self.allowed_states:
            raise ValueError("allowed_states 가 비어 있다")
        for name in ("max_ground_distance_m", "near_m", "roundtrip_tolerance_px"):
            if not getattr(self, name) > 0.0:
                raise ValueError(f"{name} 은 양수여야 한다: {getattr(self, name)}")
        if not 0.0 <= self.min_elevation_rad < math.pi / 2:
            raise ValueError(f"min_elevation_rad 는 [0, π/2) 범위여야 한다: {self.min_elevation_rad}")
        if self.feather_px < 0.0 or self.elevation_power < 0.0 or self.distance_power < 0.0:
            raise ValueError("feather_px, elevation_power, distance_power 는 음수일 수 없다")
        if self.auto_bounds_step_px < 1 or self.chunk_rows < 1 or self.max_raster_pixels < 1:
            raise ValueError("auto_bounds_step_px, chunk_rows, max_raster_pixels 는 1 이상이어야 한다")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "max_ground_distance_m": self.max_ground_distance_m,
            "min_elevation_rad": self.min_elevation_rad,
            "elevation_power": self.elevation_power,
            "distance_power": self.distance_power,
            "feather_px": self.feather_px,
            "roundtrip_tolerance_px": self.roundtrip_tolerance_px,
        }


@dataclass(frozen=True)
class BevRaster:
    """BEV 격자. 열 = +X, 행 = −Y. (min_x, max_y) 가 왼쪽 위 픽셀의 바깥 모서리다."""

    min_x: float
    max_y: float
    width: int
    height: int
    meters_per_pixel: float

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError(f"BEV 크기는 1 이상이어야 한다: {self.width}x{self.height}")
        if not self.meters_per_pixel > 0.0:
            raise ValueError(f"meters_per_pixel 은 양수여야 한다: {self.meters_per_pixel}")

    @classmethod
    def from_bounds(
        cls, min_xy: tuple[float, float], max_xy: tuple[float, float], meters_per_pixel: float
    ) -> BevRaster:
        """World 범위를 덮는 격자. 크기는 올림으로 정하고 (min_x, max_y) 모서리는 그대로 둔다."""
        width = max(1, math.ceil((max_xy[0] - min_xy[0]) / meters_per_pixel - 1e-9))
        height = max(1, math.ceil((max_xy[1] - min_xy[1]) / meters_per_pixel - 1e-9))
        return cls(float(min_xy[0]), float(max_xy[1]), width, height, float(meters_per_pixel))

    @property
    def max_x(self) -> float:
        return self.min_x + self.width * self.meters_per_pixel

    @property
    def min_y(self) -> float:
        return self.max_y - self.height * self.meters_per_pixel

    @property
    def pixel_to_world(self) -> tuple[tuple[float, float, float], ...]:
        """(col, row, 1) → (x, y, 1). 정수 (col, row) 가 픽셀 중심이다."""
        m = self.meters_per_pixel
        return ((m, 0.0, self.min_x + 0.5 * m), (0.0, -m, self.max_y - 0.5 * m), (0.0, 0.0, 1.0))

    def world_to_pixel(self, xy: np.ndarray) -> np.ndarray:
        """World (N, 2) → 연속 픽셀 좌표 (col, row) (N, 2). 정수가 픽셀 중심."""
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        m = self.meters_per_pixel
        return np.column_stack([(xy[:, 0] - self.min_x) / m - 0.5, (self.max_y - xy[:, 1]) / m - 0.5])

    def pixel_centers(self, row_start: int = 0, row_stop: int | None = None) -> np.ndarray:
        """행 범위의 픽셀 중심 World (x, y) 를 (rows·width, 2) 로. 행 우선."""
        row_stop = self.height if row_stop is None else row_stop
        m = self.meters_per_pixel
        xs = self.min_x + (np.arange(self.width) + 0.5) * m
        ys = self.max_y - (np.arange(row_start, row_stop) + 0.5) * m
        gx, gy = np.meshgrid(xs, ys)
        return np.column_stack([gx.ravel(), gy.ravel()])

    def to_metadata(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "meters_per_pixel": self.meters_per_pixel,
            "origin_xy": [self.min_x, self.max_y],
            "origin": "TOP_LEFT_PIXEL_OUTER_CORNER",
            "col_axis": "+X",
            "row_axis": "-Y",
            "pixel_to_world": [list(r) for r in self.pixel_to_world],
            "world_bounds": {"min_xy": [self.min_x, self.min_y], "max_xy": [self.max_x, self.max_y]},
        }


class StaticSceneLayer(Protocol):
    """정적 3D 를 위에서 본 층으로 BEV 에 함께 쓰는 확장 지점 (명세 4.1, 5.3).

    MapAnything 결과(점군·메시)를 같은 BEV 격자로 내려 그린 구현을 후속 작업에서 붙인다.
    `render_top_down` 은 (image (H, W, C), mask (H, W) bool) 을 돌려준다. C 는 카메라 프레임과 같아야 한다.
    """

    @property
    def scene_version(self) -> str: ...

    def render_top_down(self, raster: BevRaster) -> tuple[np.ndarray, np.ndarray]: ...


@dataclass(frozen=True)
class BevFloorPlan:
    """BEV 평면도 한 건. image 는 (H, W, C). coverage 는 카메라가 바닥을 본 픽셀."""

    image: np.ndarray
    coverage: np.ndarray
    weight: np.ndarray
    dominant_camera: np.ndarray
    raster: BevRaster
    metadata: dict[str, Any] = field(repr=False)
    # 정적 3D 층으로 채운 픽셀(카메라가 보지 못한 곳). 층을 쓰지 않았으면 None.
    static_filled: np.ndarray | None = None

    @property
    def calibration_version(self) -> str:
        return self.metadata["calibration_version"]

    def needs_regeneration(self, current_calibration_version: str) -> bool:
        return needs_regeneration(self.metadata, current_calibration_version)


def needs_regeneration(metadata: Mapping[str, Any], current_calibration_version: str) -> bool:
    """이 BEV 를 만든 보정 버전이 지금 보정 버전과 다르면 True(재생성 대상)."""
    return metadata.get("calibration_version") != current_calibration_version


def generate_bev(
    calibration: CalibrationProvider,
    frames: Mapping[str, Frame] | Iterable[Frame],
    *,
    frame_source: str,
    created_at_ms: int,
    config: BevConfig | None = None,
    floor_masks: Mapping[str, np.ndarray] | None = None,
    exclude_masks: Mapping[str, np.ndarray] | None = None,
    static_scene: StaticSceneLayer | None = None,
) -> BevFloorPlan:
    """보정과 카메라별 프레임으로 BEV 평면도를 만든다.

    frame_source: 프레임 출처(LIVE, FILE, SIM, DATASET). input_provenance 에 들어간다.
    floor_masks: 카메라별 바닥 픽셀 마스크(원본 해상도, True = 바닥). 있으면 바닥으로 표시된 픽셀만 쓰고
        방 외곽 추정 근거가 FLOOR_MASK 가 된다. 없으면 카메라 시야 전체를 바닥으로 본다.
    exclude_masks: 카메라별로 쓰지 않을 픽셀(True = 제외). 사람·이동 물체 마스크 등.
    """
    config = config or BevConfig()
    if frame_source not in FRAME_SOURCES:
        raise BevInputError(f"모르는 frame_source: {frame_source} (허용: {FRAME_SOURCES})")
    if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int):
        raise BevInputError(f"created_at_ms 는 UTC int64 ms 정수여야 한다: {created_at_ms!r}")

    rig = CameraRig.from_provider(calibration)
    if rig.state not in config.allowed_states:
        raise BevInputError(
            f"보정 {rig.calibration_version} 의 상태 {rig.state} 로는 BEV 를 만들지 않는다 (허용: {config.allowed_states})"
        )

    by_camera = _frames_by_camera(frames)
    cameras = rig.subset(sorted(by_camera))
    images = {cam.camera_id: _check_image(cam, by_camera[cam.camera_id].image) for cam in cameras}
    channels = {img.shape[2] for img in images.values()}
    if len(channels) != 1:
        raise BevInputError(f"카메라 프레임의 채널 수가 서로 다르다: {sorted(channels)}")
    n_channels = channels.pop()
    floor = _check_masks(cameras, floor_masks, "floor_masks")
    exclude = _check_masks(cameras, exclude_masks, "exclude_masks")

    skipped: dict[str, str] = {}
    usable: list[RigCamera] = []
    for cam in cameras:
        if cam.center[2] <= 0.0:
            skipped[cam.camera_id] = "CAMERA_NOT_ABOVE_FLOOR"
        else:
            usable.append(cam)
    if not usable:
        raise BevInputError(f"바닥 위에 있는 카메라가 없다: {skipped}")

    if config.world_bounds is not None:
        raster = BevRaster.from_bounds(config.world_bounds[0], config.world_bounds[1], config.meters_per_pixel)
    else:
        raster = _auto_raster(usable, config, floor)
    if raster.width * raster.height > config.max_raster_pixels:
        raise BevInputError(
            f"BEV 가 너무 크다: {raster.width}x{raster.height} > {config.max_raster_pixels} 픽셀. "
            "world_bounds 나 meters_per_pixel 을 조정한다"
        )

    acc = np.zeros((raster.height * raster.width, n_channels), dtype=np.float64)
    wsum = np.zeros(raster.height * raster.width, dtype=np.float64)
    wbest = np.zeros(raster.height * raster.width, dtype=np.float64)
    dominant = np.full(raster.height * raster.width, -1, dtype=np.int16)
    float_images = {cid: img.astype(np.float32, copy=False) for cid, img in images.items()}

    for row0 in range(0, raster.height, config.chunk_rows):
        row1 = min(raster.height, row0 + config.chunk_rows)
        sl = slice(row0 * raster.width, row1 * raster.width)
        xy = raster.pixel_centers(row0, row1)
        points = np.column_stack([xy, np.zeros(len(xy))])
        for index, cam in enumerate(cameras):
            if cam.camera_id in skipped:
                continue
            uv, weight = _camera_samples(cam, points, config, floor.get(cam.camera_id), exclude.get(cam.camera_id))
            hit = weight > 0.0
            if not hit.any():
                continue
            colors = _bilinear(float_images[cam.camera_id], uv[hit])
            w = weight[hit]
            idx = np.flatnonzero(hit) + sl.start
            acc[idx] += colors * w[:, None]
            wsum[idx] += w
            better = w > wbest[idx]
            wbest[idx[better]] = w[better]
            dominant[idx[better]] = index

    coverage = wsum > 0.0
    out = np.full_like(acc, config.fill_value)
    out[coverage] = acc[coverage] / wsum[coverage, None]
    image = out.reshape(raster.height, raster.width, n_channels)
    coverage = coverage.reshape(raster.height, raster.width)

    static_meta = None
    static_filled = None
    if static_scene is not None:
        static_meta, static_filled = _fill_from_static(image, coverage, static_scene, raster)

    if all(img.dtype == np.uint8 for img in images.values()):
        image = np.clip(np.rint(image), 0, 255).astype(np.uint8)
    else:
        image = image.astype(np.float32)

    used = [cam.camera_id for cam in cameras if cam.camera_id not in skipped]
    outline = None
    if config.extract_outline and coverage.any():
        basis = "FLOOR_MASK" if all(cid in floor for cid in used) else "CAMERA_FOOTPRINT"
        outline = convex_outline_from_mask(coverage, raster, basis=basis)

    frames_meta = [_frame_metadata(by_camera[cid]) for cid in used]
    metadata: dict[str, Any] = {
        "format": BEV_FORMAT,
        "format_version": BEV_FORMAT_VERSION,
        "created_at": created_at_ms,
        "purpose": "OVERVIEW_DISPLAY_ONLY",
        "accuracy": "NOT_CLAIMED",
        "safety_use": False,
        "calibration_version": rig.calibration_version,
        "calibration_state": rig.state,
        "coordinate_frame": dict(WORLD_FRAME),
        "plane_z_m": 0.0,
        "raster": raster.to_metadata(),
        "channels": n_channels,
        "camera_ids": [cam.camera_id for cam in cameras],
        "cameras_used": used,
        "cameras_skipped": skipped,
        "frames": frames_meta,
        "lens_distortion_applied": config.undistort,
        "blend": {
            "method": "WEIGHTED_MEAN",
            "weight": "sin(elevation)^a / distance_m^b * edge_feather",
            **config.to_metadata(),
        },
        "coverage_ratio": float(coverage.mean()),
        "humans_drawn": False,
        "masks": {"floor_masks": sorted(floor), "exclude_masks": sorted(exclude)},
        "room_outline": outline,
        "static_scene": static_meta,
        "input_provenance": build_input_provenance(
            {"source": frame_source, "camera_ids": used}, rig.calibration_provenance
        ),
    }
    return BevFloorPlan(
        image=image,
        coverage=coverage,
        weight=wsum.reshape(raster.height, raster.width).astype(np.float32),
        dominant_camera=dominant.reshape(raster.height, raster.width),
        raster=raster,
        metadata=metadata,
        static_filled=static_filled,
    )


def distort_normalized(xy: np.ndarray, model: str, coeffs: tuple[float, ...]) -> np.ndarray:
    """정규화 좌표 (N, 2) 에 OpenCV 왜곡 모델을 적용한다 (cv2.projectPoints 와 같은 식).

    OPENCV_PINHOLE: [k1 k2 p1 p2 (k3 (k4 k5 k6))], OPENCV_FISHEYE: [k1 k2 k3 k4], NONE: 그대로.
    역변환은 `geometry.rig.RigCamera.normalize` 다.
    """
    xy = np.asarray(xy, dtype=float).reshape(-1, 2)
    if model == "NONE" or not coeffs:
        return xy.copy()
    x, y = xy[:, 0], xy[:, 1]
    if model == "OPENCV_PINHOLE":
        k1, k2, p1, p2, k3, k4, k5, k6 = list(coeffs) + [0.0] * (8 - len(coeffs))
        r2 = x * x + y * y
        radial = (1 + ((k3 * r2 + k2) * r2 + k1) * r2) / (1 + ((k6 * r2 + k5) * r2 + k4) * r2)
        return np.column_stack(
            [
                x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
                y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y,
            ]
        )
    if model == "OPENCV_FISHEYE":
        k1, k2, k3, k4 = coeffs
        r = np.hypot(x, y)
        theta = np.arctan(r)
        t2 = theta * theta
        theta_d = theta * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4))))
        scale = np.ones_like(r)
        nz = r > 1e-12
        scale[nz] = theta_d[nz] / r[nz]
        return xy * scale[:, None]
    raise ValueError(f"모르는 왜곡 모델 {model}")


def project_floor_points(cam: RigCamera, points: np.ndarray, config: BevConfig) -> tuple[np.ndarray, np.ndarray]:
    """World 점 (N, 3) → 원본 픽셀 (N, 2) 와 유효 여부 (N,). 카메라 뒤·fold-over 는 무효."""
    pc = cam.to_camera(points)
    z = pc[:, 2]
    front = z > config.near_m
    xn = np.zeros((len(points), 2))
    xn[front] = pc[front, :2] / z[front, None]
    if config.undistort:
        xd = distort_normalized(xn, cam.distortion_model, cam.distortion_coeffs)
    else:
        xd = xn
    uv = cam.ideal_pixels(xd)
    ok = front & np.isfinite(uv).all(axis=1)
    if config.undistort and cam.distortion_model != "NONE" and ok.any():
        back = cam.normalize(uv[ok])
        fx = cam.K[0, 0]
        err_px = np.hypot(*(back - xn[ok]).T) * fx
        good = np.zeros(len(points), dtype=bool)
        good[np.flatnonzero(ok)] = err_px <= config.roundtrip_tolerance_px
        ok = good
    return uv, ok


def _camera_samples(
    cam: RigCamera,
    points: np.ndarray,
    config: BevConfig,
    floor_mask: np.ndarray | None,
    exclude_mask: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    """카메라 한 대가 바닥 점마다 줄 원본 픽셀 위치와 합성 가중치(0 = 쓰지 않음)."""
    uv, ok = project_floor_points(cam, points, config)
    w_img, h_img = cam.image_size
    u, v = uv[:, 0], uv[:, 1]
    with np.errstate(invalid="ignore"):
        inside = ok & (u >= 0.0) & (u <= w_img - 1) & (v >= 0.0) & (v <= h_img - 1)
    delta = points - cam.center
    dist = np.linalg.norm(delta, axis=1)
    horiz = np.hypot(delta[:, 0], delta[:, 1])
    sin_elev = np.divide(cam.center[2] - points[:, 2], dist, out=np.zeros_like(dist), where=dist > 0)
    valid = inside & (horiz <= config.max_ground_distance_m) & (sin_elev >= math.sin(config.min_elevation_rad))
    if floor_mask is not None:
        valid &= _sample_mask(floor_mask, uv, valid)
    if exclude_mask is not None:
        valid &= ~_sample_mask(exclude_mask, uv, valid)
    weight = np.zeros(len(points))
    if not valid.any():
        return uv, weight
    w = np.power(sin_elev[valid], config.elevation_power)
    w = w / np.power(np.maximum(dist[valid], 1e-6), config.distance_power)
    if config.feather_px > 0.0:
        edge = np.minimum.reduce([u[valid], w_img - 1 - u[valid], v[valid], h_img - 1 - v[valid]])
        w = w * np.clip(edge / config.feather_px, 0.0, 1.0)
    weight[valid] = w
    return uv, weight


def _bilinear(image: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """(H, W, C) 영상에서 연속 픽셀 (u, v)(정수 = 픽셀 중심) 을 쌍선형 보간한다. uv 는 영상 안이어야 한다."""
    h, w = image.shape[:2]
    u = np.clip(uv[:, 0], 0.0, w - 1)
    v = np.clip(uv[:, 1], 0.0, h - 1)
    u0 = np.minimum(np.floor(u).astype(np.int64), max(w - 2, 0))
    v0 = np.minimum(np.floor(v).astype(np.int64), max(h - 2, 0))
    u1 = np.minimum(u0 + 1, w - 1)
    v1 = np.minimum(v0 + 1, h - 1)
    du = (u - u0)[:, None]
    dv = (v - v0)[:, None]
    top = image[v0, u0] * (1 - du) + image[v0, u1] * du
    bottom = image[v1, u0] * (1 - du) + image[v1, u1] * du
    return top * (1 - dv) + bottom * dv


def _sample_mask(mask: np.ndarray, uv: np.ndarray, where: np.ndarray) -> np.ndarray:
    """마스크를 가장 가까운 픽셀로 읽는다. where 가 거짓인 곳은 False."""
    out = np.zeros(len(uv), dtype=bool)
    if where.any():
        h, w = mask.shape
        cols = np.clip(np.rint(uv[where, 0]).astype(np.int64), 0, w - 1)
        rows = np.clip(np.rint(uv[where, 1]).astype(np.int64), 0, h - 1)
        out[where] = mask[rows, cols]
    return out


def _auto_raster(cameras: list[RigCamera], config: BevConfig, floor: Mapping[str, np.ndarray]) -> BevRaster:
    """원본 영상 표본 픽셀의 광선이 바닥과 만나는 점들(설정의 거리·시선각 조건 안)을 덮는 범위."""
    found: list[np.ndarray] = []
    step = config.auto_bounds_step_px
    for cam in cameras:
        w_img, h_img = cam.image_size
        us = np.unique(np.append(np.arange(0, w_img, step), w_img - 1)).astype(float)
        vs = np.unique(np.append(np.arange(0, h_img, step), h_img - 1)).astype(float)
        gu, gv = np.meshgrid(us, vs)
        uv = np.column_stack([gu.ravel(), gv.ravel()])
        if cam.camera_id in floor:
            mask = floor[cam.camera_id]
            uv = uv[mask[uv[:, 1].astype(int), uv[:, 0].astype(int)]]
        if len(uv) == 0:
            continue
        if config.undistort:
            xn = cam.normalize(uv)
        else:
            homo = np.column_stack([uv, np.ones(len(uv))]) @ np.linalg.inv(cam.K).T
            xn = homo[:, :2] / homo[:, 2:3]
        d = np.column_stack([xn, np.ones(len(xn))]) @ cam.R  # R⁻¹ = Rᵀ → (Rᵀ d)ᵀ = dᵀ R
        dz = d[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            s = -cam.center[2] / dz
        hit = (dz < 0) & np.isfinite(s) & (s > 0)
        p = cam.center + s[hit, None] * d[hit]
        delta = p - cam.center
        horiz = np.hypot(delta[:, 0], delta[:, 1])
        sin_elev = cam.center[2] / np.linalg.norm(delta, axis=1)
        keep = (horiz <= config.max_ground_distance_m) & (sin_elev >= math.sin(config.min_elevation_rad))
        found.append(p[keep, :2])
    pts = np.concatenate(found) if found else np.zeros((0, 2))
    if len(pts) == 0:
        raise BevInputError("어느 카메라도 설정 조건 안에서 바닥을 보지 않는다. world_bounds 를 직접 준다")
    m = config.meters_per_pixel
    lo = np.floor(pts.min(axis=0) / m) * m - m
    hi = np.ceil(pts.max(axis=0) / m) * m + m
    return BevRaster.from_bounds((float(lo[0]), float(lo[1])), (float(hi[0]), float(hi[1])), m)


def _frames_by_camera(frames: Mapping[str, Frame] | Iterable[Frame]) -> dict[str, Frame]:
    items = list(frames.values()) if isinstance(frames, Mapping) else list(frames)
    if isinstance(frames, Mapping):
        for key, frame in frames.items():
            if key != frame.camera_id:
                raise BevInputError(f"frames 키 {key} 와 Frame.camera_id {frame.camera_id} 가 다르다")
    out: dict[str, Frame] = {}
    for frame in items:
        if frame.camera_id in out:
            raise BevInputError(f"같은 카메라 프레임이 두 장이다: {frame.camera_id}")
        if frame.image is None:
            raise BevInputError(f"{frame.camera_id}: 영상(image)이 없는 프레임이다")
        out[frame.camera_id] = frame
    if not out:
        raise BevInputError("프레임이 없다")
    return out


def _check_image(cam: RigCamera, image: Any) -> np.ndarray:
    img = np.asarray(image)
    if img.ndim == 2:
        img = img[:, :, None]
    if img.ndim != 3:
        raise BevInputError(f"{cam.camera_id}: 영상은 (H, W) 또는 (H, W, C) 여야 한다: {img.shape}")
    w_img, h_img = cam.image_size
    if img.shape[:2] != (h_img, w_img):
        raise BevInputError(
            f"{cam.camera_id}: 영상 크기 {img.shape[1]}x{img.shape[0]} 가 보정 image_size {w_img}x{h_img} 와 다르다"
        )
    if not (np.issubdtype(img.dtype, np.integer) or np.issubdtype(img.dtype, np.floating)):
        raise BevInputError(f"{cam.camera_id}: 숫자 영상이 아니다: {img.dtype}")
    return img


def _check_masks(cameras: list[RigCamera], masks: Mapping[str, np.ndarray] | None, name: str) -> dict[str, np.ndarray]:
    if not masks:
        return {}
    known = {cam.camera_id: cam for cam in cameras}
    out: dict[str, np.ndarray] = {}
    for cid, mask in masks.items():
        if cid not in known:
            raise BevInputError(f"{name}: 프레임이 없는 카메라 {cid}")
        arr = np.asarray(mask, dtype=bool)
        w_img, h_img = known[cid].image_size
        if arr.shape != (h_img, w_img):
            raise BevInputError(f"{name}[{cid}]: 크기 {arr.shape} 가 영상 크기 ({h_img}, {w_img}) 와 다르다")
        out[cid] = arr
    return out


def _fill_from_static(
    image: np.ndarray, coverage: np.ndarray, layer: StaticSceneLayer, raster: BevRaster
) -> tuple[dict[str, Any], np.ndarray]:
    top, mask = layer.render_top_down(raster)
    top = np.asarray(top)
    if top.ndim == 2:
        top = top[:, :, None]
    mask = np.asarray(mask, dtype=bool)
    if top.shape != image.shape or mask.shape != coverage.shape:
        raise BevInputError(f"정적 3D 층 크기 {top.shape}/{mask.shape} 가 BEV {image.shape} 와 다르다")
    fill = mask & ~coverage
    image[fill] = top[fill]
    meta = {"scene_version": layer.scene_version, "use": "FILL_UNCOVERED", "filled_ratio": float(fill.mean())}
    return meta, fill


def _frame_metadata(frame: Frame) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "camera_id": frame.camera_id,
        "estimated_capture_time": frame.estimated_capture_time,
        "timestamp_source": frame.timestamp_source,
        "frame_ref": dict(frame.frame_ref),
    }
    if frame.timestamp_uncertainty_ms is not None:
        meta["timestamp_uncertainty_ms"] = frame.timestamp_uncertainty_ms
    return meta
