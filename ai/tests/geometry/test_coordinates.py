"""좌표 어댑터 왕복·축·부호 테스트 (명세 4.2: 격자점을 왕복 변환해 원래 값으로 돌아오는지 확인).

extras 없는 기본 설치에서 돌아야 한다(어댑터는 표준 라이브러리만 쓴다).
"""

import itertools
import json
import math
from pathlib import Path

import pytest

from simai_ai.geometry.coordinates import (
    THREEJS_TO_WORLD,
    UNREAL_TO_WORLD,
    WORLD_TO_THREEJS,
    WORLD_TO_UNREAL,
    PinholeCamera,
    threejs_to_world,
    unreal_to_world,
    world_to_threejs,
    world_to_unreal,
)

TOL_M = 1e-9
REPO_DIR = Path(__file__).resolve().parents[3]
CALIBRATION_EXAMPLE = REPO_DIR / "contracts" / "examples" / "calibration.json"

ORIGIN = (0.0, 0.0, 0.0)
PLUS_X = (1.0, 0.0, 0.0)
PLUS_Y = (0.0, 1.0, 0.0)
PLUS_Z = (0.0, 0.0, 1.0)

# 10 m × 8 m 교실, 천장 3 m 를 넘는 범위까지. 소수·음수 좌표를 섞는다.
GRID = [
    (x, y, z)
    for x, y, z in itertools.product(
        [-1.25, 0.0, 0.5, 1.0, 3.3, 7.77, 10.0],
        [-0.4, 0.0, 1.0, 2.5, 6.1, 8.0],
        [0.0, 0.05, 1.0, 1.7, 3.0],
    )
]
# 카메라 테스트용 격자: 교실 안쪽 (카메라 4대 모두에서 앞쪽)
ROOM_GRID = [
    (x, y, z)
    for x, y, z in itertools.product([1.0, 2.5, 5.0, 7.5, 9.0], [1.0, 3.0, 4.0, 5.5, 7.0], [0.0, 0.9, 1.7, 2.2])
]


def _dist(a, b):
    return math.dist(a, b)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _matvec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def _matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _det(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _look_at(eye, target, K=((1000.0, 0.0, 960.0), (0.0, 1000.0, 540.0), (0.0, 0.0, 1.0))):
    """World 의 eye 에서 target 을 보는 OpenCV 카메라. +Z 상방이 화면 위쪽(−y_cam)이 되게 한다."""
    f = [target[i] - eye[i] for i in range(3)]
    n = math.sqrt(sum(c * c for c in f))
    z_cam = tuple(c / n for c in f)
    x_cam = _cross(z_cam, PLUS_Z)
    n = math.sqrt(sum(c * c for c in x_cam))
    x_cam = tuple(c / n for c in x_cam)
    y_cam = _cross(z_cam, x_cam)
    R = (x_cam, y_cam, z_cam)
    t = tuple(-c for c in _matvec(R, eye))
    return PinholeCamera(K=K, R=R, t=t)


def _example_cameras():
    """교실 네 모서리 2.7 m 높이에서 교실 중앙 바닥 쪽을 내려다보는 카메라 4대."""
    target = (5.0, 4.0, 0.5)
    corners = [(0.2, 0.2, 2.7), (9.8, 0.2, 2.7), (9.8, 7.8, 2.7), (0.2, 7.8, 2.7)]
    return [_look_at(c, target) for c in corners]


# --- Three.js ---


def test_threejs_reference_points():
    assert world_to_threejs(ORIGIN) == (0.0, 0.0, 0.0)
    assert world_to_threejs(PLUS_X) == (1.0, 0.0, 0.0)
    assert world_to_threejs(PLUS_Y) == (0.0, 0.0, -1.0)
    # World +Z 상방은 Three.js +Y 상방이 된다
    assert world_to_threejs(PLUS_Z) == (0.0, 1.0, 0.0)


def test_threejs_keeps_right_handed():
    ex, ey, ez = (world_to_threejs(p) for p in (PLUS_X, PLUS_Y, PLUS_Z))
    assert _cross(ex, ey) == ez
    assert _det(WORLD_TO_THREEJS) == 1.0


@pytest.mark.parametrize("p", GRID)
def test_threejs_round_trip(p):
    assert _dist(threejs_to_world(world_to_threejs(p)), p) <= TOL_M
    assert _dist(world_to_threejs(threejs_to_world(p)), p) <= TOL_M


def test_threejs_matrix_matches_functions():
    for p in GRID:
        assert _matvec(WORLD_TO_THREEJS, p) == world_to_threejs(p)
        assert _matvec(THREEJS_TO_WORLD, p) == threejs_to_world(p)
    assert _matmul(THREEJS_TO_WORLD, WORLD_TO_THREEJS) == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


# --- Unreal ---


def test_unreal_reference_points():
    assert world_to_unreal(ORIGIN) == (0.0, 0.0, 0.0)
    assert world_to_unreal(PLUS_X) == (100.0, 0.0, 0.0)
    assert world_to_unreal(PLUS_Y) == (0.0, -100.0, 0.0)
    assert world_to_unreal(PLUS_Z) == (0.0, 0.0, 100.0)


def test_unreal_flips_handedness():
    ex, ey, ez = (world_to_unreal(p) for p in (PLUS_X, PLUS_Y, PLUS_Z))
    # 왼손 좌표계: 오른손 외적 공식으로 X×Y 를 구하면 Z 의 반대가 나온다
    assert _cross(ex, ey) == tuple(-100.0 * c for c in ez)
    assert _det(WORLD_TO_UNREAL) < 0.0


@pytest.mark.parametrize("p", GRID)
def test_unreal_round_trip(p):
    assert _dist(unreal_to_world(world_to_unreal(p)), p) <= TOL_M
    p_cm = tuple(100.0 * c for c in p)
    # Unreal 쪽 왕복 오차도 미터로 환산해 본다
    assert _dist(world_to_unreal(unreal_to_world(p_cm)), p_cm) / 100.0 <= TOL_M


def test_unreal_matrix_matches_functions():
    for p in GRID:
        assert _dist(_matvec(WORLD_TO_UNREAL, p), world_to_unreal(p)) == 0.0
        assert _dist(_matvec(UNREAL_TO_WORLD, world_to_unreal(p)), p) <= TOL_M


# --- OpenCV 카메라 ---


def _forward_camera():
    """World (5, 0, 1.5) 에서 +Y 방향 수평을 보는 카메라. 카메라 x=+X, y=−Z, z=+Y."""
    R = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))
    eye = (5.0, 0.0, 1.5)
    t = tuple(-c for c in _matvec(R, eye))
    return PinholeCamera(K=((800.0, 0.0, 640.0), (0.0, 800.0, 360.0), (0.0, 0.0, 1.0)), R=R, t=t)


def test_camera_axes_and_signs():
    cam = _forward_camera()
    assert _dist(cam.center_world, (5.0, 0.0, 1.5)) <= TOL_M
    # 정면 4 m 는 카메라 z(앞) = 4, 영상 중심에 찍힌다
    assert cam.world_to_camera((5.0, 4.0, 1.5)) == (0.0, 0.0, 4.0)
    assert cam.project((5.0, 4.0, 1.5)) == (640.0, 360.0)
    # World +X 는 카메라 x 오른쪽 → u 증가
    assert cam.world_to_camera((6.0, 4.0, 1.5)) == (1.0, 0.0, 4.0)
    assert cam.project((6.0, 4.0, 1.5)) == (840.0, 360.0)
    # World +Z(위) 는 카메라 y 아래의 반대 → v 감소
    assert cam.world_to_camera((5.0, 4.0, 2.5)) == (0.0, -1.0, 4.0)
    assert cam.project((5.0, 4.0, 2.5)) == (640.0, 160.0)


def test_project_returns_none_behind_camera():
    cam = _forward_camera()
    assert cam.project((5.0, -1.0, 1.5)) is None
    assert cam.project((5.0, 0.0, 1.5)) is None  # 카메라 중심 (z_cam = 0)


def test_camera_skew_projection():
    R = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    cam = PinholeCamera(K=((500.0, 2.0, 320.0), (0.0, 510.0, 240.0), (0.0, 0.0, 1.0)), R=R, t=(0.0, 0.0, 0.0))
    u, v = cam.project((0.2, 0.4, 2.0))
    assert abs(u - (500.0 * 0.1 + 2.0 * 0.2 + 320.0)) <= 1e-9
    assert abs(v - (510.0 * 0.2 + 240.0)) <= 1e-9
    assert _dist(cam.unproject((u, v), 2.0), (0.2, 0.4, 2.0)) <= TOL_M


@pytest.mark.parametrize("cam_index", range(4))
def test_camera_round_trip_grid(cam_index):
    cam = _example_cameras()[cam_index]
    for p in ROOM_GRID:
        c = cam.world_to_camera(p)
        assert _dist(cam.camera_to_world(c), p) <= TOL_M
        assert c[2] > 0.0, f"격자점 {p} 이 카메라 {cam_index} 뒤에 있다"
        uv = cam.project(p)
        assert uv is not None
        # 투영 → 깊이로 역투영
        assert _dist(cam.unproject(uv, c[2]), p) <= TOL_M
        # 픽셀 광선 위에 원래 점이 있다
        origin, d = cam.pixel_ray(uv)
        assert abs(math.hypot(*d) - 1.0) <= 1e-12
        s = sum((p[i] - origin[i]) * d[i] for i in range(3))
        assert s > 0.0
        assert _dist(tuple(origin[i] + s * d[i] for i in range(3)), p) <= TOL_M


def test_look_at_cameras_see_floor_below_horizon():
    """모서리 카메라가 바닥(Z=0)을 영상 아래쪽, 같은 x·y 의 높은 점을 그 위쪽에 찍는다."""
    for cam in _example_cameras():
        low = cam.project((5.0, 4.0, 0.0))
        high = cam.project((5.0, 4.0, 2.0))
        assert low is not None and high is not None
        assert high[1] < low[1]


def test_calibration_contract_example_round_trip():
    """보정 계약 예시(반올림된 R)로 만든 카메라도 왕복 오차가 1e-9 m 이하다."""
    calib = json.loads(CALIBRATION_EXAMPLE.read_text(encoding="utf-8"))
    assert calib["extrinsic_convention"] == "OPENCV_WORLD_TO_CAMERA"
    cam = PinholeCamera.from_calibration(calib["cameras"][0])
    in_front = 0
    for p in GRID:
        c = cam.world_to_camera(p)
        assert _dist(cam.camera_to_world(c), p) <= TOL_M
        uv = cam.project(p)
        if uv is None:
            continue
        in_front += 1
        assert _dist(cam.unproject(uv, c[2]), p) <= TOL_M
    assert in_front > 0


# --- 잘못된 입력 ---


@pytest.mark.parametrize(
    "R",
    [
        pytest.param(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)), id="reflection-det-minus-1"),
        pytest.param(((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), id="scaled"),
        pytest.param(((1.0, 0.1, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), id="sheared"),
    ],
)
def test_rejects_non_rotation(R):
    with pytest.raises(ValueError):
        PinholeCamera(K=((1000.0, 0.0, 960.0), (0.0, 1000.0, 540.0), (0.0, 0.0, 1.0)), R=R, t=(0.0, 0.0, 0.0))


@pytest.mark.parametrize(
    "K",
    [
        pytest.param(((0.0, 0.0, 960.0), (0.0, 1000.0, 540.0), (0.0, 0.0, 1.0)), id="zero-fx"),
        pytest.param(((1000.0, 0.0, 960.0), (0.0, -1000.0, 540.0), (0.0, 0.0, 1.0)), id="negative-fy"),
        pytest.param(((1000.0, 0.0, 960.0), (0.0, 1000.0, 540.0), (0.0, 0.0, 2.0)), id="bad-last-row"),
    ],
)
def test_rejects_bad_intrinsics(K):
    R = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    with pytest.raises(ValueError):
        PinholeCamera(K=K, R=R, t=(0.0, 0.0, 0.0))


@pytest.mark.parametrize("depth", [0.0, -1.0, float("nan")])
def test_unproject_rejects_non_positive_depth(depth):
    with pytest.raises(ValueError):
        _forward_camera().unproject((640.0, 360.0), depth)


def test_rejects_wrong_dimension():
    with pytest.raises(ValueError):
        world_to_threejs((1.0, 2.0))
    with pytest.raises(ValueError):
        world_to_unreal((1.0, 2.0, 3.0, 4.0))
    with pytest.raises(ValueError):
        _forward_camera().unproject((1.0, 2.0, 3.0), 1.0)
