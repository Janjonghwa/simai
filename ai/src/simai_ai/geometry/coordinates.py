"""경계별 좌표 어댑터 (명세 4.2).

World 는 미터, 오른손, +Z 상방, 바닥 Z=0 이다(명세 4.1). 다른 좌표계와의 변환은 이 모듈에서만 한다.

| 대상 | 규약 | World (x, y, z) 로부터 |
|---|---|---|
| 카메라 | OpenCV: x 오른쪽, y 아래, z 앞 | x_cam = R · x_world + t |
| Three.js · glTF | 미터, 오른손, +Y 상방 | (x, z, −y) |
| Unreal | 센티미터, 왼손, +Z 상방 | (100x, −100y, 100z) |

픽셀 좌표는 왜곡을 제거한 이상적 핀홀 기준이다. 렌즈 왜곡(보정 계약의 `distortion`)의 적용·제거는
이 모듈이 하지 않는다. 표준 라이브러리만 쓴다(기본 설치에서도 import 된다).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]
Pixel = tuple[float, float]

# 축 변환 행렬 (행 우선). 뷰어(front)는 이 정의를 그대로 옮겨 쓴다.
# Three.js: 오른손을 유지하므로 det = +1.
WORLD_TO_THREEJS: Mat3 = (
    (1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, -1.0, 0.0),
)
THREEJS_TO_WORLD: Mat3 = (
    (1.0, 0.0, 0.0),
    (0.0, 0.0, -1.0),
    (0.0, 1.0, 0.0),
)
# Unreal: 미터 → 센티미터, Y 부호를 뒤집어 왼손이 된다(det < 0).
METERS_TO_UNREAL_CM = 100.0
WORLD_TO_UNREAL: Mat3 = (
    (METERS_TO_UNREAL_CM, 0.0, 0.0),
    (0.0, -METERS_TO_UNREAL_CM, 0.0),
    (0.0, 0.0, METERS_TO_UNREAL_CM),
)
UNREAL_TO_WORLD: Mat3 = (
    (1.0 / METERS_TO_UNREAL_CM, 0.0, 0.0),
    (0.0, -1.0 / METERS_TO_UNREAL_CM, 0.0),
    (0.0, 0.0, 1.0 / METERS_TO_UNREAL_CM),
)

# 보정 계약의 R 은 소수 몇 자리로 반올림될 수 있다(contracts/examples/calibration.json). 직교성 허용 오차.
ROTATION_TOLERANCE = 1e-3


def world_to_threejs(p: Sequence[float]) -> Vec3:
    """World (x, y, z) → Three.js (x, z, −y)."""
    x, y, z = _vec3(p)
    return (x, z, -y)


def threejs_to_world(p: Sequence[float]) -> Vec3:
    """Three.js (x, y, z) → World (x, −z, y)."""
    x, y, z = _vec3(p)
    return (x, -z, y)


def world_to_unreal(p: Sequence[float]) -> Vec3:
    """World 미터 → Unreal 센티미터 (100x, −100y, 100z)."""
    x, y, z = _vec3(p)
    return (x * METERS_TO_UNREAL_CM, -y * METERS_TO_UNREAL_CM, z * METERS_TO_UNREAL_CM)


def unreal_to_world(p: Sequence[float]) -> Vec3:
    """Unreal 센티미터 → World 미터 (x/100, −y/100, z/100)."""
    x, y, z = _vec3(p)
    return (x / METERS_TO_UNREAL_CM, -y / METERS_TO_UNREAL_CM, z / METERS_TO_UNREAL_CM)


@dataclass(frozen=True)
class PinholeCamera:
    """OpenCV 규약 핀홀 카메라 한 대. x_cam = R · x_world + t (보정 계약 `OPENCV_WORLD_TO_CAMERA`).

    K 는 [[fx, s, cx], [0, fy, cy], [0, 0, 1]], R·t 는 World → 카메라, t 는 미터.
    """

    K: Mat3
    R: Mat3
    t: Vec3
    _R_inv: Mat3 = field(init=False, repr=False, compare=False)
    _K_inv: Mat3 = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        K, R, t = _mat3(self.K), _mat3(self.R), _vec3(self.t)
        # NaN 은 아래 비교 검사를 그냥 통과하고, ±inf K 는 RigCamera 의 SVD(행렬 노름)를 Linux 에서 멈춘다(S15P31A101-245)
        if not all(math.isfinite(x) for row in (*K, *R, t) for x in row):
            raise ValueError(f"K·R·t 는 유한한 값이어야 한다: K={K}, R={R}, t={t}")
        _check_intrinsics(K)
        _check_rotation(R)
        object.__setattr__(self, "K", K)
        object.__setattr__(self, "R", R)
        object.__setattr__(self, "t", t)
        # 반올림된 R 에서도 왕복이 맞도록 전치가 아닌 실제 역행렬을 쓴다.
        object.__setattr__(self, "_R_inv", _inverse(R))
        object.__setattr__(self, "_K_inv", _inverse(K))
        # 유한한 R·t 에서도 중심 −R⁻¹·t 가 넘칠 수 있다(t ≈ 1.7e308). 중심은 시선각·재보정 방향에 쓰인다
        if not all(math.isfinite(x) for x in self.center_world):
            raise ValueError(f"카메라 중심 −R⁻¹·t 가 유한하지 않다(넘침): R={R}, t={t}")

    @classmethod
    def from_calibration(cls, camera: dict) -> PinholeCamera:
        """보정 계약(contracts/providers/calibration.schema.json)의 `cameras[]` 항목 하나로 만든다."""
        return cls(K=camera["K"], R=camera["R"], t=camera["t"])

    @property
    def center_world(self) -> Vec3:
        """카메라 중심의 World 좌표 C = −R⁻¹ · t."""
        return _neg(_matvec(self._R_inv, self.t))

    def world_to_camera(self, p: Sequence[float]) -> Vec3:
        return _add(_matvec(self.R, _vec3(p)), self.t)

    def camera_to_world(self, p: Sequence[float]) -> Vec3:
        return _matvec(self._R_inv, _sub(_vec3(p), self.t))

    def project(self, p: Sequence[float]) -> Pixel | None:
        """World 점 → 픽셀 (u, v). 카메라 앞(z_cam > 0)이 아니면 투영값을 만들지 않고 None."""
        xc, yc, zc = self.world_to_camera(p)
        if zc <= 0.0:
            return None
        u, v, w = _matvec(self.K, (xc / zc, yc / zc, 1.0))
        return (u / w, v / w)

    def unproject(self, uv: Sequence[float], depth: float) -> Vec3:
        """픽셀 (u, v) 와 카메라 z 깊이(미터, > 0) → World 점."""
        if not depth > 0.0:
            raise ValueError(f"깊이는 양수여야 한다: {depth}")
        xn, yn, _ = self._normalized(uv)
        return self.camera_to_world((xn * depth, yn * depth, depth))

    def pixel_ray(self, uv: Sequence[float]) -> tuple[Vec3, Vec3]:
        """픽셀 (u, v) 를 지나는 World 광선 (원점=카메라 중심, 단위 방향)."""
        d_cam = self._normalized(uv)
        d_world = _matvec(self._R_inv, d_cam)
        n = math.sqrt(_dot(d_world, d_world))
        return self.center_world, (d_world[0] / n, d_world[1] / n, d_world[2] / n)

    def _normalized(self, uv: Sequence[float]) -> Vec3:
        if len(uv) != 2:
            raise ValueError(f"픽셀은 (u, v) 두 값이어야 한다: {uv!r}")
        x, y, w = _matvec(self._K_inv, (float(uv[0]), float(uv[1]), 1.0))
        return (x / w, y / w, 1.0)


def _check_intrinsics(K: Mat3) -> None:
    if K[1][0] != 0.0 or K[2] != (0.0, 0.0, 1.0):
        raise ValueError(f"K 는 [[fx, s, cx], [0, fy, cy], [0, 0, 1]] 형식이어야 한다: {K}")
    if not (K[0][0] > 0.0 and K[1][1] > 0.0):
        raise ValueError(f"초점거리 fx, fy 는 양수여야 한다: {K}")


def _check_rotation(R: Mat3, tol: float = ROTATION_TOLERANCE) -> None:
    for i in range(3):
        for j in range(3):
            # R · Rᵀ = I
            expected = 1.0 if i == j else 0.0
            if abs(_dot(R[i], R[j]) - expected) > tol:
                raise ValueError(f"R 이 직교 행렬이 아니다: {R}")
    if abs(_det(R) - 1.0) > tol:
        raise ValueError(f"R 의 행렬식이 +1 이 아니다(반사 포함): det={_det(R)}")


def _vec3(p: Sequence[float]) -> Vec3:
    if len(p) != 3:
        raise ValueError(f"3차원 좌표여야 한다: {p!r}")
    return (float(p[0]), float(p[1]), float(p[2]))


def _mat3(m: Sequence[Sequence[float]]) -> Mat3:
    if len(m) != 3:
        raise ValueError(f"3x3 행렬이어야 한다: {m!r}")
    return (_vec3(m[0]), _vec3(m[1]), _vec3(m[2]))


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _neg(a: Vec3) -> Vec3:
    return (-a[0], -a[1], -a[2])


def _matvec(m: Mat3, v: Vec3) -> Vec3:
    return (_dot(m[0], v), _dot(m[1], v), _dot(m[2], v))


def _det(m: Mat3) -> float:
    (a, b, c), (d, e, f), (g, h, i) = m
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _inverse(m: Mat3) -> Mat3:
    (a, b, c), (d, e, f), (g, h, i) = m
    det = _det(m)
    if det == 0.0:
        raise ValueError(f"역행렬이 없는 행렬이다: {m}")
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )
