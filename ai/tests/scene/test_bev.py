"""BEV 평면도 생성 시험 (명세 4.1 BEV 평면도, 요구사항 FR-22).

시뮬레이터 카메라 4대로 바닥에 사인 격자를 그린 합성 영상을 만들고, BEV 픽셀마다 무늬의 위상으로 되읽은
World 위치가 그 픽셀 중심과 맞는지(BEV 픽셀 오차) 본다. SIM 입력의 상한이며 허용 오차는 회귀용이다
(BEV 는 정확도 목표가 없는 보조 화면이다).
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from simai_ai.geometry.rig import RigCamera
from simai_ai.scene.bev import (
    BEV_FORMAT,
    BevConfig,
    BevInputError,
    BevRaster,
    distort_normalized,
    generate_bev,
    needs_regeneration,
    project_floor_points,
)

ROOM = (9.0, 7.5)  # 시뮬레이터 basic 교실 (conftest.ROOM 과 같다)
ROOM_BOUNDS = ((-0.5, -0.5), (ROOM[0] + 0.5, ROOM[1] + 0.5))
SIM_OK = BevConfig(world_bounds=ROOM_BOUNDS, allowed_states=("PROVISIONAL",))
CREATED_AT = 1_760_342_500_000
# BEV 픽셀 오차 허용치(회귀용). 실측은 중앙값 약 0.0001 px, 최대 약 0.002 px 다.
MEDIAN_TOL_PX = 0.05
MAX_TOL_PX = 0.5


@pytest.fixture(scope="module")
def h(scene_helpers):
    assert scene_helpers.ROOM == ROOM
    return scene_helpers


@pytest.fixture(scope="module")
def bev(h):
    """SIM 프레임·고정 생성 시각으로 generate_bev 를 부르는 함수."""

    def _bev(calibration: dict, images: dict, config: BevConfig = SIM_OK, **kwargs):
        return generate_bev(
            h.DictCalibrationProvider(calibration),
            h.make_frames(calibration, images),
            frame_source="SIM",
            created_at_ms=CREATED_AT,
            config=config,
            **kwargs,
        )

    return _bev


@pytest.fixture(scope="module")
def grid_error(h):
    """덮인 BEV 픽셀마다 (격자 위상으로 읽은 위치 − 픽셀 중심) 거리, BEV 픽셀 단위."""

    def _error(plan) -> np.ndarray:
        period = h.GRATING_PERIOD_M
        img = plan.image.astype(float) * 2 - 1
        x_obs = np.arctan2(img[..., 0], img[..., 1]) * period / (2 * math.pi)
        y_obs = np.arctan2(img[..., 2], img[..., 3]) * period / (2 * math.pi)
        r = plan.raster
        xy = r.pixel_centers().reshape(r.height, r.width, 2)
        ex = (x_obs - xy[..., 0] + period / 2) % period - period / 2
        ey = (y_obs - xy[..., 1] + period / 2) % period - period / 2
        return np.hypot(ex, ey)[plan.coverage] / r.meters_per_pixel

    return _error


@pytest.fixture(scope="module")
def plan_basic(bev, sim_calibration, grating_images):
    """왜곡 없는 카메라 4대, 기본 설정(SIM_OK)의 BEV. 여러 시험이 함께 읽는다(바꾸지 않는다)."""
    return bev(sim_calibration, grating_images)


@pytest.fixture(scope="module")
def plan_distorted(bev, sim_calibration_distorted, grating_images_distorted):
    return bev(sim_calibration_distorted, grating_images_distorted)


def _calibrated(doc: dict) -> dict:
    """시뮬레이터 보정을 CALIBRATED 문서처럼 바꾼 시험용 사본(G1_AUTO 출처·검증 결과를 붙인다)."""
    doc = copy.deepcopy(doc)
    doc.update(
        provider_source="G1_AUTO",
        state="CALIBRATED",
        calibration_version="calib-test-0001",
        validation={
            "checkpoint_count": 5,
            "max_head_height_error_m": 0.03,
            "max_pairwise_position_disagreement_m": 0.04,
        },
    )
    return doc


# ---------------------------------------------------------------- 격자 위치


def test_grid_lands_in_place(grid_error, plan_basic):
    plan = plan_basic
    err = grid_error(plan)
    assert plan.coverage.mean() > 0.99
    assert np.median(err) < MEDIAN_TOL_PX
    assert err.max() < MAX_TOL_PX


def test_grid_lands_in_place_with_lens_distortion(grid_error, plan_distorted):
    plan = plan_distorted
    err = grid_error(plan)
    assert plan.metadata["lens_distortion_applied"] is True
    assert plan.coverage.mean() > 0.99
    assert np.median(err) < MEDIAN_TOL_PX
    assert err.max() < MAX_TOL_PX


def test_ignoring_distortion_misplaces_grid(
    bev, grid_error, plan_distorted, sim_calibration_distorted, grating_images_distorted
):
    """왜곡 제거 유무 비교: 왜곡을 무시하면 격자가 수 픽셀 이상 어긋난다."""
    with_undistort = grid_error(plan_distorted)
    config = BevConfig(world_bounds=ROOM_BOUNDS, allowed_states=("PROVISIONAL",), undistort=False)
    plan = bev(sim_calibration_distorted, grating_images_distorted, config)
    without = grid_error(plan)
    assert plan.metadata["lens_distortion_applied"] is False
    assert np.median(without) > 2.0
    assert np.percentile(without, 99) > 10.0
    assert np.median(without) > 100 * np.median(with_undistort)


def test_distortion_free_camera_same_either_way(bev, plan_basic, sim_calibration, grating_images):
    a = plan_basic
    config = BevConfig(world_bounds=ROOM_BOUNDS, allowed_states=("PROVISIONAL",), undistort=False)
    b = bev(sim_calibration, grating_images, config)
    np.testing.assert_allclose(a.image, b.image, atol=1e-6)


# ---------------------------------------------------------------- 보정 버전·상태·메타데이터


def test_metadata_records_calibration_version_and_world_mapping(plan_basic, sim_calibration):
    plan = plan_basic
    meta = plan.metadata
    assert meta["format"] == BEV_FORMAT
    assert meta["calibration_version"] == sim_calibration["calibration_version"]
    assert meta["calibration_state"] == "PROVISIONAL"
    assert meta["created_at"] == CREATED_AT
    assert meta["coordinate_frame"] == {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}
    assert meta["purpose"] == "OVERVIEW_DISPLAY_ONLY"
    assert meta["safety_use"] is False and meta["accuracy"] == "NOT_CLAIMED"
    assert meta["humans_drawn"] is False
    assert meta["cameras_used"] == ["cam1", "cam2", "cam3", "cam4"]
    assert meta["input_provenance"] == {
        "frames": {"source": "SIM", "camera_ids": ["cam1", "cam2", "cam3", "cam4"]},
        "calibration": {"source": "SIM", "calibration_version": sim_calibration["calibration_version"]},
        "is_upper_bound": True,
    }
    raster = meta["raster"]
    assert raster["meters_per_pixel"] == 0.02
    assert raster["origin_xy"] == [-0.5, 8.0]
    assert (raster["col_axis"], raster["row_axis"]) == ("+X", "-Y")
    assert (raster["width"], raster["height"]) == (500, 425)
    # pixel_to_world 로 구석 픽셀 중심을 되돌린다
    m = np.array(raster["pixel_to_world"])
    np.testing.assert_allclose(m @ [0, 0, 1], [-0.49, 7.99, 1])
    np.testing.assert_allclose(m @ [499, 424, 1], [9.49, -0.49, 1])
    assert [f["camera_id"] for f in meta["frames"]] == meta["cameras_used"]


def test_needs_regeneration_when_calibration_version_changes(plan_basic, sim_calibration):
    plan = plan_basic
    assert not plan.needs_regeneration(sim_calibration["calibration_version"])
    assert plan.needs_regeneration("calib-0002")
    assert needs_regeneration(plan.metadata, "calib-0002")


def test_refuses_non_calibrated_by_default(bev, sim_calibration, grating_images):
    with pytest.raises(BevInputError, match="PROVISIONAL"):
        bev(sim_calibration, grating_images, BevConfig(world_bounds=ROOM_BOUNDS))


def test_calibrated_document_accepted_by_default(bev, sim_calibration, grating_images):
    plan = bev(_calibrated(sim_calibration), grating_images, BevConfig(world_bounds=ROOM_BOUNDS))
    meta = plan.metadata
    assert meta["calibration_state"] == "CALIBRATED"
    assert meta["calibration_version"] == "calib-test-0001"
    assert meta["input_provenance"]["calibration"] == {"source": "G1_AUTO", "calibration_version": "calib-test-0001"}


def test_rejects_bad_inputs(h, bev, sim_calibration, grating_images):
    with pytest.raises(BevInputError, match="영상 크기"):
        bev(sim_calibration, {"cam1": grating_images["cam1"][:-1]})
    with pytest.raises(KeyError):
        bev(sim_calibration, {"cam9": grating_images["cam1"]})
    provider = h.DictCalibrationProvider(sim_calibration)
    frames = h.make_frames(sim_calibration, grating_images)
    with pytest.raises(BevInputError, match="frame_source"):
        generate_bev(provider, frames, frame_source="CAMERA", created_at_ms=CREATED_AT, config=SIM_OK)
    with pytest.raises(BevInputError, match="created_at_ms"):
        generate_bev(provider, frames, frame_source="SIM", created_at_ms=1.5, config=SIM_OK)
    tiny = BevConfig(world_bounds=ROOM_BOUNDS, allowed_states=("PROVISIONAL",), max_raster_pixels=1000)
    with pytest.raises(BevInputError, match="너무 크다"):
        bev(sim_calibration, grating_images, tiny)


# ---------------------------------------------------------------- 합성·범위·마스크


def test_subset_of_cameras_and_dominant_camera(bev, grid_error, sim_calibration, grating_images):
    plan = bev(sim_calibration, {k: grating_images[k] for k in ("cam1", "cam3")})
    assert plan.metadata["cameras_used"] == ["cam1", "cam3"]
    # cam1 은 (0.3, 0.3), cam3 은 (8.7, 7.2) 에서 방 가운데를 본다. 시야 안의 가까운 바닥은 가까운 카메라가 주로 칠한다.
    col, row = np.rint(plan.raster.world_to_pixel([[2.5, 2.0], [6.5, 5.5]])).astype(int).T
    assert plan.dominant_camera[row[0], col[0]] == 0
    assert plan.dominant_camera[row[1], col[1]] == 1
    assert np.median(grid_error(plan)) < MEDIAN_TOL_PX


def test_exclude_mask_drops_camera_pixels(bev, sim_calibration, grating_images):
    h_img, w_img = grating_images["cam1"].shape[:2]
    plan = bev(sim_calibration, grating_images, exclude_masks={"cam1": np.ones((h_img, w_img), dtype=bool)})
    assert not (plan.dominant_camera == 0).any()
    assert plan.metadata["masks"]["exclude_masks"] == ["cam1"]


def test_auto_bounds_cover_room(bev, sim_calibration, grating_images):
    config = BevConfig(allowed_states=("PROVISIONAL",), meters_per_pixel=0.05, max_ground_distance_m=12.0)
    plan = bev(sim_calibration, grating_images, config)
    r = plan.raster
    assert r.min_x <= 0.0 and r.min_y <= 0.0 and r.max_x >= ROOM[0] and r.max_y >= ROOM[1]
    assert r.max_x - r.min_x <= 2 * 12.0 + ROOM[0]
    col, row = np.rint(r.world_to_pixel([[4.5, 3.75]])).astype(int).T
    assert plan.coverage[row[0], col[0]]


def test_far_floor_is_not_used(bev, sim_calibration, grating_images):
    config = BevConfig(
        world_bounds=((-20.0, -20.0), (30.0, 30.0)),
        meters_per_pixel=0.1,
        allowed_states=("PROVISIONAL",),
        max_ground_distance_m=6.0,
    )
    plan = bev(sim_calibration, grating_images, config)
    xy = plan.raster.pixel_centers().reshape(plan.raster.height, plan.raster.width, 2)
    centers = np.array([RigCamera.from_calibration(c).center for c in sim_calibration["cameras"]])
    nearest = np.min(np.hypot(xy[..., None, 0] - centers[:, 0], xy[..., None, 1] - centers[:, 1]), axis=-1)
    assert (nearest[plan.coverage] <= 6.0 + 1e-9).all()


def test_grazing_floor_is_not_used(bev, sim_calibration, grating_images):
    """시선각이 min_elevation_rad 보다 누운 바닥은 쓰지 않는다(높이 2.7 m, 30° → 수평 거리 약 4.68 m 까지)."""
    config = BevConfig(world_bounds=ROOM_BOUNDS, allowed_states=("PROVISIONAL",), min_elevation_rad=math.radians(30.0))
    plan = bev(sim_calibration, {"cam1": grating_images["cam1"]}, config)
    xy = plan.raster.pixel_centers().reshape(plan.raster.height, plan.raster.width, 2)
    horiz = np.hypot(xy[..., 0] - 0.3, xy[..., 1] - 0.3)
    limit = 2.7 / math.tan(math.radians(30.0))
    assert (horiz[plan.coverage] <= limit + 1e-6).all()
    assert plan.coverage[horiz < limit - 0.5].mean() > 0.2


def test_uint8_frames_give_uint8_bev(bev, sim_calibration, checker_images):
    plan = bev(sim_calibration, checker_images)
    assert plan.image.dtype == np.uint8 and plan.image.shape == (425, 500, 3)
    # 0.5 m 바둑판: (4.25, 3.25) 는 밝은 칸, (4.75, 3.25) 는 어두운 칸
    col, row = np.rint(plan.raster.world_to_pixel([[4.25, 3.25], [4.75, 3.25]])).astype(int).T
    assert plan.image[row[0], col[0], 0] > 180 and plan.image[row[1], col[1], 0] < 100


def test_room_outline_from_floor_masks_is_inferred_and_close_to_room(h, bev, sim_calibration, grating_images):
    masks = {c["camera_id"]: h.room_floor_mask(c) for c in sim_calibration["cameras"]}
    plan = bev(sim_calibration, grating_images, floor_masks=masks)
    outline = plan.metadata["room_outline"]
    assert outline["source"] == "inferred" and outline["basis"] == "FLOOR_MASK"
    assert outline["accuracy"] == "NOT_CLAIMED" and outline["openings"] is None
    poly = np.array(outline["polygon_xy"])
    # 방 직사각형 [0, 9] x [0, 7.5] 와 BEV 픽셀 두어 개 이내
    assert np.allclose(poly.min(axis=0), [0.0, 0.0], atol=0.05)
    assert np.allclose(poly.max(axis=0), ROOM, atol=0.05)
    area = 0.5 * abs(np.dot(poly[:, 0], np.roll(poly[:, 1], 1)) - np.dot(poly[:, 1], np.roll(poly[:, 0], 1)))
    assert area == pytest.approx(ROOM[0] * ROOM[1], rel=0.02)
    assert plan.metadata["masks"]["floor_masks"] == ["cam1", "cam2", "cam3", "cam4"]


def test_room_outline_without_masks_is_camera_footprint(plan_basic):
    assert plan_basic.metadata["room_outline"]["basis"] == "CAMERA_FOOTPRINT"


class _FakeStaticScene:
    scene_version = "scene-test-0001"

    def render_top_down(self, raster: BevRaster):
        image = np.full((raster.height, raster.width, 4), 0.25, dtype=np.float32)
        return image, np.ones((raster.height, raster.width), dtype=bool)


def test_static_scene_extension_fills_uncovered(bev, sim_calibration, grating_images):
    plan = bev(sim_calibration, {"cam1": grating_images["cam1"]}, static_scene=_FakeStaticScene())
    assert plan.metadata["static_scene"]["scene_version"] == "scene-test-0001"
    assert plan.static_filled is not None
    assert not (plan.static_filled & plan.coverage).any()
    assert (plan.static_filled | plan.coverage).all()
    np.testing.assert_allclose(plan.image[plan.static_filled], 0.25)


# ---------------------------------------------------------------- 카메라 모델


def _camera(model: str, coeffs, K, R, t, size) -> RigCamera:
    return RigCamera.from_calibration(
        {
            "camera_id": "c",
            "image_size": list(size),
            "K": K,
            "distortion": {"model": model, "coeffs": list(coeffs)},
            "R": R,
            "t": t,
        }
    )


@pytest.mark.parametrize(
    ("model", "coeffs"),
    [
        ("OPENCV_PINHOLE", (-0.12, 0.02, 0.0005, -0.0003)),
        ("OPENCV_PINHOLE", (-0.12, 0.02, 0.0005, -0.0003, -0.004)),
        ("OPENCV_PINHOLE", (-0.3, 0.1, 0.001, 0.0, 0.01, 0.05, 0.01, 0.002)),
        ("OPENCV_FISHEYE", (-0.02, 0.003, -0.001, 0.0002)),
    ],
)
def test_distort_is_inverse_of_rig_undistort(model, coeffs):
    K = [[884.0, 0, 1152], [0, 884.0, 648], [0, 0, 1]]
    cam = _camera(model, coeffs, K, [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [0, 0, 0], (2304, 1296))
    g = np.linspace(-1.0, 1.0, 21)
    xn = np.stack(np.meshgrid(g, g * 0.6), axis=-1).reshape(-1, 2)
    uv = cam.ideal_pixels(distort_normalized(xn, model, coeffs))
    np.testing.assert_allclose(cam.normalize(uv), xn, atol=1e-7)


def test_fold_over_points_are_rejected():
    """다항 왜곡이 접히는 반경(k1 = −0.5 이면 r ≈ 0.816) 바깥의 바닥 점은 쓰지 않는다."""
    k1 = -0.5
    # 높이 1 m 에서 바닥을 똑바로 내려다보는 카메라: 카메라 z = −World Z
    K = [[500.0, 0, 1000], [0, 500.0, 1000], [0, 0, 1]]
    cam = _camera("OPENCV_PINHOLE", (k1, 0, 0, 0), K, [[1, 0, 0], [0, -1, 0], [0, 0, -1]], [0, 0, 1.0], (2000, 2000))
    r = np.array([0.2, 0.5, 0.7, 0.9, 1.1, 1.3])
    points = np.column_stack([r, np.zeros_like(r), np.zeros_like(r)])
    _, ok = project_floor_points(cam, points, BevConfig())
    fold = 1 / math.sqrt(-3 * k1)
    assert ok.tolist() == (r < fold).tolist()
