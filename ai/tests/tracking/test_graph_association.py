"""S15P31A101-464: 공간 그래프 + 시간 간선, 카메라 쌍별 문턱, 시간 그래프 연결 인터페이스 단위 시험."""

from __future__ import annotations

import math

import numpy as np
import pytest

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.cross_view import (
    CrossViewAssociator,
    CrossViewConfig,
    LocalView,
    PairEvidence,
    RejectReason,
    TrackKey,
    UncertainReason,
)
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
    resolve_temporal_graph,
    temporal_edge_cost,
)

from .test_cross_view import CORNERS, T, DictCalibrationProvider, calibration_doc, lv, project, skeleton, views_of


@pytest.fixture
def rig() -> CameraRig:
    return CameraRig.from_provider(DictCalibrationProvider(calibration_doc()))


def _find_shift_for_residual(
    rig: CameraRig,
    person: np.ndarray,
    cam_shifted: str,
    cam_ref: str,
    lo_px: float,
    hi_px: float,
) -> tuple[float, float]:
    """`cam_shifted` 의 2D 관절 전체를 이동시켜 `cam_ref` 와의 2-view 잔차가 `[lo_px, hi_px]` 에 들어오는 이동량(px)을 찾는다."""
    probe = CrossViewAssociator(
        rig,
        CrossViewConfig(
            epipolar_gate_px=60.0,
            residual_marginal_px=8.0,
            residual_gate_px=20.0,
            pairwise_gate_max_px=20.0,
        ),
    )
    pa = probe._prepare(lv(cam_ref, f"{cam_ref}-a", project(rig, cam_ref, person)))
    for dy in np.linspace(4.0, 28.0, 97):
        for dx in (0.0, 3.0, -3.0):
            pb = probe._prepare(lv(cam_shifted, f"{cam_shifted}-a", project(rig, cam_shifted, person, shift=(float(dx), float(dy)))))
            ev = probe._pair(pa, pb)
            if ev.residual_px is not None and lo_px <= ev.residual_px <= hi_px and ev.epipolar_px is not None and ev.epipolar_px <= 28.0:
                return float(dx), float(dy)
    raise AssertionError(f"{cam_shifted}/{cam_ref} 에서 잔차 [{lo_px}, {hi_px}] 구간을 찾지 못했다")


def test_two_plus_two_split_scene_merges_with_strong_bridge_or_temporal_prior(rig: CameraRig) -> None:
    """2+2 분할 장면이 제약 그래프 분할과 시간 간선으로 한 4대 클러스터·한 World ID 로 합쳐진다."""
    person = skeleton(4.0, 3.2)
    person_c34 = [pt + np.array([0.04, -0.03, 0.06]) for pt in person]

    tick0_views = views_of(rig, {"a": person}, t=T)
    tick1_views = views_of(rig, {"a": person}, t=T + 100, cameras=("cam1", "cam2")) + views_of(
        rig, {"a": person_c34}, t=T + 100, cameras=("cam3", "cam4")
    )
    probe = CrossViewAssociator(rig)
    prep = {v.key: probe._prepare(v) for v in tick1_views}
    raw_pairs = probe._pairs(prep)
    cross_res = [
        ev.residual_px
        for (ka, kb), ev in raw_pairs.items()
        if (ka[0] in ("cam1", "cam2")) != (kb[0] in ("cam1", "cam2")) and ev.residual_px is not None
    ]
    assert len(cross_res) == 4
    marg_px = min(cross_res) - 0.5
    gate_px = max(cross_res) + 1.0
    assert marg_px > 1.0  # 그룹 내부 잔차(0 px)는 충분히 아래다

    # 기존 탐욕 완전 연결(graph_clustering_enabled=False, temporal_prior_enabled=False)에서는 교차 4쌍이 모두 MARGINAL_RESIDUAL 이라 2+2 로 쪼개진다
    legacy_assoc = CrossViewAssociator(
        rig,
        CrossViewConfig(
            residual_marginal_px=marg_px,
            residual_gate_px=gate_px,
            pairwise_marginal_max_px=max(12.0, marg_px),
            pairwise_gate_max_px=max(16.0, gate_px),
            graph_clustering_enabled=False,
            temporal_prior_enabled=False,
            pairwise_threshold_enabled=False,
        ),
    )
    legacy_mgr = WorldTrackManager(WorldIdIssuer("legacy464"), WorldTrackConfig(split_suppression_enabled=False))
    f0_leg = legacy_assoc.associate(T, tick0_views)
    legacy_mgr.update(f0_leg, rig)
    f1_leg = legacy_assoc.associate(T + 100, tick1_views)
    assert len(f1_leg.clusters) == 2
    assert sorted(len(c.views) for c in f1_leg.clusters) == [2, 2]
    assert all(UncertainReason.MARGINAL_RESIDUAL in c.uncertain_reasons for c in f1_leg.clusters)

    # 새 공간 그래프 + 시간 간선에서는 직전 World Track 소속 쌍의 시간 간선이 경계 잔차를 이겨 4대 단일 클러스터·단일 World ID 를 유지한다
    graph_assoc = CrossViewAssociator(
        rig,
        CrossViewConfig(
            residual_marginal_px=marg_px,
            residual_gate_px=gate_px,
            pairwise_marginal_max_px=max(12.0, marg_px),
            pairwise_gate_max_px=max(16.0, gate_px),
        ),
    )
    graph_mgr = WorldTrackManager(WorldIdIssuer("graph464"), WorldTrackConfig(split_suppression_enabled=False))
    f0_new = graph_assoc.associate(T, tick0_views, prior_groups=graph_mgr.temporal_prior_groups(T))
    w0_new = graph_mgr.update(f0_new, rig)
    assert len(w0_new.tracks) == 1
    wid = w0_new.tracks[0].world_id

    f1_new = graph_assoc.associate(T + 100, tick1_views, prior_groups=graph_mgr.temporal_prior_groups(T + 100))
    assert len(f1_new.clusters) == 1
    assert set(f1_new.clusters[0].camera_ids) == set(CORNERS)
    assert not f1_new.clusters[0].identity_uncertain
    w1_new = graph_mgr.update(f1_new, rig)
    assert [tr.world_id for tr in w1_new.tracks] == [wid]
    assert len(graph_mgr.issuer.issued) == 1


def test_two_plus_two_with_one_strong_cross_edge_merges_without_prior(rig: CameraRig) -> None:
    """이력이 없는 첫 시각이라도 {cam1, cam2} 와 {cam3, cam4} 사이에 강한 간선이 하나 이상 있고 나머지가 경계 잔차면 하나로 묶인다."""
    person = skeleton(4.0, 3.2)
    dx, dy = _find_shift_for_residual(rig, person, "cam4", "cam1", 8.5, 11.5)
    # cam1, cam2, cam3 는 정확하고 cam4 만 경계 잔차(8.5~11.5 px)인 경우:
    # 기존 _linkage 는 {cam1, cam2, cam3} 와 {cam4} 병합 시 경계 잔차 쌍 하나 때문에 병합을 막았으나,
    # 제약 그래프 분할은 강한 간선 신장 부분그래프와 3D 일관성이 성립하면 4대 한 클러스터로 묶는다.
    views = [
        lv("cam1", "cam1-a", project(rig, "cam1", person), T),
        lv("cam2", "cam2-a", project(rig, "cam2", person), T),
        lv("cam3", "cam3-a", project(rig, "cam3", person), T),
        lv("cam4", "cam4-a", project(rig, "cam4", person, shift=(dx, dy)), T),
    ]
    frame = CrossViewAssociator(rig).associate(T, views)
    assert len(frame.clusters) == 1
    assert set(frame.clusters[0].camera_ids) == set(CORNERS)
    assert not frame.clusters[0].identity_uncertain


def test_close_two_people_keep_two_ids_with_and_without_disproof(rig: CameraRig) -> None:
    """가까운 두 사람(같은 카메라 동시 검출 반증이 있을 때와 없을 때 모두)은 2개 클러스터·2개 World ID 를 유지한다."""
    p_a = skeleton(4.0, 3.0)
    p_b = skeleton(4.0, 3.55)  # 0.55 m 간격

    # (1) 반증 있음: 4대 카메라 모두 두 사람을 각각 검출 (총 8개 view)
    assoc_with = CrossViewAssociator(rig)
    mgr_with = WorldTrackManager(WorldIdIssuer("closewith"))
    for step in range(4):
        t = T + 100 * step
        views = views_of(rig, {"a": p_a, "b": p_b}, t=t)
        af = assoc_with.associate(t, views, prior_groups=mgr_with.temporal_prior_groups(t))
        wf = mgr_with.update(af, rig)
        assert len(af.clusters) == 2
        assert len(wf.tracks) == 2
        for c in af.clusters:
            assert len({k.split("-")[1] for _, k in c.keys}) == 1
    assert len(mgr_with.issuer.issued) == 2

    # (2) 반증 없음: 사람 A 는 (cam1, cam2) 에만, 사람 B 는 (cam3, cam4) 에만 검출 (카메라가 겹치지 않는 2+2)
    assoc_no = CrossViewAssociator(rig)
    mgr_no = WorldTrackManager(WorldIdIssuer("closeno"))
    for step in range(4):
        t = T + 100 * step
        views = views_of(rig, {"a": p_a}, t=t, cameras=("cam1", "cam2")) + views_of(
            rig, {"b": p_b}, t=t, cameras=("cam3", "cam4")
        )
        af = assoc_no.associate(t, views, prior_groups=mgr_no.temporal_prior_groups(t))
        wf = mgr_no.update(af, rig)
        assert len(af.clusters) == 2
        assert len(wf.tracks) == 2
    assert len(mgr_no.issuer.issued) == 2


def test_geometrically_impossible_pair_breaks_temporal_prior_and_triangle_check(rig: CameraRig) -> None:
    """기하적으로 불가능한 쌍은 강한 시간 간선이 있어도 끊기며, A–B·B–C 강함인데 A–C 불가인 삼각 불일치를 한 클러스터로 묶지 않는다."""
    person_a = skeleton(3.0, 3.0)
    person_b = skeleton(6.8, 5.5)

    assoc = CrossViewAssociator(rig)
    mgr = WorldTrackManager(WorldIdIssuer("break464"))
    # tick 0: 정상 4대 관측으로 시간 간선 형성
    f0 = assoc.associate(T, views_of(rig, {"a": person_a}, t=T))
    mgr.update(f0, rig)
    assert len(mgr.temporal_prior_groups(T)) == 1

    # tick 1: cam4-a 의 로컬 트랙 ID 가 멀리 떨어진 다른 사람(person_b)으로 점프한 경우 -> 시간 간선이 있어도 끊어야 한다
    views_t1 = views_of(rig, {"a": person_a}, t=T + 100, cameras=("cam1", "cam2", "cam3")) + [
        lv("cam4", "cam4-a", project(rig, "cam4", person_b), T + 100)
    ]
    f1 = assoc.associate(T + 100, views_t1, prior_groups=mgr.temporal_prior_groups(T + 100))
    c_main = f1.cluster_of(("cam1", "cam1-a"))
    c_jump = f1.cluster_of(("cam4", "cam4-a"))
    assert c_main is not None and c_jump is not None
    assert set(c_main.camera_ids) == {"cam1", "cam2", "cam3"}
    assert c_jump.keys == (("cam4", "cam4-a"),)
    assert assoc.link_age(("cam1", "cam1-a"), ("cam4", "cam4-a")) == 0

    # 삼각 일관성 검사 + 전역 그래프 분할:
    # A(cam1)–B(cam2) 강함, B(cam2)–C(cam3) 강함(더 낮은 비용), D(cam4) 는 A·B 와 강함인데
    # A–C 와 C–D 는 기하 불가(EPIPOLAR)일 때:
    # 1) {A, B, C} 는 시간 간선이 있어도 절대 한 클러스터로 묶이지 않는다.
    # 2) 탐욕 병합은 B–C 를 먼저 합쳐 {B, C} + {A, D} (2+2)로 쪼개지만, 제약 그래프 분할은 3대 클러스터 {A, B, D} + {C} 를 고른다.
    ka: TrackKey = ("cam1", "cam1-a")
    kb: TrackKey = ("cam2", "cam2-a")
    kc: TrackKey = ("cam3", "cam3-a")
    kd: TrackKey = ("cam4", "cam4-a")
    prepared = {v.key: assoc._prepare(v) for v in views_of(rig, {"a": person_a}, t=T + 200)}
    synthetic_pairs = {
        (ka, kb): PairEvidence(ka, kb, 12, epipolar_px=2.0, residual_px=2.0, consistent_joints=12, cost=0.65),
        (kb, kc): PairEvidence(kb, kc, 12, epipolar_px=1.5, residual_px=1.5, consistent_joints=12, cost=0.55),
        (ka, kc): PairEvidence(ka, kc, 12, epipolar_px=45.0, reject=RejectReason.EPIPOLAR, cost=math.inf),
        (ka, kd): PairEvidence(ka, kd, 12, epipolar_px=2.0, residual_px=2.0, consistent_joints=12, cost=0.65),
        (kb, kd): PairEvidence(kb, kd, 12, epipolar_px=2.0, residual_px=2.0, consistent_joints=12, cost=0.65),
        (kc, kd): PairEvidence(kc, kd, 12, epipolar_px=45.0, reject=RejectReason.EPIPOLAR, cost=math.inf),
    }
    prior_all = {(ka, kb), (kb, kc), (ka, kc), (ka, kd), (kb, kd), (kc, kd)}
    flags_graph: list = []
    works_graph = assoc._assign_graph(prepared, synthetic_pairs, {}, prior_all, flags_graph)
    clusters_graph = {frozenset(w.keys) for w in works_graph}
    assert clusters_graph == {frozenset({ka, kb, kd}), frozenset({kc})}


def test_pairwise_thresholds_from_calibration_and_runtime_distribution(rig: CameraRig) -> None:
    """카메라 쌍별 문턱이 보정 오차 설정과 실행 중 잔차 분포 분위수에서 각각 계산되고 상한을 넘지 않는다."""
    # 1) 보정 카메라 쌍별 기준 재투영 오차 설정 사용 + 상한 클램프 확인
    cfg_cal = CrossViewConfig(
        pair_reprojection_error_px={"cam1/cam2": 9.5, "cam3-cam4": 20.0},
        pairwise_marginal_scale=1.0,
        pairwise_gate_scale=1.35,
        pairwise_marginal_max_px=11.5,
        pairwise_gate_max_px=15.0,
    )
    assoc_cal = CrossViewAssociator(rig, cfg_cal)
    m12, g12 = assoc_cal.pair_residual_thresholds("cam2", "cam1")
    assert m12 == pytest.approx(9.5)
    assert g12 == pytest.approx(min(9.5 * 1.35, 15.0))
    # 상한 클램프 확인 (20.0 px 기준이어도 marginal <= 11.5, gate <= 15.0)
    m34, g34 = assoc_cal.pair_residual_thresholds("cam3", "cam4")
    assert m34 == pytest.approx(11.5)
    assert g34 == pytest.approx(15.0)
    # 설정에 없는 쌍은 전역 기본값 (8.0, 12.0)
    assert assoc_cal.pair_residual_thresholds("cam1", "cam3") == (8.0, 12.0)

    # 2) 실행 중 관측 잔차 분포 분위수 기반 문턱 + replace_rig 초기화 확인
    cfg_rt = CrossViewConfig(
        pairwise_min_samples=10,
        pairwise_marginal_quantile=0.80,
        pairwise_gate_quantile=0.95,
        pairwise_marginal_max_px=12.0,
        pairwise_gate_max_px=16.0,
    )
    assoc_rt = CrossViewAssociator(rig, cfg_rt)
    for r in [6.0, 7.0, 8.0, 9.0, 9.5, 10.0, 10.5, 11.0, 13.0, 14.5]:
        assoc_rt.record_pair_residual("cam2", "cam3", r)
    m_rt, g_rt = assoc_rt.pair_residual_thresholds("cam2", "cam3")
    assert 10.5 <= m_rt <= 12.0
    assert 13.0 <= g_rt <= 16.0
    assoc_rt.replace_rig(rig)
    assert assoc_rt.pair_residual_thresholds("cam2", "cam3") == (8.0, 12.0)


def test_temporal_edge_cost_and_resolve_temporal_graph_interface(rig: CameraRig) -> None:
    """시간 그래프 간선 인터페이스(`temporal_edge_cost`, `resolve_temporal_graph`)가 위치+시간+외형 비용을 분리해 계산한다."""
    person = skeleton(3.0, 3.0)
    frame = CrossViewAssociator(rig).associate(T, views_of(rig, {"a": person}, t=T))
    cluster = frame.clusters[0]
    cfg = WorldTrackConfig(temporal_time_weight=0.2, appearance_weight=0.5)

    edge_default = temporal_edge_cost(
        "w-test-0001",
        (3.0, 3.0),
        T - 500,
        cluster,
        (3.2, 3.0),
        T,
        cfg,
        shared_keys=2,
    )
    assert edge_default.feasible
    assert edge_default.appearance_cost == 0.0
    assert edge_default.time_cost > 0.0

    edge_with_reid = temporal_edge_cost(
        "w-test-0001",
        (3.0, 3.0),
        T - 500,
        cluster,
        (3.2, 3.0),
        T,
        cfg,
        shared_keys=2,
        appearance_cost=0.4,
    )
    assert edge_with_reid.appearance_cost == pytest.approx(0.2)
    assert edge_with_reid.total_cost == pytest.approx(edge_default.total_cost + 0.2)

    # 기하 게이트 밖 후보는 외형 비용이 0 이어도 탈락한다
    edge_far = temporal_edge_cost(
        "w-test-0002",
        (0.0, 0.0),
        T - 100,
        cluster,
        (3.2, 3.0),
        T,
        cfg,
        appearance_cost=0.0,
    )
    assert not edge_far.feasible and math.isinf(edge_far.total_cost)

    matched, ambiguous = resolve_temporal_graph([edge_with_reid, edge_far], ambiguity_margin=cfg.ambiguity_margin)
    assert matched == {"w-test-0001": cluster.association_id}
    assert ambiguous == set()
