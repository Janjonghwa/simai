"""World Track COPE Re-ID 보조 연결 단위 시험 (명세 5.2, S15P31A101-463 2단계).

시험 항목:
1. 갤러리 다중 시점·방향 슬롯 갱신과 `max` 유사도 계산 및 `reid_margin` 기록
2. 분할 판정: 같은 사람 외형(`sim` 높음)일 때 잔여 조각 억제·병합
3. 분할 판정(`<n` 보호): 서로 다른 사람 외형(`sim` 낮음)이 근접했을 때 억제하지 않고 별도 Track 유지
4. 근접 교차 뒤 ID swap 발생 시 외형 갤러리 비교로 원래 ID 복원
5. 근접(`<= 1.0 m`)·`IDENTITY_UNCERTAIN` 중 갤러리 오염 방지(갱신 동결)
6. `LOST` → `DORMANT` 전환, `world_state` 미송출, 30분 안 재입장 시 기존 World ID 승계
7. `IDENTITY_UNCERTAIN`·분할·swap 의심 중 `avatar_update_hold = True` 설정 및 해소 시 복구
8. `reid_enabled = False` 또는 특징 부재 시 기존 기하 추적과 동일하게 동작
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.reid.collector import LocalTrackFeature, LocalTrackFeatureCache
from simai_ai.reid.visibility import PartVisibility
from simai_ai.tracking.cross_view import ViewCluster
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    RevisionKind,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt_helpers", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT = wt.T, wt.DT


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _ortho_basis(idx: int, dim: int = 1280) -> np.ndarray:
    """직교 단위 벡터: 같은 idx 끼리는 코사인 유사도 1.0, 다른 idx 끼리는 0.0."""
    v = np.zeros(dim, dtype=np.float32)
    v[idx % dim] = 1.0
    return v


def _blend_vec(base_idx: int, other_idx: int, weight_other: float, dim: int = 1280) -> np.ndarray:
    v = (1.0 - weight_other) * _ortho_basis(base_idx, dim) + weight_other * _ortho_basis(other_idx, dim)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _put_features(
    cache: LocalTrackFeatureCache,
    ids: dict[str, str],
    t_ms: int,
    emb: np.ndarray,
    *,
    orientation_bin: int = 1,
    quality: float = 0.9,
) -> None:
    for idx, (cam, lid) in enumerate(sorted(ids.items())):
        cache.put(
            LocalTrackFeature(
                camera_id=cam,
                local_track_id=lid,
                frame_sequence=max(1, (t_ms - T) // DT + 1),
                capture_time_ms=t_ms,
                embedding=emb,
                visibility=PartVisibility.all_visible(),
                quality_score=quality,
                orientation_bin=(orientation_bin + idx) % 8,
            ),
            keep_timeline=True,
        )


def _cluster(rig, name: str, t_ms: int, x: float, y: float, ids: dict[str, str], *, uncertain: bool = False) -> ViewCluster:
    views = tuple(wt._view(rig, cam, lid, t_ms, x, y) for cam, lid in sorted(ids.items()))
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    reasons = (wt.UncertainReason.AMBIGUOUS_ASSIGNMENT,) if uncertain else ()
    return ViewCluster(name, views, skeleton, reasons)


def test_gallery_multiview_slots_and_reid_margin(rig) -> None:
    """1. 갤러리 다중 시점·방향 슬롯 갱신과 max 유사도 계산 및 경쟁 트랙 대비 reid_margin 기록."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(reid_enabled=True)
    mgr = WorldTrackManager(WorldIdIssuer("gal"), cfg, feature_lookup=cache.lookup)

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    b_ids = {c: f"{c}-b" for c in wt.ALL}
    emb_a = _ortho_basis(0)
    emb_b = _ortho_basis(1)

    for k in range(4):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a, orientation_bin=k)
        _put_features(cache, b_ids, t, emb_b, orientation_bin=k + 2)
        c_a = _cluster(rig, "A", t, 2.0, 2.0, a_ids)
        c_b = _cluster(rig, "B", t, 4.5, 4.5, b_ids)
        frame = mgr.update(wt._frame(rig, t, [c_a, c_b]), rig)

    assert len(frame.tracks) == 2
    for snap in frame.tracks:
        gal = mgr.galleries.get(snap.world_id)
        assert gal is not None
        assert len(gal) >= 2
        # 직교 임베딩(유사도 1.0 vs 0.0)이므로 경쟁 트랙 대비 마진이 양수로 기록되어야 한다
        assert snap.reid_margin is not None
        assert snap.reid_margin >= 0.8


def test_split_adjudication_suppresses_same_person_remainder(rig) -> None:
    """2. 분할 판정: 같은 사람 외형(sim 높음)일 때 잔여 2카메라 조각을 새 ID 로 만들지 않고 억제한다."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(
        reid_enabled=True,
        split_suppression_enabled=True,
        split_suppression_radius_m=0.6,
        reid_split_max_radius_m=1.2,
    )
    mgr = WorldTrackManager(WorldIdIssuer("splitsame"), cfg, feature_lookup=cache.lookup)

    a_full = {c: f"{c}-a" for c in wt.ALL}
    emb_a = _ortho_basis(2)

    # k=0..3: 4대 카메라로 A 확정 및 갤러리 축적
    for k in range(4):
        t = T + k * DT
        _put_features(cache, a_full, t, emb_a, orientation_bin=k)
        mgr.update(wt._frame(rig, t, [_cluster(rig, "A", t, 3.0, 3.0, a_full)]), rig)

    # k=4: 보정 오차로 cam1·cam2 는 (3.0, 3.0), cam3·cam4 는 0.85 m 떨어진 (3.85, 3.0) 의 새 로컬 ID 로 갈라짐
    # 기하 반경(0.6 m) 밖이지만 Re-ID 확장 반경(1.2 m) 안이고 외형이 같은 사람(sim=1.0)이므로 억제되어야 함
    t4 = T + 4 * DT
    a_main = {"cam1": "cam1-a", "cam2": "cam2-a"}
    a_rem = {"cam3": "cam3-a2", "cam4": "cam4-a2"}
    _put_features(cache, a_main, t4, emb_a)
    _put_features(cache, a_rem, t4, _blend_vec(2, 3, 0.05))
    frame = mgr.update(
        wt._frame(
            rig,
            t4,
            [
                _cluster(rig, "A_main", t4, 3.0, 3.0, a_main),
                _cluster(rig, "A_rem", t4, 3.85, 3.0, a_rem),
            ],
        ),
        rig,
    )

    assert len(frame.tracks) == 1
    assert mgr.diag.get("new_track.split_remainder_suppressed", 0) >= 1
    assert mgr.diag.get("reid.split_suppressed_same", 0) >= 1


def test_split_adjudication_preserves_different_person_near_owner_under_count_protection(rig) -> None:
    """3. 분할 판정(<n 보호): 기하 억제 반경(0.6 m) 안(0.45 m)에 나타난 조각이라도 외형이 뚜렷이 다른 사람(sim < 0.42)이면
    억제하지 않고 별도 World Track 으로 만들어 <n(놓침)을 막는다."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(
        reid_enabled=True,
        split_suppression_enabled=True,
        split_suppression_radius_m=0.6,
        split_suppression_require_owner=False,
    )
    mgr = WorldTrackManager(WorldIdIssuer("splitdiff"), cfg, feature_lookup=cache.lookup)

    a_full = {c: f"{c}-a" for c in wt.ALL}
    emb_a = _ortho_basis(4)
    emb_b = _ortho_basis(5)  # 직교(코사인 유사도 0.0 < 0.42)

    for k in range(4):
        t = T + k * DT
        _put_features(cache, a_full, t, emb_a)
        mgr.update(wt._frame(rig, t, [_cluster(rig, "A", t, 3.0, 3.0, a_full)]), rig)

    # k=4: A 는 cam1·cam2 로 (3.0, 3.0) 에 있고, 0.45 m 옆 (3.45, 3.0) 에 다른 사람 B 가 등장하면서 로컬 트랙 cam3-a 를 물려받음
    # Re-ID 가 꺼져 있으면 split_suppression_radius_m=0.6m 조건에 걸려 B 가 억제되지만, Re-ID 가 다른 사람임을 판별해 보존한다
    t4 = T + 4 * DT
    a_sub = {"cam1": "cam1-a", "cam2": "cam2-a"}
    b_sub = {"cam3": "cam3-a", "cam4": "cam4-b"}
    _put_features(cache, a_sub, t4, emb_a)
    _put_features(cache, b_sub, t4, emb_b)
    frame = mgr.update(
        wt._frame(
            rig,
            t4,
            [
                _cluster(rig, "A", t4, 3.0, 3.0, a_sub),
                _cluster(rig, "B", t4, 3.45, 3.0, b_sub),
            ],
        ),
        rig,
    )

    assert len(frame.tracks) == 2
    assert mgr.diag.get("reid.split_preserved_different", 0) == 1


def test_close_crossing_swap_reverted_by_gallery(rig) -> None:
    """4. 근접 교차 뒤 ID swap 발생 시 외형 갤러리 교차 유사도 우위로 원래 World ID 를 복원한다."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(
        reid_enabled=True,
        reid_swap_correction_enabled=True,
        reid_swap_proximity_m=1.5,
        reid_swap_margin=0.10,
        reid_swap_min_sim=0.58,
    )
    mgr = WorldTrackManager(WorldIdIssuer("swap"), cfg, feature_lookup=cache.lookup)

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    b_ids = {c: f"{c}-b" for c in wt.ALL}
    emb_a = _ortho_basis(6)
    emb_b = _ortho_basis(7)

    # k=0..3: A 와 B 가 멀리 떨어져 걸으며 각자 갤러리를 쌓는다
    for k in range(4):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a)
        _put_features(cache, b_ids, t, emb_b)
        frame = mgr.update(
            wt._frame(
                rig,
                t,
                [
                    _cluster(rig, "A", t, 2.0 + 0.1 * k, 2.5, a_ids),
                    _cluster(rig, "B", t, 3.6 - 0.1 * k, 2.5, b_ids),
                ],
            ),
            rig,
        )
    wid_a = next(tr.world_id for tr in frame.tracks if tr.position_xy[0] < 2.8)
    wid_b = next(tr.world_id for tr in frame.tracks if tr.position_xy[0] > 2.8)

    # k=4: 두 트랙이 1.1 m 거리까지 근접 (근접 교차 기록, <= 1.5 m)
    t4 = T + 4 * DT
    _put_features(cache, a_ids, t4, emb_a)
    _put_features(cache, b_ids, t4, emb_b)
    mgr.update(
        wt._frame(
            rig,
            t4,
            [
                _cluster(rig, "A", t4, 2.35, 2.5, a_ids),
                _cluster(rig, "B", t4, 3.25, 2.5, b_ids),
            ],
        ),
        rig,
    )

    # k=5: 로컬 트랙 ID 는 a_ids 가 (2.45, 2.5) 에 남았지만 실제 사람은 B(emb_b)이고,
    # b_ids 가 (3.15, 2.5) 에 남았지만 실제 사람은 A(emb_a)로 뒤바뀐 상황 (ID swap)
    t5 = T + 5 * DT
    _put_features(cache, a_ids, t5, emb_b)
    _put_features(cache, b_ids, t5, emb_a)
    frame5 = mgr.update(
        wt._frame(
            rig,
            t5,
            [
                _cluster(rig, "c1", t5, 2.45, 2.5, a_ids),
                _cluster(rig, "c2", t5, 3.15, 2.5, b_ids),
            ],
        ),
        rig,
    )

    assert mgr.diag.get("reid.swap_reverted", 0) == 1
    snap_by_id = {tr.world_id: tr for tr in frame5.tracks}
    # swap 복원 뒤 wid_a 는 emb_a 가 관측된 (3.15, 2.5) 쪽 클러스터를, wid_b 는 emb_b 가 관측된 (2.45, 2.5) 쪽을 따라간다
    assert snap_by_id[wid_a].position_xy[0] == pytest.approx(3.15, abs=0.08)
    assert snap_by_id[wid_b].position_xy[0] == pytest.approx(2.45, abs=0.08)


def test_gallery_frozen_during_close_proximity_and_uncertain(rig) -> None:
    """5. 근접(<= 1.0 m) 또는 IDENTITY_UNCERTAIN 구간에서는 갤러리를 갱신하지 않아 오염을 막는다."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(reid_enabled=True, reid_gallery_freeze_distance_m=1.0)
    mgr = WorldTrackManager(WorldIdIssuer("freeze"), cfg, feature_lookup=cache.lookup)

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    b_ids = {c: f"{c}-b" for c in wt.ALL}
    emb_a = _ortho_basis(8)
    emb_b = _ortho_basis(9)
    emb_polluted = _ortho_basis(10)

    # k=0..2: 멀리 떨어져 갤러리 초기 등록
    for k in range(3):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a)
        _put_features(cache, b_ids, t, emb_b)
        frame = mgr.update(
            wt._frame(
                rig,
                t,
                [
                    _cluster(rig, "A", t, 2.0, 2.0, a_ids),
                    _cluster(rig, "B", t, 4.5, 4.5, b_ids),
                ],
            ),
            rig,
        )
    wid_a = next(tr.world_id for tr in frame.tracks if tr.position_xy[0] < 3.0)

    # k=3: 두 사람이 0.5 m 이내로 근접한 상태에서 오염된 특징(emb_polluted)이 들어옴 -> 동결되어야 함
    t3 = T + 3 * DT
    _put_features(cache, a_ids, t3, emb_polluted, orientation_bin=7)
    _put_features(cache, b_ids, t3, emb_polluted, orientation_bin=7)
    mgr.update(
        wt._frame(
            rig,
            t3,
            [
                _cluster(rig, "A", t3, 2.1, 2.0, a_ids),
                _cluster(rig, "B", t3, 2.5, 2.0, b_ids),
            ],
        ),
        rig,
    )

    gal_a = mgr.galleries.get(wid_a)
    assert gal_a is not None
    # 오염된 임베딩과의 유사도는 0.0 이어야 하고 원래 임베딩과의 유사도는 > 0.9 이어야 한다
    assert gal_a.similarity(emb_polluted) == pytest.approx(0.0, abs=1e-5)
    assert gal_a.similarity(emb_a) >= 0.95


def test_lost_to_dormant_not_exported_and_reentry_within_30min_inherits_world_id(rig, contract_errors) -> None:
    """6. LOST → DORMANT 전환 시 world_state 에 송출되지 않으며, 30분 안 재입장 시 기존 World ID 를 승계한다."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(
        reid_enabled=True,
        reid_dormant_enabled=True,
        reid_dormant_ttl_ms=1_800_000,  # 30분
    )
    mgr = WorldTrackManager(
        WorldIdIssuer("dormant"),
        cfg,
        input_provenance={"frames": {"source": "SIM", "camera_ids": list(wt.ALL)}, "is_upper_bound": True},
        feature_lookup=cache.lookup,
    )

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    emb_a = _ortho_basis(11)

    # k=0..4: A 가 확정(CONFIRMED)되고 갤러리에 외형이 쌓임
    for k in range(5):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a, orientation_bin=k)
        frame = mgr.update(wt._frame(rig, t, [_cluster(rig, "A", t, 2.5, 2.5, a_ids)]), rig)
    (wid_orig,) = (tr.world_id for tr in frame.tracks)
    assert frame.tracks[0].lifecycle is TrackLifecycle.CONFIRMED

    # 15초 뒤(reconnect_window_ms=10s 경과): 첫 소실 tick 에서는 LOST 이벤트를 한 번 내고 바로 DORMANT 로 이관됨
    t_lost = T + 15_000
    frame_lost = mgr.update(wt._frame(rig, t_lost, []), rig)
    assert len(frame_lost.tracks) == 1
    assert frame_lost.tracks[0].lifecycle is TrackLifecycle.LOST
    assert mgr.diag.get("lifecycle.dormant", 0) == 1
    assert wid_orig in mgr.dormant_candidates

    # 다음 tick(16초 뒤)부터는 DORMANT 상태이므로 frame.tracks 및 world_state 에 절대 송출되지 않는다
    t_absent = T + 16_000
    frame_absent = mgr.update(wt._frame(rig, t_absent, []), rig)
    assert frame_absent.tracks == ()
    assert mgr.export_tracks() == ()
    payload = world_state_payload(frame_absent, t_absent, scene_version="scene-1")
    assert payload["tracks"] == []
    msg = world_state_message(
        payload,
        msg_id="0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f",
        sent_at=t_absent,
    )
    assert contract_errors("world_state", msg) == []

    # 5분(300,000 ms < 30분) 뒤: 다른 위치(4.2, 3.8)에서 완전히 새로운 로컬 트랙 ID 로 같은 사람(emb_a)이 재입장
    t_reentry = T + 300_000
    new_ids = {c: f"{c}-reentry" for c in wt.ALL}
    _put_features(cache, new_ids, t_reentry, _blend_vec(11, 12, 0.05), orientation_bin=2)
    frame_reentry = mgr.update(
        wt._frame(rig, t_reentry, [_cluster(rig, "A_return", t_reentry, 4.2, 3.8, new_ids)]),
        rig,
    )

    assert len(frame_reentry.tracks) == 1
    snap = frame_reentry.tracks[0]
    assert snap.world_id == wid_orig
    assert snap.lifecycle is TrackLifecycle.CONFIRMED
    assert mgr.diag.get("reid.reentry_reconnected", 0) == 1
    assert wid_orig in frame_reentry.reassociations


def test_avatar_update_hold_set_on_uncertain_and_cleared_when_confirmed_high(rig) -> None:
    """7. IDENTITY_UNCERTAIN·분할·swap 의심 중 avatar_update_hold = True 설정 및 해소 시 False 복구."""
    cache = LocalTrackFeatureCache()
    cfg = WorldTrackConfig(reid_enabled=True)
    mgr = WorldTrackManager(WorldIdIssuer("hold"), cfg, feature_lookup=cache.lookup)

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    emb_a = _ortho_basis(13)

    # k=0..3: 정상 CONFIRMED + HIGH 상태에서는 avatar_update_hold == False
    for k in range(4):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a)
        f_ok = mgr.update(wt._frame(rig, t, [_cluster(rig, "A", t, 2.5, 2.5, a_ids)]), rig)
    assert f_ok.tracks[0].lifecycle is TrackLifecycle.CONFIRMED
    assert f_ok.tracks[0].identity_confidence is IdentityConfidence.HIGH
    assert f_ok.tracks[0].avatar_update_hold is False

    # k=4: 클러스터가 IDENTITY_UNCERTAIN 이 되면 avatar_update_hold == True 로 전환
    t4 = T + 4 * DT
    _put_features(cache, a_ids, t4, emb_a)
    f_unc = mgr.update(wt._frame(rig, t4, [_cluster(rig, "A", t4, 2.5, 2.5, a_ids, uncertain=True)]), rig)
    assert f_unc.tracks[0].identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
    assert f_unc.tracks[0].avatar_update_hold is True
    assert f_unc.tracks[0].hold_appearance_update is True

    # uncertain_hold_ms(1000 ms)가 지난 뒤 정상 관측되면 avatar_update_hold == False 로 복구
    t_rec = t4 + cfg.uncertain_hold_ms + DT
    _put_features(cache, a_ids, t_rec, emb_a)
    f_rec = mgr.update(wt._frame(rig, t_rec, [_cluster(rig, "A", t_rec, 2.5, 2.5, a_ids, uncertain=False)]), rig)
    assert f_rec.tracks[0].identity_confidence is IdentityConfidence.HIGH
    assert f_rec.tracks[0].avatar_update_hold is False


def test_reid_disabled_matches_geometry_only_behavior(rig) -> None:
    """8. reid_enabled = False 또는 특징 부재 시 기존 기하 추적과 동일하게 동작한다."""
    cache = LocalTrackFeatureCache()
    mgr_geom = WorldTrackManager(WorldIdIssuer("same"), WorldTrackConfig(reid_enabled=False))
    mgr_reid_off = WorldTrackManager(
        WorldIdIssuer("same"),
        WorldTrackConfig(reid_enabled=False),
        feature_lookup=cache.lookup,
    )

    a_ids = {c: f"{c}-a" for c in wt.ALL}
    emb_a = _ortho_basis(14)

    for k in range(6):
        t = T + k * DT
        _put_features(cache, a_ids, t, emb_a)
        c1 = _cluster(rig, "A", t, 2.0 + 0.1 * k, 3.0, a_ids)
        c2 = _cluster(rig, "A", t, 2.0 + 0.1 * k, 3.0, a_ids)
        f1 = mgr_geom.update(wt._frame(rig, t, [c1]), rig)
        f2 = mgr_reid_off.update(wt._frame(rig, t, [c2]), rig)
        assert [tr.world_id for tr in f1.tracks] == [tr.world_id for tr in f2.tracks]
        assert [tr.position_xy for tr in f1.tracks] == [tr.position_xy for tr in f2.tracks]
