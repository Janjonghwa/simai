"""-657 사람 트랙 갈라짐 방지 시험 (명세 5.2 추적 3계층·ID 교정, 14차 GPU 세션 3명 → 89 ID).

합성 장면만 쓴다. 예각 장면은 같은 벽에 1.3 m 떨어진 두 카메라 쌍(camA·camD 남쪽, camB·camC 북쪽)이라 한 쌍만의 2대
삼각측량은 깊이 방향으로 크게 흔들린다. 북쪽 쌍의 엉덩이 점을 에피폴라선 방향(u)으로 반대로 밀면(±14~16 px, 재투영 잔차는
그대로 0) 그 쌍의 골반이 0.7~0.8 m 밀려 2+2 로 갈라진 한 사람이 두 위치에 보인다(14차 cam_B/cam_D 상시 EPIPOLAR 장면).

안전장치: 같은 시각 같은 카메라에서 다른 로컬 트랙으로 따로 보인 두 사람은 절대 억제·병합하지 않는다.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import joint_consistency_px, triangulate_skeleton
from simai_ai.reid.collector import LocalTrackFeature, LocalTrackFeatureCache
from simai_ai.reid.visibility import PartVisibility
from simai_ai.tracking.cross_view import LocalView, UncertainReason, ViewCluster
from simai_ai.tracking.diagnostics import TrackingDiagnostics, window_log_line
from simai_ai.tracking.world_track import (
    IdentityFlag,
    RevisionKind,
    RevisionReason,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt657", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)
T, DT = wt.T, wt.DT

ACUTE = {"camA": (0.3, 0.3, 2.45), "camD": (1.6, 0.3, 2.45), "camB": (8.7, 7.2, 2.45), "camC": (7.4, 7.2, 2.45)}
SOUTH, NORTH = ("camA", "camD"), ("camB", "camC")
X0, Y0 = 4.5, 3.75


def _acute_doc():
    doc = wt._calibration_doc()
    f = (wt.SIZE[0] / 2) / math.tan(math.radians(105) / 2)
    cams = []
    for cid, pos in ACUTE.items():
        R, t = wt._look_at(pos, (4.5, 3.75, 0.9))
        cams.append({
            "camera_id": cid, "image_size": list(wt.SIZE),
            "K": [[f, 0.0, wt.SIZE[0] / 2], [0.0, f, wt.SIZE[1] / 2], [0.0, 0.0, 1.0]],
            "distortion": {"model": "NONE", "coeffs": []}, "R": R.tolist(), "t": t.tolist(),
        })  # fmt: skip
    doc["cameras"] = cams
    doc["calibration_version"] = "acute-rig-1"
    return doc


@pytest.fixture(scope="module")
def acute():
    return CameraRig.from_provider(wt._DictCalibrationProvider(_acute_doc()))


@pytest.fixture(scope="module")
def rig():
    return CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _view(rig, cam, lid, t, x, y, du=0.0):
    camera = rig.cameras[cam]
    kps = []
    for X in wt._joints(x, y):
        xc = camera.to_camera(X)[0]
        uv = camera.K[:2, :2] @ (xc[:2] / xc[2]) + camera.K[:2, 2]
        kps.append((float(uv[0] + du), float(uv[1]), 0.9))
    return LocalView(cam, lid, InterpolatedView(cam, t, tuple(kps), "SIM", 0.0, (t,)))


def _cl(rig, name, t, x, y, ids, shift=0.0, uncertain=False):
    """ids: {camera: local id}. 북쪽 카메라(camB +shift, camC −shift)만 u 로 민다."""
    signs = {"camB": 1.0, "camC": -1.0}
    views = tuple(_view(rig, c, lid, t, x, y, shift * signs.get(c, 0.0)) for c, lid in sorted(ids.items()))
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    reasons = (UncertainReason.VIEW_SPLIT,) if uncertain else ()
    return ViewCluster(name, views, skeleton, reasons)


def _ids(cams, prefix="p"):
    return {c: f"{c}-{prefix}" for c in cams}


def _mgr(**cfg):
    return WorldTrackManager(WorldIdIssuer("g657"), WorldTrackConfig(**cfg))


def _step(m, rig, k, clusters):
    return m.update(wt._frame(rig, T + k * DT, clusters), rig)


@pytest.mark.parametrize("shift", [14.0, 16.0])
def test_scene_geometry_is_a_real_acute_split(acute, shift):
    """전제: 밀린 북쪽 쌍의 골반은 0.7 m 넘게 떨어지지만 네 view 를 한 점으로 풀면 엉덩이 재투영이 20 px 안이다."""
    north = _cl(acute, "n", T, X0, Y0, _ids(NORTH), shift)
    south = _cl(acute, "s", T, X0, Y0, _ids(SOUTH))
    p = north.skeleton.pelvis_xyz()
    assert math.hypot(p[0] - X0, p[1] - Y0) > 0.7
    groups = [[acute.cameras[v.camera_id].undistort_keypoints(v.view.keypoints) for v in c.views] for c in (south, north)]
    assert joint_consistency_px(groups, (11, 12), 0.3) <= 20.0


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("shift", [14.0, 16.0])
def test_acute_two_plus_two_split_with_ownerless_remainder_keeps_one_id(acute, shift, uncertain):
    """확정된 한 사람이 2+2 로 갈라지고 나머지는 처음 보는 로컬 트랙(주인 없음)·IDENTITY_UNCERTAIN 이어도 ID 는 1개다."""
    m = _mgr()
    w = None
    for k in range(5):
        f = _step(m, acute, k, [_cl(acute, "all", T + k * DT, X0, Y0, _ids(ACUTE))])
        w = f.tracks[0].world_id
    assert f.tracks[0].lifecycle is TrackLifecycle.CONFIRMED
    for k in range(5, 105):
        t = T + k * DT
        f = _step(m, acute, k, [
            _cl(acute, "s", t, X0, Y0, _ids(SOUTH)),
            _cl(acute, "n", t, X0, Y0, _ids(NORTH, "new"), shift, uncertain),
        ])
        assert [tr.world_id for tr in f.tracks] == [w]
        assert f.tracks[0].updated and f.tracks[0].cameras == tuple(sorted(SOUTH))
        assert IdentityFlag.CROSS_VIEW_UNCERTAIN not in f.tracks[0].identity_flags  # 한 점으로 맞으면 신원은 애매하지 않다
    assert m.issuer.issued == (w,)
    assert m.diag["new_track.suppressed_disjoint_shadow"] == 100


def test_first_seen_split_duplicates_are_merged_while_both_active(acute):
    """처음부터 2+2 로 보여 두 ID 가 생겨도(첫 등장 두 사람은 허용) 둘 다 활성인 채 시점 분할 근거가 4번 이어지면 먼저 만든
    ID 로 합친다: MERGE·aliases, 그 뒤 Track 1개이고 새 ID 를 더 만들지 않는다."""
    m = _mgr()
    frames = []
    for k in range(30):
        t = T + k * DT
        frames.append(_step(m, acute, k, [
            _cl(acute, "s", t, X0, Y0, _ids(SOUTH)),
            _cl(acute, "n", t, X0, Y0, _ids(NORTH), 14.0),
        ]))
    a, b = m.issuer.issued
    revs = [r for f in frames for r in f.revisions]
    assert [(r.kind, r.world_id, r.previous_world_id, r.reason) for r in revs] == [
        (RevisionKind.MERGE, a, b, str(RevisionReason.DUPLICATE_MERGE))
    ]
    assert list(frames[3].revisions) == revs  # 4번째 근거 시각(0.4 s)
    for f in frames[4:]:
        assert [tr.world_id for tr in f.tracks] == [a]
    assert frames[-1].track(a).aliases == (b,) and frames[-1].track(a).identity_revision == 1
    assert m.diag["revision.active_merge"] == 1


def test_same_camera_co_observation_blocks_merge_until_ttl(acute):
    """같은 카메라에서 따로 본 쌍은 병합 금지 TTL 동안 합치지 않고, 그 근거가 다시 없이 TTL 이 지나면 합친다."""
    m = _mgr(distinct_ttl_ms=1000)
    t = T
    both = _ids(NORTH)
    both["camA"] = "camA-x"  # 첫 시각: camA 에서 두 검출(camA-p, camA-x)이 따로 보였다
    _step(m, acute, 0, [_cl(acute, "s", t, X0, Y0, _ids(SOUTH)), _cl(acute, "n", t, X0, Y0 + 0.6, both)])
    a, b = m.issuer.issued
    assert frozenset((a, b)) in m._distinct
    merged_at = None
    for k in range(1, 30):
        t = T + k * DT
        f = _step(m, acute, k, [_cl(acute, "s", t, X0, Y0, _ids(SOUTH)), _cl(acute, "n", t, X0, Y0, _ids(NORTH), 14.0)])
        if f.revisions:
            merged_at = k
            break
    assert merged_at is not None and merged_at * DT > 1000  # TTL 안에서는 합치지 않는다


@pytest.mark.parametrize("dx,dy", [(0.0, 0.5), (0.5, 0.0), (0.0, 0.45)])
def test_two_close_people_are_never_merged_or_suppressed(rig, dx, dy):
    """진짜 근접 두 사람(0.45~0.5 m): 평소 네 카메라에 각자 로컬 트랙으로 보이고, 3 s 마다 1 s 서로 가려 2대씩 따로 보여도
    300 tick 내내 Track 2개이고 병합 0건이다."""
    m = _mgr()
    for k in range(300):
        t = T + k * DT
        x = 2.5 + 0.004 * k
        hidden = k % 30 >= 20
        f = _step(m, rig, k, [
            _cl(rig, "P1", t, x, 3.0, _ids(("cam1", "cam2") if hidden else wt.ALL, "a")),
            _cl(rig, "P2", t, x + dx, 3.0 + dy, _ids(("cam3", "cam4") if hidden else wt.ALL, "b")),
        ])
        assert f.revisions == ()
        if k >= 3:
            assert len(f.tracks) == 2 and all(tr.updated for tr in f.tracks)
    assert len(m.issuer.issued) == 2
    assert m.diag["new_track.suppressed_disjoint_shadow"] == 0


@pytest.mark.parametrize("dy", [0.5, 0.8])
def test_two_people_always_seen_by_disjoint_pairs_keep_two_ids(rig, dy):
    """같은 카메라에서 따로 본 적이 없는 두 사람(늘 2대씩 따로)도 한 점으로 맞지 않으면 두 ID 를 유지한다."""
    m = _mgr()
    for k in range(60):
        t = T + k * DT
        f = _step(m, rig, k, [
            _cl(rig, "P1", t, 3.0, 3.0, _ids(("cam1", "cam2"), "a")),
            _cl(rig, "P2", t, 3.0, 3.0 + dy, _ids(("cam3", "cam4"), "b")),
        ])
        assert f.revisions == () and len(f.tracks) == 2
    assert len(m.issuer.issued) == 2


def test_low_confidence_candidate_near_confirmed_track_confirms_later(acute):
    """카메라가 겹치지 않는 확정 Track 근처의 저신뢰(클러스터 IDENTITY_UNCERTAIN) 새 후보는 6번 관측해야 확정된다.
    확정 지연 단계만 본다(-784 출생 보류를 켜면 이 후보는 내보내지 않고 보류한다: test_birth_hold.py)."""
    m = _mgr(shadow_suppression_enabled=False, active_merge_enabled=False, birth_hold_enabled=False)
    for k in range(5):
        _step(m, acute, k, [_cl(acute, "s", T + k * DT, X0, Y0, _ids(SOUTH))])
    lifecycles = []
    for k in range(5, 12):
        t = T + k * DT
        f = _step(m, acute, k, [
            _cl(acute, "s", t, X0, Y0, _ids(SOUTH)),
            _cl(acute, "n", t, X0, Y0, _ids(NORTH, "new"), 14.0, uncertain=True),
        ])
        (n,) = (tr for tr in f.tracks if tr.world_id != m.issuer.issued[0])
        lifecycles.append(n.lifecycle)
    assert lifecycles[:5] == [TrackLifecycle.TENTATIVE] * 5 and lifecycles[5] is TrackLifecycle.CONFIRMED
    assert m.diag["lifecycle.confirm_delayed"] >= 1


# ---------------------------------------------------------------- 재입장 ID 유지 (Lead 추가 지시)


def test_occluded_three_seconds_then_back_keeps_id(rig):
    """(a) 한 사람이 3.5 s 동안 카메라 1대에만 보였다가 다시 2대 이상에 보이면 같은 ID 다(LOST 뒤 2대 로컬 이력 재연결)."""
    m = _mgr()
    for k in range(10):
        f = _step(m, rig, k, [_cl(rig, "P", T + k * DT, 3.0, 3.0, _ids(wt.ALL))])
    w = f.tracks[0].world_id
    for k in range(10, 45):
        _step(m, rig, k, [_cl(rig, "P1", T + k * DT, 3.0, 3.0, _ids(("cam1",)))])
    for k in range(45, 50):
        f = _step(m, rig, k, [_cl(rig, "P", T + k * DT, 3.1, 3.0, _ids(("cam1", "cam2", "cam3")))])
    assert [tr.world_id for tr in f.tracks] == [w] and m.issuer.issued == (w,)


def _features(cache, ids, t, emb):
    for i, (cam, lid) in enumerate(sorted(ids.items())):
        cache.put(LocalTrackFeature(
            camera_id=cam, local_track_id=lid, frame_sequence=max(1, (t - T) // DT + 1), capture_time_ms=t,
            embedding=emb, visibility=PartVisibility.all_visible(), quality_score=0.9, orientation_bin=i % 8,
        ), keep_timeline=True)  # fmt: skip


def _basis(i, dim=1280):
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


def test_reentry_after_twenty_seconds_with_twin_track_left_keeps_original_id(acute):
    """(b) 한 사람이 2+2 분신 두 ID(합치기 전)로 보이다가 20 s 나갔다 들어오면, 두 분신이 같은 외형이라도 서로 경쟁자가 아니라
    원래 ID(먼저 만든 것)로 돌아온다(14차 reid.reentry_ambiguous: 분신끼리 margin 0)."""
    cache = LocalTrackFeatureCache()
    m = WorldTrackManager(WorldIdIssuer("g657"), WorldTrackConfig(active_merge_confirm_ticks=1000), feature_lookup=cache.lookup)
    emb = _basis(3)
    for k in range(20):
        t = T + k * DT
        if k >= 5:  # 비동기 외형 특징은 늦게 온다(첫 시각엔 없어 외형 분할 억제가 못 막았다, 14차)
            _features(cache, _ids(ACUTE), t, emb)
        _step(m, acute, k, [_cl(acute, "s", t, X0, Y0, _ids(SOUTH)), _cl(acute, "n", t, X0, Y0, _ids(NORTH), 14.0)])
    a, b = m.issuer.issued
    assert m.galleries.get(a) is not None and m.galleries.get(b) is not None
    back = None
    for k in range(220, 230):
        t = T + k * DT
        ids = _ids(ACUTE, "r")
        _features(cache, ids, t, emb)
        f = _step(m, acute, k, [_cl(acute, "all", t, X0 + 0.3, Y0, ids)])
        back = f
    assert [tr.world_id for tr in back.tracks] == [a]
    assert len(m.issuer.issued) == 2


def test_similar_people_reentry_is_ambiguous_and_gets_new_id(rig):
    """(c) 외형이 같은 두 사람(같은 카메라에 따로 보인 적 있음)이 나간 뒤 한 명이 둘 다 갈 수 있는 곳으로 들어오면 애매하므로
    새 ID 를 만든다(오연결 금지)."""
    cache = LocalTrackFeatureCache()
    m = WorldTrackManager(WorldIdIssuer("g657"), WorldTrackConfig(), feature_lookup=cache.lookup)
    emb = _basis(5)
    for k in range(20):
        t = T + k * DT
        _features(cache, _ids(wt.ALL, "a"), t, emb)
        _features(cache, _ids(wt.ALL, "b"), t, emb)
        _step(m, rig, k, [_cl(rig, "A", t, 3.0, 3.0, _ids(wt.ALL, "a")), _cl(rig, "B", t, 3.0, 4.2, _ids(wt.ALL, "b"))])
    first = set(m.issuer.issued)
    for k in range(220, 226):
        t = T + k * DT
        ids = _ids(wt.ALL, "r")
        _features(cache, ids, t, emb)
        f = _step(m, rig, k, [_cl(rig, "R", t, 3.0, 3.6, ids)])
    (tr,) = f.tracks
    assert tr.world_id not in first and len(m.issuer.issued) == 3


# ---------------------------------------------------------------- 계측


def test_window_line_reports_confirmations_tentative_losses_concurrency_and_merges(acute):
    m = _mgr()
    diag = TrackingDiagnostics()
    for k in range(10):
        t = T + k * DT
        diag.observe_tick(t, ())
        f = _step(m, acute, k, [_cl(acute, "s", t, X0, Y0, _ids(SOUTH)), _cl(acute, "n", t, X0, Y0, _ids(NORTH), 14.0)])
        diag.observe_world(f, m.diag, m.duplicate_pair_count)
    line = window_log_line(diag.take_window(T + 10 * DT))
    assert "확정승격 2" in line and "동시확정최대 2" in line and "활성병합 1" in line
    assert "임시소멸 0" in line and "중복쌍" in line and "소실/되찾음" in line


# ---------------------------------------------------------------- 기립 키 보조 근거 (Lead 추가 지시 2)

_SITTING = [(dx, dz) for dx, dz in wt._OFFSETS[:11]] + [(-0.12, 0.45), (0.12, 0.45), (-0.12, 0.45), (0.12, 0.45),
                                                      (-0.12, 0.08), (0.12, 0.08)]  # fmt: skip
_SITTING = [(dx, dz - (0.4 if i < 11 else 0.0)) for i, (dx, dz) in enumerate(_SITTING)]  # 상체 0.4 m 내려 앉음
_LYING = [(dx, 0.15 + 0.02 * i) for i, (dx, _) in enumerate(wt._OFFSETS)]


def _body_view(rig, cam, lid, t, x, y, scale=1.0, offsets=None):
    camera = rig.cameras[cam]
    kps = []
    for dx, dz in offsets or wt._OFFSETS:
        X = np.array([x, y - dx, dz * scale])
        xc = camera.to_camera(X)[0]
        uv = camera.K[:2, :2] @ (xc[:2] / xc[2]) + camera.K[:2, 2]
        kps.append((float(uv[0]), float(uv[1]), 0.9))
    return LocalView(cam, lid, InterpolatedView(cam, t, tuple(kps), "SIM", 0.0, (t,)))


def _body(rig, name, t, x, y, ids, scale=1.0, offsets=None):
    views = tuple(_body_view(rig, c, lid, t, x, y, scale, offsets) for c, lid in sorted(ids.items()))
    return ViewCluster(name, views, triangulate_skeleton([v.view for v in views], rig))


def _gated(rig, cluster):
    from simai_ai.geometry.joint_gate import JointGate

    return JointGate().update([v.view for v in cluster.views], rig, {}, "COCO17", frozenset(), skeleton=cluster.skeleton)


@pytest.mark.parametrize("offsets,expected", [(None, True), (_SITTING, False), (_LYING, False)])
def test_stature_sample_only_from_standing_posture(rig, offsets, expected):
    """키 표본은 기립 자세에서만 낸다. 앉기·누움(낙상) 표본은 빼고, 값을 지어내지 않는다."""
    from simai_ai.tracking.world_track import standing_stature

    cluster = _body(rig, "P", T, 3.0, 3.0, _ids(wt.ALL), offsets=offsets)
    value = standing_stature(_gated(rig, cluster), WorldTrackConfig())
    assert (value is not None) is expected
    if expected:
        assert value == pytest.approx(1.63 + 0.12 - 0.08, abs=0.03)  # 귀·눈 평균 + 머리 꼭대기 오프셋 − 발목


def _two_people_then_return(rig, scale_a, scale_b, return_scale, return_offsets=None, ticks_back=40):
    """외형이 같은 두 사람 A(키 배율 scale_a)·B(scale_b)가 같은 카메라에 따로 보이다가 나가고, 20 s 뒤 한 명이 둘 다 갈 수 있는
    곳으로 새 로컬 트랙으로 들어온다. (A, B, 마지막 frame, manager)."""
    cache = LocalTrackFeatureCache()
    m = WorldTrackManager(WorldIdIssuer("g657"), WorldTrackConfig(), feature_lookup=cache.lookup)
    emb = _basis(7)
    for k in range(30):
        t = T + k * DT
        _features(cache, _ids(wt.ALL, "a"), t, emb)
        _features(cache, _ids(wt.ALL, "b"), t, emb)
        _step(m, rig, k, [_body(rig, "A", t, 3.0, 3.0, _ids(wt.ALL, "a"), scale_a),
                          _body(rig, "B", t, 3.0, 4.2, _ids(wt.ALL, "b"), scale_b)])  # fmt: skip
    a, b = m.issuer.issued
    f = None
    for k in range(230, 230 + ticks_back):
        t = T + k * DT
        ids = _ids(wt.ALL, "r")
        _features(cache, ids, t, emb)
        f = _step(m, rig, k, [_body(rig, "R", t, 3.0, 3.6, ids, return_scale, return_offsets)])
    return a, b, f, m


def test_same_look_different_stature_reentry_returns_to_right_id(rig):
    """외형이 같고 키가 다른(약 1.62/1.80 m) 두 사람 중 키 큰 B 가 재입장하면, 처음엔 애매해 새 ID 지만 기립 표본 1 s 뒤 키로
    A 를 빼고 B 로 합친다."""
    _a, b, f, m = _two_people_then_return(rig, 1.0, 1.11, 1.11)
    assert [tr.world_id for tr in f.tracks] == [b]
    assert m.diag["reid.stature_rejected"] >= 1
    assert [(r.world_id, r.kind) for r in m.revisions] == [(b, RevisionKind.MERGE)]


def test_same_look_same_stature_reentry_stays_new_id(rig):
    """외형도 키도 비슷하면(1 cm 차) 애매하므로 새 ID 를 유지한다(오연결 금지)."""
    a, b, f, m = _two_people_then_return(rig, 1.0, 1.006, 1.0)
    (tr,) = f.tracks
    assert tr.world_id not in (a, b) and m.revisions == ()
    assert m.diag["reid.stature_tiebreak"] == 0


def test_stature_unused_without_enough_standing_samples(rig):
    """재입장한 사람이 계속 앉아 있으면 기립 표본이 없어 키를 쓰지 않는다(새 ID 유지)."""
    a, b, f, m = _two_people_then_return(rig, 1.0, 1.11, 1.11, return_offsets=_SITTING)
    (tr,) = f.tracks
    assert tr.world_id not in (a, b)
    assert m.diag["reid.stature_rejected"] == 0 and m.diag["reid.stature_tiebreak"] == 0
