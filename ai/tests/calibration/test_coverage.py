"""커버리지 기반 설치 안내·걷기 안내 시험 (명세 4.5 설치 안내와 걷기 경로 안내)."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from simai_ai.calibration.coverage import CoverageConfig, GuidanceCode, GuidanceKind, coverage_guidance

POLY = [(0.5, 0.5), (7.5, 0.5), (7.5, 5.5), (0.5, 5.5)]
CORNER_CENTERS = {"cam1": (0.2, 0.2, 2.5), "cam2": (7.8, 0.2, 2.5), "cam3": (7.8, 5.8, 2.5), "cam4": (0.2, 5.8, 2.5)}


def _look_at(center, target):
    forward = np.subtract(target, center) / np.linalg.norm(np.subtract(target, center))
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    R = np.array([right, down, forward])
    return R.tolist(), (-R @ np.asarray(center)).tolist()


@pytest.fixture
def doc(cameras):
    return {"calibration_version": "cal-1", "cameras": [c.to_contract() for c in cameras]}


def _aimed(camera_factory, camera_id, target):
    R, t = _look_at(CORNER_CENTERS[camera_id], target)
    return dataclasses.replace(camera_factory(camera_id), R=R, t=t).to_contract()


def _codes(report):
    return [(g.code, g.camera_id) for g in report.guidance]


def test_good_layout_has_no_guidance(doc):
    report = coverage_guidance(doc, POLY)
    assert report.floor_evaluated and not report.observed_evaluated
    assert report.guidance == ()
    assert report.triangulable_ratio == pytest.approx(1.0)
    assert sum(report.cells_by_camera_count.values()) == report.walkable_cells == 140


def test_camera_looking_too_low(doc, camera_factory):
    doc["cameras"][2] = _aimed(camera_factory, "cam3", (6.5, 4.5, 0.0))
    report = coverage_guidance(doc, POLY)
    assert _codes(report) == [(GuidanceCode.CAMERA_AIM_TOO_LOW, "cam3")]
    g = report.guidance[0]
    assert g.kind is GuidanceKind.INSTALLATION and "너무 아래" in g.message
    assert g.metrics["head_cut_ratio"] > 0.5


def test_camera_looking_too_high(doc, camera_factory):
    doc["cameras"][2] = _aimed(camera_factory, "cam3", (2.0, 1.0, 3.2))
    report = coverage_guidance(doc, POLY)
    assert (GuidanceCode.CAMERA_AIM_TOO_HIGH, "cam3") in _codes(report)


def test_camera_not_covering_floor(doc, camera_factory):
    doc["cameras"][0] = _aimed(camera_factory, "cam1", (-5.0, -5.0, 2.5))  # 벽 쪽을 본다
    report = coverage_guidance(doc, POLY)
    assert (GuidanceCode.CAMERA_NOT_COVERING_FLOOR, "cam1") in _codes(report)


def test_weak_and_blind_floor(doc):
    one = {"cameras": doc["cameras"][:1]}
    report = coverage_guidance(one, [(0.0, 0.0), (8.0, 0.0), (8.0, 6.0), (0.0, 6.0)])
    codes = [g.code for g in report.guidance]
    assert GuidanceCode.FLOOR_WEAK_REGION in codes and GuidanceCode.FLOOR_BLIND_REGION in codes
    weak = next(g for g in report.guidance if g.code is GuidanceCode.FLOOR_WEAK_REGION)
    assert weak.cell_count == report.cells_by_camera_count[1]
    assert len(weak.cells_xy) == min(weak.cell_count, CoverageConfig().max_cells_listed)
    assert weak.bbox_xy is not None and weak.metrics["area_m2"] == pytest.approx(weak.cell_count * 0.25)
    assert report.triangulable_ratio == 0.0


def test_no_walkable_area_means_no_floor_guidance(doc, camera_factory):
    doc["cameras"][2] = _aimed(camera_factory, "cam3", (6.5, 4.5, 0.0))
    report = coverage_guidance(doc)
    assert not report.floor_evaluated
    assert report.guidance == () and report.triangulable_ratio is None
    assert all(c.floor_cells_visible is None for c in report.cameras)


def test_walk_path_for_unvisited_cells(doc):
    walked = [(x / 10, y / 10) for x in range(5, 40) for y in range(5, 55)]  # 왼쪽 절반만 걸었다
    report = coverage_guidance(doc, POLY, observed_floor_xy=walked)
    assert report.observed_evaluated
    assert report.observed_floor_ratio == pytest.approx(0.5, abs=0.05)
    walk = [g for g in report.guidance if g.code is GuidanceCode.WALK_PATH_NEEDED]
    assert len(walk) == 1 and walk[0].kind is GuidanceKind.WALK
    assert all(x > 3.9 for x, _ in walk[0].cells_xy)


def test_image_coverage_guidance(doc):
    corner = [(50 + i % 10, 60 + i // 10) for i in range(100)]
    spread = [(u, v) for u in range(100, 1900, 200) for v in range(100, 1000, 150) for _ in range(3)]
    report = coverage_guidance(doc, observed_pixels={"cam1": corner, "cam2": [], "cam3": spread, "cam9": corner})
    assert _codes(report) == [
        (GuidanceCode.CAMERA_LOW_IMAGE_COVERAGE, "cam1"),
        (GuidanceCode.CAMERA_NO_OBSERVATIONS, "cam2"),
    ]
    cams = {c.camera_id: c for c in report.cameras}
    assert cams["cam3"].image_coverage == pytest.approx(1.0) and cams["cam4"].image_coverage is None


def test_record_is_json(doc, camera_factory):
    doc["cameras"][2] = _aimed(camera_factory, "cam3", (6.5, 4.5, 0.0))
    record = coverage_guidance(doc, POLY, observed_floor_xy=[(1.0, 1.0)]).to_record()
    text = json.dumps(record, allow_nan=False, ensure_ascii=False)
    assert "CAMERA_AIM_TOO_LOW" in text and "WALK_PATH_NEEDED" in text


@pytest.mark.parametrize("kwargs", [{"cell_m": 0}, {"head_height_m": 0.05}, {"aim_cut_ratio": 1.5}, {"min_cameras": 0}])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        CoverageConfig(**kwargs)


def test_aim_guidance_uses_actual_boundary(doc, camera_factory):
    """머리·발이 좌우 경계로 나간 칸은 위·아래 방향 안내로 세지 않는다(카메라를 90° 눕힌 경우)."""
    R, t = _look_at(CORNER_CENTERS["cam3"], (7.0, 5.0, 0.0))  # 경계를 구분하지 않으면 약 68% 를 위로 잘림으로 센다
    roll = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])  # 광축 둘레 90°
    R2 = (roll @ np.asarray(R)).tolist()
    t2 = (roll @ np.asarray(t)).tolist()
    doc["cameras"][2] = dataclasses.replace(camera_factory("cam3"), R=R2, t=t2).to_contract()
    report = coverage_guidance(doc, POLY)
    assert GuidanceCode.CAMERA_AIM_TOO_LOW not in [g.code for g in report.guidance]
    cam3 = next(c for c in report.cameras if c.camera_id == "cam3")
    assert cam3.head_cut_ratio is not None and cam3.head_cut_ratio < 0.3
