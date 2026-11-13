"""World ID 오억제·불필요한 불확실 회귀 시험 (S15P31A101-684 작업 1~3).

외부 재현(2026-11-13 ID 감사)을 저장소 시험 도우미로 옮겼다. 각 결함 시험 옆에 정상 대조군을 둔다.
- ID-01: 이미 다른 사람으로 확인된(동시 관측) B 를 Re-ID 확장 분기가 A 의 분할 나머지로 억제하면 안 된다.
- ID-02: 시점 분할 그림자로 확인해 거른 묶음이 생존 Track 에 신원 애매 사유를 남기면 안 된다.
- ID-03: 공통으로 보이는 부위가 없는 외형 비교는 신원 근거가 되면 안 된다(운영 호출도 질의 가시성을 넘긴다).
주어진 클러스터·특징에 대한 코드 논리 시험이다. 실영상에서의 원인 비중은 녹화 재생으로 따로 잰다.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

from simai_ai.geometry.rig import CameraRig
from simai_ai.reid.collector import LocalTrackFeature, LocalTrackFeatureCache
from simai_ai.reid.visibility import PartVisibility
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackConfig, WorldTrackManager


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rd = _load("_reid684", "test_world_track_reid.py")
dg = _load("_dup684", "test_track_duplicate_guard.py")
wt = rd.wt


def _rig() -> CameraRig:
    return CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


# ------------------------------------------------------------------ ID-01


def _distinct_case(biased: bool, **cfg):
    """A·B 가 4 tick 동안 따로 보여 서로 다른 사람으로 기록된 뒤, 같은 외형의 B 가 2대만 보이며 A 쪽으로 치우친다."""
    rig = _rig()
    cache = LocalTrackFeatureCache()
    manager = WorldTrackManager(WorldIdIssuer("veto"), WorldTrackConfig(**cfg), feature_lookup=cache.lookup)
    a_ids = {c: f"{c}-a" for c in wt.ALL}
    b_ids = {c: f"{c}-b" for c in wt.ALL}
    embedding = rd._ortho_basis(0)
    for k in range(4):
        t = wt.T + k * wt.DT
        for ids in (a_ids, b_ids):
            rd._put_features(cache, ids, t, embedding)
        manager.update(wt._frame(rig, t, [rd._cluster(rig, "A", t, 3, 3, a_ids), rd._cluster(rig, "B", t, 6, 3, b_ids)]), rig)
    a, b = manager.issuer.issued
    assert manager._is_distinct(a, b), "전제: A·B 는 동시에 따로 관측됐다"
    t = wt.T + 4 * wt.DT
    sa = {c: a_ids[c] for c in ("cam1", "cam2")}
    sb = {c: b_ids[c] for c in ("cam3", "cam4")}
    for ids in (sa, sb):
        rd._put_features(cache, ids, t, embedding)
    frame = manager.update(
        wt._frame(rig, t, [rd._cluster(rig, "A", t, 3, 3, sa), rd._cluster(rig, "B", t, 3.85 if biased else 6, 3, sb)]),
        rig,
    )
    return manager, frame, a, b


def test_known_distinct_person_is_not_suppressed_by_reid_branch() -> None:
    manager, _, a, b = _distinct_case(True)
    assert manager._is_distinct(a, b)
    assert manager.diag["reid.split_suppressed_same"] == 0


def test_jumped_distinct_person_gets_flagged_new_id_not_absorbed() -> None:
    """수정 후 동작 고정(명세 5.2 "근거가 모자라면 새 ID + IDENTITY_UNCERTAIN", "갈 수 없는 곳이면 병합 금지").

    B 위치가 B Track 이 갈 수 없는 거리로 튀면 A 로 흡수되지도, B 로 이어지지도 않고 재연결 애매 표시를 단 새 ID 가 된다.
    A 는 B 의 관측 때문에 불확실해지지 않는다(수정 전에는 B 를 흡수하며 A 가 불확실해졌다).
    """
    manager, frame, a, b = _distinct_case(True)
    new = [tr for tr in frame.tracks if tr.world_id not in (a, b)]
    assert len(new) == 1 and new[0].updated
    assert str(new[0].identity_confidence) == "IDENTITY_UNCERTAIN"
    assert "REASSOCIATION_AMBIGUOUS" in map(str, new[0].identity_flags)
    assert manager.diag["reconnect_skip.unreachable"] >= 1
    assert str(frame.track(a).identity_confidence) != "IDENTITY_UNCERTAIN"
    assert not frame.track(b).updated  # B 의 옛 Track 은 위치를 갱신하지 않는다(가림으로 간다)


def test_known_distinct_control_without_bias_keeps_two_people() -> None:
    manager, frame, a, b = _distinct_case(False)
    assert {tr.world_id for tr in frame.tracks if tr.updated} == {a, b}
    assert manager.diag["reid.split_suppressed_same"] == 0


# ------------------------------------------------------------------ ID-02


def _shadow_flags(shift: float, **cfg) -> Counter:
    """예각 2쌍(남·북)이 한 사람을 두 묶음으로 나눠 보는 30 tick. 북쪽 묶음은 그림자로 걸러져야 한다(-657)."""
    acute = CameraRig.from_provider(dg.wt._DictCalibrationProvider(dg._acute_doc()))
    manager = dg._mgr(**cfg)
    for k in range(5):
        t = dg.T + k * dg.DT
        dg._step(manager, acute, k, [dg._cl(acute, "all", t, dg.X0, dg.Y0, dg._ids(dg.ACUTE))])
    flags: Counter = Counter()
    for k in range(5, 35):
        t = dg.T + k * dg.DT
        frame = dg._step(manager, acute, k, [
            dg._cl(acute, "s", t, dg.X0, dg.Y0, dg._ids(dg.SOUTH)),
            dg._cl(acute, "n", t, dg.X0, dg.Y0, dg._ids(dg.NORTH, "new"), shift),
        ])
        assert len(frame.tracks) == 1
        flags.update(map(str, frame.tracks[0].identity_flags))
    assert manager.diag["new_track.suppressed_disjoint_shadow"] == 30
    return flags


def test_proven_shadow_leaves_no_identity_flag() -> None:
    assert not _shadow_flags(0.0)


def test_supported_shadow_control_stays_certain() -> None:
    assert not _shadow_flags(14.0)


# ------------------------------------------------------------------ ID-03


def _visibility_case(query_visibility: PartVisibility, **cfg):
    rig = _rig()
    cache = LocalTrackFeatureCache()
    manager = WorldTrackManager(WorldIdIssuer("vis"), WorldTrackConfig(**cfg), feature_lookup=cache.lookup)
    embedding = rd._ortho_basis(0)
    ids = {c: f"{c}-a" for c in wt.ALL}
    cluster = rd._cluster(rig, "A", wt.T, 3, 3, ids)
    gallery = manager.galleries.get_or_create("w-vis-0001", wt.T)
    gallery.update(embedding=embedding, camera_id="cam1", t_ms=wt.T, quality=0.9,
                   visibility=PartVisibility(False, True, False), orientation_bin=1)
    feat = LocalTrackFeature("cam1", "cam1-a", 1, wt.T, embedding, query_visibility, 0.9, 1)
    manager._tick_features = {("cam1", "cam1-a"): feat}
    production = manager._cluster_gallery_sim("w-vis-0001", cluster, wt.T)
    explicit = gallery.similarity(embedding, camera_id="cam1", query_visibility=query_visibility)
    return production, explicit


def test_disjoint_visible_parts_are_not_identity_evidence() -> None:
    production, explicit = _visibility_case(PartVisibility(False, False, True))
    assert explicit is None, "전제: gallery API 는 공통 가시 부위가 없으면 비교하지 않는다"
    assert production is None


def test_shared_visible_torso_control_matches() -> None:
    production, explicit = _visibility_case(PartVisibility(False, True, False))
    assert production == explicit == 1.0


# ------------------------------------------------------------------ 재생 비교용 스위치 (끄면 이전 동작)


def test_switches_off_reproduce_previous_behavior() -> None:
    """같은 녹화로 원인 비중을 나눠 재려면 수정마다 이전 동작을 다시 낼 수 있어야 한다."""
    manager, _, _, _ = _distinct_case(True, reid_split_respect_distinct=False)
    assert manager.diag["reid.split_suppressed_same"] == 1
    assert _shadow_flags(0.0, shadow_clears_ambiguity=False) == Counter({"AMBIGUOUS_TRACK_MATCH": 30})
    production, _ = _visibility_case(PartVisibility(False, False, True), reid_query_visibility=False)
    assert production == 1.0


def test_shadow_clearing_is_counted() -> None:
    acute = CameraRig.from_provider(dg.wt._DictCalibrationProvider(dg._acute_doc()))
    manager = dg._mgr()
    for k in range(5):
        dg._step(manager, acute, k, [dg._cl(acute, "all", dg.T + k * dg.DT, dg.X0, dg.Y0, dg._ids(dg.ACUTE))])
    for k in range(5, 10):
        t = dg.T + k * dg.DT
        dg._step(manager, acute, k, [
            dg._cl(acute, "s", t, dg.X0, dg.Y0, dg._ids(dg.SOUTH)),
            dg._cl(acute, "n", t, dg.X0, dg.Y0, dg._ids(dg.NORTH, "new")),
        ])
    assert manager.diag["identity.shadow_ambiguity_cleared"] == 5
