"""구역별 관측 카메라(R7 입력 zone_cameras, S15P31A101-171). 모서리 카메라 4대(sim basic 배치)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.zone_coverage import zone_cameras
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.risk import Zone

AI_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cov")
    out = tmp / "basic"
    subprocess.run([sys.executable, "-m", "sim.run", "--scenario", "basic", "--out", str(out), "--seed", "1"],
                   cwd=AI_DIR, check=True, capture_output=True)
    return CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))


def zone(zone_id, polygon, z_range=None):
    return Zone.from_json({"zone_id": zone_id, "zone_version": "v1", "judge_type": "VOLUME_3D" if z_range else
                           "GROUND_FOOTPRINT", "polygon_xy": polygon, "z_range": z_range, "severity": "HIGH"})


def test_center_zone_is_seen_by_all_corner_cameras(rig):
    cov = zone_cameras(rig, [zone("center", [[4.0, 3.2], [5.0, 3.2], [5.0, 4.2], [4.0, 4.2]])])
    assert cov == {"center": ("cam1", "cam2", "cam3", "cam4")}


def test_zone_behind_cameras_is_not_covered_by_them(rig):
    """cam1(0.3, 0.3)·cam2(8.7, 0.3) 는 교실 안쪽(+y)을 본다. 그 뒤(y < 0)의 구역은 두 카메라가 담지 않는다."""
    cov = zone_cameras(rig, [zone("behind", [[4.0, -6.0], [5.0, -6.0], [5.0, -5.0], [4.0, -5.0]])])
    assert "cam1" not in cov["behind"] and "cam2" not in cov["behind"]


def test_zone_partly_out_of_view_is_not_counted(rig):
    """카메라 바로 아래 모서리 구역: 꼭짓점 일부가 그 카메라 시야 밖이면 그 카메라는 세지 않는다(보수적)."""
    near = zone("corner", [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    cov = zone_cameras(rig, [near])
    assert "cam1" not in cov["corner"]


def test_versions_of_the_same_zone_intersect(rig):
    a = zone("z", [[4.0, 3.2], [5.0, 3.2], [5.0, 4.2], [4.0, 4.2]])
    b = zone("z", [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    assert set(zone_cameras(rig, [a, b])["z"]) == set(zone_cameras(rig, [b])["z"])


# --- 렌즈 왜곡 (리뷰 F4): 실제 왜곡 투영으로 판정하고, 보수성을 보장할 수 없는 계수는 '미확인'으로 뺀다 ---

import numpy as np  # noqa: E402

from simai_ai.geometry.coordinates import PinholeCamera  # noqa: E402
from simai_ai.geometry.distortion import distort  # noqa: E402
from simai_ai.geometry.rig import RigCamera  # noqa: E402
from simai_ai.geometry.zone_coverage import camera_sees_points  # noqa: E402

K = ((500.0, 0.0, 640.0), (0.0, 500.0, 360.0), (0.0, 0.0, 1.0))
DOWN = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))  # 2.8 m 높이에서 바로 아래를 본다


def down_camera(model="OPENCV_PINHOLE", coeffs=(0.0, 0.0, 0.0, 0.0)) -> RigCamera:
    return RigCamera("cam1", PinholeCamera(K, DOWN, (0.0, 0.0, 2.8)), (1280, 720), model, tuple(coeffs))


def prism(x0, x1, y0=-0.01, y1=0.01) -> np.ndarray:
    return np.array([[x, y, z] for z in (0.0, 1.8) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])


def test_positive_radial_distortion_pushes_zone_out_of_view() -> None:
    """리뷰 부록 D: 이상적 핀홀로는 U 1255~1265(안)이지만 실제 k1=+0.1 투영은 1348~1363(영상 폭 1280 밖)이다."""
    points = prism(1.23, 1.25)
    cam = down_camera(coeffs=(0.1, 0.0, 0.0, 0.0))
    xc = cam.to_camera(points)
    real_u = cam.ideal_pixels(distort(xc[:, :2] / xc[:, 2:3], cam.distortion_model, cam.distortion_coeffs))[:, 0]
    assert real_u.max() > 1280
    assert not camera_sees_points(cam, points)
    assert camera_sees_points(down_camera(), points)  # 왜곡이 없으면 같은 구역이 영상 안


def test_barrel_distortion_near_edge_counts_only_when_really_inside() -> None:
    """술통형(k1 < 0)은 바깥 점을 안으로 당긴다: 이상적 투영이 가장자리 밖이어도 실제 투영이 안이면 센다."""
    cam = down_camera(coeffs=(-0.1, 0.0, 0.0, 0.0))
    inside_real = prism(1.40, 1.42)
    xc = cam.to_camera(inside_real)
    xn = xc[:, :2] / xc[:, 2:3]
    assert cam.ideal_pixels(xn)[:, 0].max() > 1280  # 이상적 핀홀로는 밖
    assert cam.ideal_pixels(distort(xn, cam.distortion_model, cam.distortion_coeffs))[:, 0].max() < 1278
    assert camera_sees_points(cam, inside_real)


def test_tangential_distortion_is_unverified_and_not_counted() -> None:
    center = prism(-0.05, 0.05)
    assert camera_sees_points(down_camera(coeffs=(0.0, 0.0, 0.0, 0.0)), center)
    assert not camera_sees_points(down_camera(coeffs=(0.0, 0.0, 0.001, 0.0)), center)
    assert not camera_sees_points(down_camera(coeffs=(0.0, 0.0, 0.0, -0.002)), center)


def test_points_beyond_the_monotone_branch_are_not_counted() -> None:
    """강한 술통형은 가지 끝 너머에서 접혀 먼 광선이 영상 안으로 돌아온다. 가지 밖이면 세지 않는다."""
    cam = down_camera(coeffs=(-0.5, 0.0, 0.0, 0.0))  # 가지 끝 r = 1/sqrt(1.5) ≈ 0.816
    far = np.array([[x, y, z] for z in (0.0, 0.2) for x, y in ((2.6, -0.01), (2.62, -0.01), (2.62, 0.01), (2.6, 0.01))])
    # 정규화 반지름 ≈ 0.93~1.01: 가지 끝(0.816) 너머, 왜곡 식이 접힌 곳
    xc = cam.to_camera(far)
    real = cam.ideal_pixels(distort(xc[:, :2] / xc[:, 2:3], cam.distortion_model, cam.distortion_coeffs))
    assert np.all((real[:, 0] > 0) & (real[:, 0] < 1280))  # 접혀서 영상 안처럼 보인다
    assert not camera_sees_points(cam, far)


def test_zone_edge_bulging_out_between_vertices_is_caught() -> None:
    """술통형(k1 < 0)은 먼 꼭짓점을 더 당긴다: 가로로 긴 구역의 꼭짓점은 영상 안(v ≈ 56)인데 위 변 가운데는 밖(v < 0)이다.
    꼭짓점만 보면(edge_samples=1, 음성 대조) 세고, 경계 표본으로 보면 세지 않는다."""
    cam = down_camera(coeffs=(-0.15, 0.0, 0.0, 0.0))
    zone_pts = np.array([[x, y, z] for z in (0.0, 1.8) for x, y in ((-1.0, 0.79), (1.0, 0.79), (1.0, 0.81), (-1.0, 0.81))])
    mid = cam.to_camera(np.array([[0.0, 0.81, 1.8]]))
    assert cam.ideal_pixels(distort(mid[:, :2] / mid[:, 2:3], cam.distortion_model, cam.distortion_coeffs))[0, 1] < 0
    assert camera_sees_points(cam, zone_pts, edge_samples=1)  # 꼭짓점만: 잘못 센다
    assert not camera_sees_points(cam, zone_pts)
