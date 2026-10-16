"""가상 핀홀 카메라. 외부 파라미터는 OpenCV 규약(x_cam = R · x_world + t, x 오른쪽·y 아래·z 앞)."""

import math
from dataclasses import dataclass

from sim import vec
from sim.scenario import CameraSpec
from sim.vec import Vec3

WORLD_UP: Vec3 = (0.0, 0.0, 1.0)
NEAR_M = 0.05
"""카메라 앞 이 거리보다 가까운 점은 투영하지 않는다."""


@dataclass(frozen=True)
class VirtualCamera:
    spec: CameraSpec
    K: tuple[Vec3, Vec3, Vec3]
    R: tuple[Vec3, Vec3, Vec3]
    t: Vec3

    @property
    def camera_id(self) -> str:
        return self.spec.camera_id

    @property
    def center(self) -> Vec3:
        return self.spec.position

    def to_camera(self, p: Vec3) -> Vec3:
        return vec.add(vec.mat_vec(self.R, p), self.t)

    def project(self, p: Vec3) -> tuple[float, float] | None:
        """World 점을 픽셀 (u, v)로 투영한다. 카메라 뒤(또는 NEAR_M 이내)면 None."""
        xc, yc, zc = self.to_camera(p)
        if zc <= NEAR_M:
            return None
        x, y = _distort(xc / zc, yc / zc, self.spec.distortion_model, self.spec.distortion_coeffs)
        fx, cx = self.K[0][0], self.K[0][2]
        fy, cy = self.K[1][1], self.K[1][2]
        return (fx * x + cx, fy * y + cy)

    def in_image(self, uv: tuple[float, float]) -> bool:
        w, h = self.spec.image_size
        return 0.0 <= uv[0] < w and 0.0 <= uv[1] < h


def build(spec: CameraSpec) -> VirtualCamera:
    w, h = spec.image_size
    fx = (w / 2.0) / math.tan(spec.hfov_rad / 2.0)
    K = ((fx, 0.0, w / 2.0), (0.0, fx, h / 2.0), (0.0, 0.0, 1.0))
    z = vec.normalize(vec.sub(spec.look_at, spec.position))
    side = vec.cross(z, WORLD_UP)
    if vec.norm(side) < 1e-9:
        raise ValueError(f"{spec.camera_id}: 시선이 연직이라 자세를 정할 수 없다 (look_at 을 옮긴다)")
    x = vec.normalize(side)
    y = vec.cross(z, x)
    c, s = math.cos(spec.roll_rad), math.sin(spec.roll_rad)
    x, y = vec.add(vec.scale(x, c), vec.scale(y, s)), vec.add(vec.scale(x, -s), vec.scale(y, c))
    R = (x, y, z)
    t = vec.scale(vec.mat_vec(R, spec.position), -1.0)
    return VirtualCamera(spec=spec, K=K, R=R, t=t)


def _distort(x: float, y: float, model: str, k: tuple[float, ...]) -> tuple[float, float]:
    """정규화 이미지 좌표에 OpenCV 왜곡 모델을 적용한다 (cv2.projectPoints 와 같은 식)."""
    if model == "NONE":
        return x, y
    if model == "OPENCV_PINHOLE":
        k1, k2, p1, p2, k3 = k[:5]
        r2 = x * x + y * y
        radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        return (
            x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
            y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y,
        )
    # OPENCV_FISHEYE
    r = math.hypot(x, y)
    if r < 1e-12:
        return x, y
    th = math.atan(r)
    th2 = th * th
    th_d = th * (1 + k[0] * th2 + k[1] * th2**2 + k[2] * th2**3 + k[3] * th2**4)
    return x * th_d / r, y * th_d / r
