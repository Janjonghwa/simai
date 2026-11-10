"""SIM 합성 관측: ROI는 교차 시점 입력만 줄이고 안쪽 World 결과를 보존한다."""
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cross_view import CORNERS, DictCalibrationProvider, T, calibration_doc, project, skeleton, views_of
from services.tier0.perception import Perception
from services.tier0.settings import Tier0ConfigError, load_settings
from simai_ai.contact import SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.interpolation import KeypointSample, interpolate_camera
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection
from simai_ai.tracking.roi_filter import filter_views, zone_envelope


POLYGON = ((3, 3), (6, 3), (6, 5), (3, 5))


@pytest.fixture
def rig():
    return CameraRig.from_provider(DictCalibrationProvider(calibration_doc()))


def test_four_cameras_reduce_views_and_preserve_inside_world(rig):
    people = {"a": skeleton(4, 4), "b": skeleton(5, 4)}
    outsiders = {f"out{i}": skeleton(x, y) for i, (x, y) in enumerate(
        [(1, 1), (1, 3), (1, 6), (8, 1), (8, 3), (8, 6)])}

    def run(all_people, enabled, active=True):
        feed = SimpleNamespace(camera_ids=tuple(CORNERS), poll=lambda now: [])
        p = Perception(rig, feed, session="roi", scene_version="sim", input_provenance={}, roi_enabled=enabled,
                       roi_margin_m=0.2)
        p.roi_zones = (SimpleNamespace(polygon_xy=POLYGON, is_active=lambda t: active),)
        captured = []
        associate = p.associator.associate
        def record(t, views, **kwargs):
            captured.append(len(views))
            return associate(t, views, **kwargs)
        p.associator.associate = record
        for cam in CORNERS:
            detections = []
            for name, joints in all_people.items():
                kp = project(rig, cam, joints)
                detections.append(TrackedDetection(name, PoseDetection((0, 0, 10, 10), 0.9, kp)))
            p.ingest([Observation2DFrame(cam, 0, 0, T, "SIM", 0.0, tuple(detections))])
        result = p.step(T, SurfaceSet())
        return p, result, captured

    baseline, expected, _ = run(people, False)
    filtered, actual, counts = run(people | outsiders, True)
    assert counts == [8]
    assert filtered.roi_filtered == {c: 6 for c in CORNERS}
    assert len(actual.payload["tracks"]) == 2
    assert actual.payload["tracks"] == expected.payload["tracks"]
    inactive, _, all_counts = run(people | outsiders, True, active=False)
    assert all_counts == [32] and all(n == 0 for n in inactive.roi_filtered.values())


def test_no_zones_and_failed_projection_pass(rig):
    views = views_of(rig, {"out": skeleton(1, 1)})
    assert filter_views(views, rig, (), 1.5) == (views, {})
    # 위쪽 광선은 바닥과 전방에서 만나지 않는다.
    v = views[0]
    camera = rig.cameras[v.camera_id]
    uv = camera.K[:2, :2] @ [0, -10] + camera.K[:2, 2]
    kp = list(v.view.keypoints)
    kp[15] = kp[16] = (float(uv[0]), float(uv[1]), 0.9)
    v = dataclasses.replace(v, view=dataclasses.replace(v.view, keypoints=tuple(kp)))
    assert filter_views([v], rig, (POLYGON,), 1.5) == ([v], {})


def test_bbox_fallback_and_margin(rig):
    view = views_of(rig, {"out": skeleton(1, 1)})[0]
    ankle = view.view.keypoints[15]
    kps = list(view.view.keypoints)
    kps[15] = kps[16] = None
    v = dataclasses.replace(view, view=dataclasses.replace(view.view, keypoints=tuple(kps),
                            bbox=(ankle[0] - 1, ankle[1] - 20, ankle[0] + 1, ankle[1])))
    assert filter_views([v], rig, (POLYGON,), 1.5)[0] == []
    assert filter_views([v], rig, (POLYGON,), 100)[0] == [v]


def test_bbox_uses_same_temporal_interpolation_as_keypoints():
    samples = [KeypointSample("cam1", t, "SIM", 0.0, (None,), bbox=bbox)
               for t, bbox in ((100, (0, 0, 10, 10)), (200, (10, 20, 20, 30)))]
    view, drops = interpolate_camera(samples, 150)
    assert not drops and view.bbox == (5, 10, 15, 20)


@pytest.mark.parametrize("roi", [{"enabled": 1}, {"margin_m": -1}, {"margin_m": float('nan')}, {"extra": 1}])
def test_invalid_settings(tmp_path, roi):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "sim", "roi_filter": roi}}))
    with pytest.raises(Tier0ConfigError):
        load_settings(config, env={})


def test_settings_defaults_and_override(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "sim"}}))
    assert load_settings(config, env={}).roi_filter.margin_m == 1.5
    config.write_text(json.dumps({"input": {"mode": "sim", "run_dir": "sim", "roi_filter": {"enabled": False, "margin_m": 0}}}))
    roi = load_settings(config, env={}).roi_filter
    assert not roi.enabled and roi.margin_m == 0


def test_zone_envelope_covers_gap_between_zones():
    # 단상 작업면 두 개와 사이 개구부가 떨어져 있어도 하나의 사각형으로 묶는다
    left = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    right = ((4.0, 2.0), (5.0, 2.0), (5.0, 3.0), (4.0, 3.0))
    assert zone_envelope((left, right)) == (((0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (0.0, 3.0)),)
    assert zone_envelope(()) == ()
