"""리뷰 반례를 실제 기하 경로로 재현하는 회귀 시험 (F1·F2·F3).

합성 카메라 4대(tests/tracking/test_world_track.py 의 카메라·투영 도우미)로 2D 를 만들고, 삼각측량 → 관절 유효성 게이트 →
WorldTrackManager → ContactEstimator → world_state 조립을 그대로 지난다. 도우미는 카메라·좌표·투영 생성에만 쓴다.
"""

import importlib.util
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from simai_ai.contact import ContactEstimator, ContactReason, SupportSurface, SurfaceSet
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.tracking.cross_view import LocalView, ViewCluster
from simai_ai.tracking.world_state import world_state_payload

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def wt():
    spec = importlib.util.spec_from_file_location("_contact_wt_helpers", HERE.parent / "tracking" / "test_world_track.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rig(wt):
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def test_f1_hips_lost_but_ankles_valid_stays_observed_at_anchor(wt, rig):
    """1 m/s 보행 중 10번째 tick 부터 양쪽 엉덩이 2D 를 지운다: 발목이 VALID 인 동안 문서는 접지점 관측 시각(지금) 기준이다."""
    manager, est, surfaces = wt._manager(), ContactEstimator(), SurfaceSet.floor_only()
    last_valid = None
    for k in range(20):
        t = wt.T + k * 100
        x = 2.0 + 0.1 * k
        cluster = wt._cluster(rig, "xv-1", t, x, 3.0)
        if k >= 10:
            views = []
            for v in cluster.views:
                points = list(v.view.keypoints)
                points[11] = points[12] = None
                views.append(replace(v, view=replace(v.view, keypoints=tuple(points))))
            cluster = replace(cluster, views=tuple(views), skeleton=wt.triangulate_skeleton([v.view for v in views], rig))
        frame = manager.update(wt._frame(rig, t, [cluster]), rig)
        doc = world_state_payload(frame, t, "scene-test", est.update(frame, surfaces).contacts())["tracks"][0]
        if last_valid is not None:
            assert doc["t_last_valid"] >= last_valid  # 시각 기준이 뒤로 가지 않는다
        last_valid = doc["t_last_valid"]
        if k >= 10:
            assert doc["pelvis_xyz"] is None
            assert doc["t_last_valid"] == t and doc["observation_state"] == "OBSERVED"
            assert doc["position_xy"] == pytest.approx([x, 3.0], abs=0.03)
            assert max(doc["position_xy_sigma"]) < 0.05 and "velocity" not in doc
            assert doc["ground_anchor_xyz"][2] == 0.0 and doc["support_surface_id"] == "floor"


def test_f2_unregistered_10cm_top_is_not_floor(wt, rig, monkeypatch):
    """10 cm 미등록 상판 위에 선 사람(몸 전체 +0.1 m): 실제 게이트의 VALID 발목(Z 0.18 m)을 바닥으로 확정하지 않는다."""
    original = wt._joints
    monkeypatch.setattr(wt, "_joints", lambda x, y: [X + np.array([0.0, 0.0, 0.1]) for X in original(x, y)])
    manager, est = wt._manager(), ContactEstimator()
    for k in range(6):
        t = wt.T + k * 100
        frame = manager.update(wt._frame(rig, t, [wt._cluster(rig, "xv-1", t, 4.0, 3.0)]), rig)
        tr = frame.tracks[0]
        c = est.update(frame, SurfaceSet.floor_only()).estimates[tr.world_id].contact
        assert [str(tr.skeleton.joints[j].validity) for j in (15, 16)] == ["VALID", "VALID"]
        assert c.support_surface_id != "floor" and c.ground_anchor_xyz is None


def test_f3_knees_hidden_pelvis_moves_off_podium_while_ankles_stay(wt, rig):
    """무릎을 가린 채 발목은 단상 위에 두고 골반·상체만 1 m/s 로 단상 밖으로: 공간 모순이면 접지점을 비운다."""
    manager, est = wt._manager(), ContactEstimator()
    surfaces = SurfaceSet((SupportSurface("podium", 0.4, center_xy=(4.5, 6.75), size_xy=(4.0, 1.1)),))
    out = {}
    for k in range(20):
        t = wt.T + 100 * k
        py = 6.7 - max(0, k - 2) * 0.1
        views = []
        for cid, camera in rig.cameras.items():
            points = []
            for j, (dx, dz) in enumerate(wt._OFFSETS):
                if j in (13, 14):
                    points.append(None)
                    continue
                X = np.array([4.0, (6.7 if j in (15, 16) else py) - dx, dz + 0.4])
                xc = camera.to_camera(X)[0]
                uv = camera.K[:2, :2] @ (xc[:2] / xc[2]) + camera.K[:2, 2]
                points.append((float(uv[0]), float(uv[1]), 0.9))
            views.append(LocalView(cid, cid + "-p", InterpolatedView(cid, t, tuple(points), "SIM", 0.0, (t,))))
        cluster = ViewCluster("xv-1", tuple(views), wt.triangulate_skeleton([v.view for v in views], rig), ())
        frame = manager.update(wt._frame(rig, t, [cluster]), rig)
        out[k] = (frame.tracks[0], est.update(frame, surfaces).estimates[frame.tracks[0].world_id])
    tr, e = out[2]
    assert e.contact.support_surface_validity == "VALID"  # 골반이 발목 위에 있을 때는 단상
    for k in (12, 19):
        tr, e = out[k]
        assert str(tr.skeleton.pelvis_validity()) == "VALID"
        assert e.contact.ground_anchor_xyz is None and e.contact.support_surface_validity == "UNCERTAIN"
        assert ContactReason.PELVIS_ANKLE_INCONSISTENT in e.reasons
