"""묶음 왜곡 제거·묶음 삼각측량(S15P31A101-168)이 이전 구현과 결과가 같은지 본다 (명세 5.2).

기준은 이전 구현(관절·카메라마다 1점씩 왜곡 제거, 관절마다 가우스-뉴턴)의 고정 사본 `legacy_geometry.py` 다.
무작위 장면(`bench_tier0_geometry.py` 의 장면 생성기)을 여러 렌즈 모델·시드로 만들어 같은 입력을 두 구현에 넣는다.
거친 장면에는 이상치 keypoint(30~80 px), NaN 값, 왜곡 식이 접히는 영역(k1 만 있는 렌즈의 영상 모서리),
영상 모서리 keypoint(왜곡 식이 접히는 렌즈에서 왜곡 제거 실패), 카메라 통째 가림(2대 관측 관절·경쟁 후보 검사)을 넣는다.

허용 오차 (부동소수 연산 순서 차이만 허용한다. 묶음 연산은 같은 식을 행렬로 한 번에 계산해 마지막 자리가 다르고,
가우스-뉴턴은 걸음이 1e-9 m 미만이면 멈추므로 멈추는 반복이 하나 달라지면 해가 1e-9 m 수준으로 다를 수 있다):
- 정규화 좌표 1e-12, World XYZ 1e-6 m, 재투영 오차·에피폴라 거리·쌍 잔차 1e-5 px, 시선각 1e-6 rad,
  삼각측량 표준편차 1e-9 m, 속도 1e-6 m/s, 쌍 비용 1e-6, 바닥 가설 거리 1e-9 m.
- 범주형 결과(관절 상태·사유·쓴 카메라·뺀 카메라·클러스터 구성·association_id·IDENTITY_UNCERTAIN 사유)는 완전히 같아야 한다.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry import joint_gate as joint_gate_module
from simai_ai.geometry.joint_gate import (
    JointGate,
    JointGateConfig,
    epipolar_distance_px,
)
from simai_ai.geometry.triangulation import (
    TriangulationConfig,
    reprojection_errors,
    triangulate_point,
    triangulate_skeleton,
    triangulate_skeletons,
)
from simai_ai.tracking import cross_view as cross_view_module
from simai_ai.tracking.cross_view import CrossViewAssociator, _Work

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("bench_tier0_geometry")
legacy = _load("legacy_geometry")

ATOL_NORMALIZED = 1e-12
ATOL_XYZ_M = 1e-6
ATOL_PX = 1e-5
ATOL_RAD = 1e-6
ATOL_SIGMA_M = 1e-9
ATOL_SPEED = 1e-6
ATOL_COST = 1e-6
ATOL_FLOOR_M = 1e-9

# 렌즈 모델 (model, coeffs, 보정 상태)
LENSES = {
    "wide": ("OPENCV_PINHOLE", bench.WIDE_COEFFS, "PROVISIONAL"),
    "none": ("NONE", (), "PROVISIONAL"),
    "fold": ("OPENCV_PINHOLE", (-0.30, 0.0, 0.0, 0.0, 0.0), "PROVISIONAL"),
    "tangential": ("OPENCV_PINHOLE", (-0.28, 0.08, 0.0012, -0.0009, 0.0), "PROVISIONAL"),
    "rational": ("OPENCV_PINHOLE", (-0.25, 0.05, 0.0005, 0.0004, 0.01, 0.02, -0.01, 0.003), "PROVISIONAL"),
    "fisheye": ("OPENCV_FISHEYE", (-0.02, 0.003, -0.001, 0.0002), "PROVISIONAL"),
    "drifted": ("OPENCV_PINHOLE", bench.WIDE_COEFFS, "DRIFTED"),
}
HARSH = {"outlier_rate": 0.15, "nan_rate": 0.05, "corner_rate": 0.03}


def _rig(lens: str):
    model, coeffs, state = LENSES[lens]
    return bench.make_rig(coeffs, state, model)


def _ticks(lens: str, seed: int, n_people: int, n_ticks: int, harsh: bool):
    rng = np.random.default_rng(seed)
    rig = _rig(lens)
    people = bench.make_people(n_people, rng)
    kwargs = dict(HARSH, camera_drop_rate=0.25) if harsh else {}
    out = []
    for k in range(n_ticks):
        out.append(bench.tick_input(rig, people, bench.T0 + k * bench.TICK_MS, rng, **kwargs))
        bench.step_people(people, rng)
    return rig, out


def _legacy_triangulate(views, rig, joint_layout="COCO17", config=None, *, undistorted=None):
    return legacy.triangulate_skeleton(views, rig, joint_layout, config or TriangulationConfig())


def _assert_close(new, old, atol, what):
    if old is None or new is None:
        assert new is None and old is None, f"{what}: {new} != {old}"
        return
    new, old = np.asarray(new, dtype=float), np.asarray(old, dtype=float)
    np.testing.assert_allclose(new, old, rtol=0, atol=atol, err_msg=what)


def assert_same_skeleton(new, old, where="", camera_order=True):
    """camera_order=False: 관절의 camera_ids·rejected_camera_ids 를 순서 없이 비교한다(view 를 넘긴 순서만 다를 때)."""
    assert new.t == old.t and new.joint_layout == old.joint_layout
    assert new.calibration_version == old.calibration_version
    assert new.camera_ids == old.camera_ids, where
    order = (lambda ids: ids) if camera_order else sorted
    for j, (a, b) in enumerate(zip(new.joints, old.joints, strict=True)):
        w = f"{where} 관절 {j}"
        assert a.validity == b.validity, w
        assert a.issues == b.issues, w
        assert order(a.camera_ids) == order(b.camera_ids), w
        assert order(a.rejected_camera_ids) == order(b.rejected_camera_ids), w
        _assert_close(a.xyz, b.xyz, ATOL_XYZ_M, w)
        _assert_close(a.reprojection_error_px, b.reprojection_error_px, ATOL_PX, w)
        _assert_close(a.ray_angle_rad, b.ray_angle_rad, ATOL_RAD, w)


# ---------------------------------------------------------------- 왜곡 제거 묶음


@pytest.mark.parametrize("lens", sorted(LENSES))
def test_undistort_keypoints_matches_point_by_point(lens):
    rig = _rig(lens)
    rng = np.random.default_rng(11)
    for cam in rig.cameras.values():
        w, h = cam.image_size
        uv = np.column_stack([rng.uniform(-50, w + 50, 60), rng.uniform(-50, h + 50, 60)])
        conf = rng.uniform(0.0, 1.0, 60)
        kps = [(float(u), float(v), float(c)) for (u, v), c in zip(uv, conf)]
        kps[3] = None
        kps[5] = (math.nan, 10.0, 0.9)
        kps[7] = (10.0, 10.0, math.inf)
        und = cam.undistort_keypoints(kps)
        for j, kp in enumerate(kps):
            if kp is None or not all(math.isfinite(x) for x in kp):
                assert np.isnan(und.normalized[j]).all() and not und.undistorted[j]
                continue
            xn, ok = cam.normalize_with_mask(np.array([kp[:2]]))
            assert bool(ok[0]) == bool(und.undistorted[j])
            if ok[0]:
                _assert_close(und.normalized[j], xn[0], ATOL_NORMALIZED, f"{cam.camera_id} {j}")
                _assert_close(und.ideal_pixels[j], cam.ideal_pixels(xn)[0], ATOL_PX, f"{cam.camera_id} {j}")
            else:
                assert np.isnan(und.normalized[j]).all() and np.isnan(und.ideal_pixels[j]).all()
        # 관측으로 세는 관절은 이전 구현의 판정과 같다
        config = TriangulationConfig()
        assert und.usable(config.min_keypoint_confidence).tolist() == legacy._usable_keypoints(cam, kps, config)
        with pytest.raises(ValueError):
            und.normalized[0, 0] = 1.0  # 읽기 전용 (다시 쓰는 쪽이 고치지 못한다)


def test_fold_lens_actually_has_unsolvable_keypoints():
    """fold 렌즈 장면에 왜곡 제거 실패 keypoint 가 실제로 있어야 아래 비교가 그 경로를 덮는다."""
    rig, ticks = _ticks("fold", seed=3, n_people=10, n_ticks=1, harsh=True)
    failed = 0
    for lv in ticks[0].local_views:
        und = rig.cameras[lv.camera_id].undistort_keypoints(lv.view.keypoints)
        present = np.array([kp is not None and all(math.isfinite(x) for x in kp) for kp in lv.view.keypoints])
        failed += int((present & ~und.undistorted).sum())
    assert failed > 0


# ---------------------------------------------------------------- 삼각측량


@pytest.mark.parametrize("lens", sorted(LENSES))
@pytest.mark.parametrize("harsh", [False, True], ids=["clean", "harsh"])
def test_triangulate_skeleton_matches_legacy(lens, harsh):
    issues_seen = set()
    for seed in range(2):
        rig, ticks = _ticks(lens, seed=100 + seed, n_people=4, n_ticks=1, harsh=harsh)
        for i, views in enumerate(ticks[0].per_person):
            if not views:
                continue
            new = triangulate_skeleton(views, rig)
            old = legacy.triangulate_skeleton(views, rig)
            assert_same_skeleton(new, old, f"{lens} seed {seed} 사람 {i}")
            issues_seen.update(issue for j in new.joints for issue in j.issues)
            # 미리 왜곡 제거한 결과를 넘겨도 같다
            und = {cid: rig.cameras[cid].undistort_keypoints(v.keypoints) for cid, v in views.items()}
            assert_same_skeleton(triangulate_skeleton(views, rig, undistorted=und), old)
    if harsh:
        # 거친 장면이 이상치 제거·관측 부족 경로를 실제로 지난다
        assert {"TOO_FEW_VIEWS"} <= {str(x) for x in issues_seen}


@pytest.mark.parametrize("lens", ["wide", "fold", "fisheye"])
def test_triangulate_skeletons_matches_legacy_per_person(lens):
    """여러 사람을 한 번에 풀어도 사람마다 이전 구현으로 푼 것과 같다."""
    rig, ticks = _ticks(lens, seed=150, n_people=6, n_ticks=1, harsh=True)
    people = [views for views in ticks[0].per_person if views]
    for n, (new, views) in enumerate(zip(triangulate_skeletons(people, rig), people, strict=True)):
        assert_same_skeleton(new, legacy.triangulate_skeleton(views, rig), f"{lens} 사람 {n}")
    assert triangulate_skeletons([], rig) == []
    with pytest.raises(ValueError):
        triangulate_skeletons(people, rig, undistorted=[None])


def test_harsh_scenes_cover_outlier_rejection():
    """이상치 관측을 빼고 다시 푸는 경로(rejected_camera_ids)와 INCONSISTENT_VIEWS 를 비교 장면이 실제로 지난다."""
    rejected = inconsistent = 0
    for seed in range(3):
        rig, ticks = _ticks("wide", seed=100 + seed, n_people=4, n_ticks=1, harsh=True)
        for views in ticks[0].per_person:
            if views:
                for j in triangulate_skeleton(views, rig).joints:
                    rejected += bool(j.rejected_camera_ids)
                    inconsistent += "INCONSISTENT_VIEWS" in {str(x) for x in j.issues}
    assert rejected > 0 and inconsistent > 0


def test_stale_undistorted_is_not_used():
    """다른 keypoint 로 만든 왜곡 제거 결과를 넘기면 쓰지 않고 다시 푼다(결과가 이전 구현과 같다)."""
    rig, ticks = _ticks("wide", seed=5, n_people=2, n_ticks=2, harsh=False)
    views_now, views_before = ticks[1].per_person[0], ticks[0].per_person[0]
    stale = {cid: rig.cameras[cid].undistort_keypoints(v.keypoints) for cid, v in views_before.items()}
    got = triangulate_skeleton(views_now, rig, undistorted=stale)
    assert_same_skeleton(got, legacy.triangulate_skeleton(views_now, rig))


@pytest.mark.parametrize("lens", ["wide", "tangential", "fisheye"])
def test_point_api_matches_legacy(lens):
    rig = _rig(lens)
    rng = np.random.default_rng(21)
    cams = list(rig.cameras.values())
    for _ in range(40):
        X = np.array([rng.uniform(1, 8), rng.uniform(1, 6.5), rng.uniform(0, 2)])
        k = int(rng.integers(2, 5))
        sub = [cams[i] for i in rng.choice(4, k, replace=False)]
        px = np.vstack([bench.project(c, X[None])[0] for c in sub]) + rng.normal(0, 2.0, (k, 2))
        if rng.random() < 0.2:
            px[0] += 60.0
        w = rng.uniform(0.3, 1.0, k)
        new, old = triangulate_point(sub, px, w), legacy.triangulate_point(sub, px, w)
        _assert_close(new, old, ATOL_XYZ_M, "triangulate_point")
        if old is not None:
            reproj_new, reproj_old = reprojection_errors(sub, px, new), legacy.reprojection_errors(sub, px, old)
            _assert_close(reproj_new, reproj_old, ATOL_PX, "reproj")
        a, b = sub[0], sub[1]
        epi_new, epi_old = epipolar_distance_px(a, px[0], b, px[1]), legacy.epipolar_distance_px(a, px[0], b, px[1])
        _assert_close(epi_new, epi_old, ATOL_PX, "epi")


# ---------------------------------------------------------------- 관절 게이트


def _legacy_competitor(j, cams, pixels, others, cfg, undistorted, others_undistorted=None):
    return legacy._has_epipolar_competitor(j, cams, pixels, others, cfg)


def _legacy_covariances(cameras, mask, X, sigma_px):
    cov = np.full((len(X), 3, 3), np.nan)
    ok = np.zeros(len(X), dtype=bool)
    for m in range(len(X)):
        c = legacy.triangulation_covariance([cameras[k] for k in np.flatnonzero(mask[m])], X[m], sigma_px)
        if c is not None:
            cov[m], ok[m] = c, True
    return cov, ok


def _patch_legacy_gate(m):
    m.setattr(joint_gate_module, "triangulate_skeleton", _legacy_triangulate)
    m.setattr(joint_gate_module, "_has_epipolar_competitor", _legacy_competitor)
    m.setattr(joint_gate_module, "_triangulation_covariances", _legacy_covariances)


def assert_same_gated(new, old, where):
    assert new.camera_ids == old.camera_ids, where
    assert new.identity_uncertain_candidate == old.identity_uncertain_candidate, where
    for j, (a, b) in enumerate(zip(new.joints, old.joints, strict=True)):
        w = f"{where} 관절 {j}"
        assert a.validity == b.validity, w
        assert a.reasons == b.reasons, w
        assert a.camera_ids == b.camera_ids, w
        _assert_close(a.xyz, b.xyz, ATOL_XYZ_M, w)
        _assert_close(a.reprojection_error_px, b.reprojection_error_px, ATOL_PX, w)
        _assert_close(a.ray_angle_rad, b.ray_angle_rad, ATOL_RAD, w)
        if a.sigma_max_m is not None and math.isinf(a.sigma_max_m):
            assert b.sigma_max_m is not None and math.isinf(b.sigma_max_m), w
        else:
            _assert_close(a.sigma_xyz_m, b.sigma_xyz_m, ATOL_SIGMA_M, w)
            _assert_close(a.sigma_max_m, b.sigma_max_m, ATOL_SIGMA_M, w)
        _assert_close(a.speed_mps, b.speed_mps, ATOL_SPEED, w)


@pytest.mark.parametrize("lens", ["wide", "fold", "fisheye"])
def test_joint_gate_matches_legacy(lens, monkeypatch):
    rig, ticks = _ticks(lens, seed=200, n_people=5, n_ticks=8, harsh=True)
    n = len(ticks[0].per_person)
    new_gates = [JointGate() for _ in range(n)]
    old_gates = [JointGate() for _ in range(n)]
    reasons = set()
    for k, inp in enumerate(ticks):
        for i, views in enumerate(inp.per_person):
            if not views:
                continue
            others = bench.others_for(inp.per_person, i)
            if k % 3 == 2:
                others = None  # 다른 사람 검출을 모름
            new = new_gates[i].update(views, rig, others)
            with monkeypatch.context() as m:
                _patch_legacy_gate(m)
                old = old_gates[i].update(views, rig, others)
            assert_same_gated(new, old, f"{lens} tick {k} 사람 {i}")
            reasons.update(str(r) for gj in new.joints for r in gj.reasons)
    # 경쟁 후보 검사(2대 관측)와 시간 일관성 경로를 실제로 지난다
    assert {"COMPETITOR_CHECK_UNAVAILABLE"} <= reasons


def test_joint_gate_competitor_paths_match_legacy(monkeypatch):
    """두 사람을 가깝게 세워 2대 관측 경쟁 후보(TWO_VIEW_COMPETITOR)가 실제로 나오는 장면도 같다."""
    rng = np.random.default_rng(7)
    rig = bench.make_rig()
    people = bench.make_people(2, rng)
    people[1].xy = people[0].xy + np.array([0.0, 0.62])
    people[1].yaw = people[0].yaw
    found = 0
    for k in range(12):
        inp = bench.tick_input(rig, people, bench.T0 + k * bench.TICK_MS, rng)
        bench.step_people(people, rng)
        for i, views in enumerate(inp.per_person):
            two = {cid: views[cid] for cid in sorted(views)[:2]}  # 두 대만 남겨 2대 관측을 만든다
            if len(two) < 2:
                continue
            others = bench.others_for(inp.per_person, i)
            new = JointGate().update(two, rig, others)
            with monkeypatch.context() as m:
                _patch_legacy_gate(m)
                old = JointGate().update(two, rig, others)
            assert_same_gated(new, old, f"tick {k} 사람 {i}")
            # 다른 사람 keypoint 의 왜곡 제거 결과를 넘겨도 같다
            others_und = {cid: [rig.cameras[cid].undistort_keypoints(kps) for kps in kl] for cid, kl in others.items()}
            assert_same_gated(JointGate().update(two, rig, others, others_undistorted=others_und), old, f"tick {k}")
            found += new.identity_uncertain_candidate
    assert found > 0


# ---------------------------------------------------------------- Cross-view
# 비교 범위: 이전 구현 쪽은 후보 근거(`_pairs` → 쌍마다 이전 `_pair`), 배정(`_assign`), 클러스터 검증(`_validate`),
# 삼각측량을 이전 사본으로 바꾼다. view 준비(`_prepare`)와 그 밖의 흐름(`associate` 본체, `_finish`, 연결 이력)은
# 양쪽 모두 새 코드다. `_prepare` 는 `_Prepared` 에 왜곡 제거 결과 필드가 늘어 이전 사본을 끼울 수 없으므로, 그 차이
# (왜곡 제거 묶음과 관측 마스크)는 `test_undistort_keypoints_matches_point_by_point` 가 점별 이전 판정과 비교한다.


def _legacy_pairs(self, prepared):
    """이전 구현: view 쌍마다 하나씩 근거를 계산한다."""
    return {
        (ka, kb): legacy.cross_view_pair(self, prepared[ka], prepared[kb])
        for ka, kb in itertools.combinations(sorted(prepared), 2)
        if ka[0] != kb[0]
    }


def _legacy_triangulate_many(people, rig, joint_layout="COCO17", config=None, *, undistorted=None):
    return [_legacy_triangulate(views, rig, joint_layout, config) for views in people]


def _patch_legacy_cross_view(m):
    m.setattr(cross_view_module, "triangulate_skeleton", _legacy_triangulate)
    m.setattr(cross_view_module, "triangulate_skeletons", _legacy_triangulate_many)
    m.setattr(CrossViewAssociator, "_pairs", _legacy_pairs)
    m.setattr(CrossViewAssociator, "_assign", legacy.cross_view_assign)
    m.setattr(CrossViewAssociator, "_validate", legacy.cross_view_validate)


def assert_same_frame(new, old, where):
    assert len(new.clusters) == len(old.clusters), where
    for a, b in zip(new.clusters, old.clusters, strict=True):
        assert (a.association_id, a.keys, a.uncertain_reasons, a.competitors) == (
            b.association_id,
            b.keys,
            b.uncertain_reasons,
            b.competitors,
        ), where
        assert (a.skeleton is None) == (b.skeleton is None)
        if a.skeleton is not None:
            # 클러스터 삼각측량에 넘기는 view 순서를 키 순서로 바꿨다(이전: 병합 순서). 관절의 카메라 순서만 다르다
            assert_same_skeleton(a.skeleton, b.skeleton, f"{where} {a.association_id}", camera_order=False)
        assert set(a.undistorted) == set(a.camera_ids)
    assert len(new.pairs) == len(old.pairs), where
    for p, q in zip(new.pairs, old.pairs, strict=True):
        assert (p.a, p.b, p.common_joints, p.consistent_joints, p.reject, p.marginal) == (
            q.a,
            q.b,
            q.common_joints,
            q.consistent_joints,
            q.reject,
            q.marginal,
        ), where
        _assert_close(p.epipolar_px, q.epipolar_px, ATOL_PX, "epipolar_px")
        _assert_close(p.residual_px, q.residual_px, ATOL_PX, "residual_px")
        _assert_close(p.floor_gap_m, q.floor_gap_m, ATOL_FLOOR_M, "floor_gap_m")
        if math.isinf(q.cost):
            assert math.isinf(p.cost)
        else:
            _assert_close(p.cost, q.cost, ATOL_COST, "cost")


@pytest.mark.parametrize("lens", ["wide", "fold", "fisheye"])
@pytest.mark.parametrize("harsh", [False, True], ids=["clean", "harsh"])
def test_cross_view_matches_legacy(lens, harsh, monkeypatch):
    rig, ticks = _ticks(lens, seed=300, n_people=6, n_ticks=4, harsh=harsh)
    new_assoc, old_assoc = CrossViewAssociator(rig), CrossViewAssociator(rig)
    for inp in ticks:
        new = new_assoc.associate(inp.t, inp.local_views)
        with monkeypatch.context() as m:
            _patch_legacy_cross_view(m)
            old = old_assoc.associate(inp.t, inp.local_views)
        assert_same_frame(new, old, f"{lens} t {inp.t}")


def _crowd_ticks(seed: int, n_people: int = 8, n_ticks: int = 6, noise_px: float = 7.0):
    """2.4 × 2.4 m 안에 0.5 m 간격으로 몰아 세운 장면. 애매한 배정·잔차 경계·2대 경쟁 후보가 나온다."""
    rng = np.random.default_rng(seed)
    rig = bench.make_rig()
    people = bench.make_people(n_people, rng)
    spots: list[np.ndarray] = []
    while len(spots) < n_people:
        xy = np.array([rng.uniform(3.3, 5.7), rng.uniform(2.5, 4.9)])
        if all(np.linalg.norm(xy - q) >= 0.5 for q in spots):
            spots.append(xy)
    for person, xy in zip(people, spots):
        person.xy, person.velocity = xy, person.velocity * 0.3
    out = []
    for k in range(n_ticks):
        kwargs = dict(HARSH, camera_drop_rate=0.2, noise_px=noise_px)
        out.append(bench.tick_input(rig, people, bench.T0 + k * bench.TICK_MS, rng, **kwargs))
        bench.step_people(people, rng)
    return rig, out


def test_cross_view_crowd_matches_legacy(monkeypatch):
    """사람이 몰린 장면에서 배정의 애매함·잔차 경계 경로까지 이전 구현과 같다."""
    reasons = set()
    for seed in (0, 1):
        rig, ticks = _crowd_ticks(seed)
        new_assoc, old_assoc = CrossViewAssociator(rig), CrossViewAssociator(rig)
        for inp in ticks:
            new = new_assoc.associate(inp.t, inp.local_views)
            with monkeypatch.context() as m:
                _patch_legacy_cross_view(m)
                old = old_assoc.associate(inp.t, inp.local_views)
            assert_same_frame(new, old, f"crowd seed {seed} t {inp.t}")
            reasons.update(str(r) for c in new.clusters for r in c.uncertain_reasons)
    assert {"AMBIGUOUS_ASSIGNMENT", "MARGINAL_RESIDUAL", "TWO_VIEW_COMPETITOR"} <= reasons


def test_view_split_matches_legacy():
    """잘못된 view 를 섞은 클러스터(병합 순서도 섞임)를 클러스터 검증에 넣어 VIEW_SPLIT 으로 떼어 내는 경로가 같다.

    새 `_validate` 는 처음 클러스터 전체를 한 번에 삼각측량해 두고 view 를 뗀 클러스터만 다시 푼다. 이전 구현은
    클러스터마다 병합 순서의 view 로 푼다(관절 camera_ids 순서만 다르다).
    """
    splits = 0
    for seed in range(6):
        rng = np.random.default_rng(seed)
        rig = bench.make_rig()
        people = bench.make_people(4, rng)
        inp = bench.tick_input(rig, people, bench.T0, rng)
        assoc = CrossViewAssociator(rig)
        prepared = {lv.key: assoc._prepare(lv) for lv in inp.local_views}

        def key(cam, person):
            return (cam, f"{cam}-p{person}")

        groups = [
            [key("cam3", 0), key("cam1", 0), key("cam2", 0), key("cam4", 1)],  # 넷째 view 가 다른 사람
            [key("cam2", 1), key("cam4", 2), key("cam1", 1)],  # 3대 중 하나가 다른 사람
            [key("cam4", 3), key("cam2", 3), key("cam1", 2), key("cam3", 0)],  # 다른 사람 view 둘
            [key("cam2", 2), key("cam1", 2)],
        ]
        groups = [[k for k in g if k in prepared] for g in groups]
        new_works = [_Work(list(g)) for g in groups if g]
        old_works = [_Work(list(g)) for g in groups if g]
        new_flags, old_flags = [], []
        new = assoc._validate(new_works, prepared, new_flags)
        old = legacy.cross_view_validate(assoc, old_works, prepared, old_flags)
        assert [w.keys for w in new_works] == [w.keys for w in old_works], seed
        assert [dataclasses.astuple(f) for f in new_flags] == [dataclasses.astuple(f) for f in old_flags], seed
        assert sorted(new) == sorted(old), seed
        for i in new:
            assert_same_skeleton(new[i], old[i], f"seed {seed} 클러스터 {i}", camera_order=False)
        splits += sum(str(f.reason) == "VIEW_SPLIT" for f in new_flags)
    assert splits > 0


def test_cluster_undistorted_feeds_joint_gate():
    """Cross-view 클러스터의 왜곡 제거·삼각측량 결과를 관절 게이트에 넘기면 다시 풀지 않고 같은 결과를 낸다."""
    rig, ticks = _ticks("wide", seed=9, n_people=4, n_ticks=1, harsh=True)
    frame = CrossViewAssociator(rig).associate(ticks[0].t, ticks[0].local_views)
    camera_type = type(rig.cameras["cam1"])
    calls = []

    def counting(name):
        original = getattr(camera_type, name)

        def wrapper(self, *args):
            calls.append((name, self.camera_id))
            return original(self, *args)

        return wrapper

    checked = 0
    for c in frame.clusters:
        if c.skeleton is None:
            continue
        others, others_und = {}, {}
        for o in frame.clusters:
            if o is not c:
                for lv in o.views:
                    others.setdefault(lv.camera_id, []).append(lv.view.keypoints)
                    others_und.setdefault(lv.camera_id, []).append(o.undistorted[lv.camera_id])
        views = [lv.view for lv in c.views]
        expected = JointGate().update(views, rig, others)
        with pytest.MonkeyPatch.context() as m:
            for name in ("undistort_keypoints", "undistort_many", "normalize_with_mask"):
                m.setattr(camera_type, name, counting(name))
            got = JointGate().update(views, rig, others, undistorted=c.undistorted, others_undistorted=others_und)
            tri_calls = _counting_triangulate(m)
            reused = JointGate().update(
                views, rig, others, undistorted=c.undistorted, others_undistorted=others_und, skeleton=c.skeleton
            )
        assert calls == [] and tri_calls == []
        assert_same_gated(got, expected, c.association_id)
        assert_same_gated(reused, expected, c.association_id)
        checked += 1
    assert checked > 0


# ---------------------------------------------------------------- 다시 쓰기 조건


def test_undistorted_from_other_calibration_is_not_used():
    """camera_id 가 같아도 다른 보정(렌즈 계수)으로 푼 결과는 쓰지 않는다."""
    rig_wide, ticks = _ticks("wide", seed=4, n_people=2, n_ticks=1, harsh=False)
    rig_none = _rig("none")
    views = ticks[0].per_person[0]
    foreign = {cid: rig_wide.cameras[cid].undistort_keypoints(v.keypoints) for cid, v in views.items()}
    assert_same_skeleton(
        triangulate_skeleton(views, rig_none, undistorted=foreign), legacy.triangulate_skeleton(views, rig_none)
    )


def _counting_triangulate(monkeypatch):
    calls = []
    original = joint_gate_module.triangulate_skeleton

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(joint_gate_module, "triangulate_skeleton", wrapper)
    return calls


def test_joint_gate_reuses_cluster_skeleton_only_when_inputs_match(monkeypatch):
    rig, ticks = _ticks("wide", seed=12, n_people=3, n_ticks=2, harsh=False)
    frame = CrossViewAssociator(rig).associate(ticks[0].t, ticks[0].local_views)
    cluster = next(c for c in frame.clusters if c.skeleton is not None and len(c.views) >= 3)
    views = [lv.view for lv in cluster.views]
    expected = JointGate().update(views, rig, {})
    calls = _counting_triangulate(monkeypatch)

    # 같은 입력: 다시 삼각측량하지 않고 같은 결과
    got = JointGate().update(views, rig, {}, skeleton=cluster.skeleton)
    assert calls == []
    assert_same_gated(got, expected, "reuse")

    # 설정이 다르면 다시 계산한다
    strict = JointGateConfig(triangulation=TriangulationConfig(max_reprojection_error_px=6.0))
    JointGate(strict).update(views, rig, {}, skeleton=cluster.skeleton)
    assert len(calls) == 1
    # 동기화 무효 카메라를 빼면 view 가 달라 다시 계산한다
    dropped = JointGate().update(views, rig, {}, sync_invalid_cameras=[views[0].camera_id], skeleton=cluster.skeleton)
    assert len(calls) == 2
    assert views[0].camera_id not in dropped.camera_ids
    # view 순서가 다르면(관절의 camera_ids 순서가 달라진다) 다시 계산한다
    JointGate().update(list(reversed(views)), rig, {}, skeleton=cluster.skeleton)
    assert len(calls) == 3
    # 보정 상태가 다르면 다시 계산한다
    drifted = _rig("drifted")
    JointGate().update(views, drifted, {}, skeleton=cluster.skeleton)
    assert len(calls) == 4
    # keypoint 가 다르면(다음 시각) 다시 계산한다
    later = [lv.view for lv in ticks[1].local_views if lv.key in set(cluster.keys)]
    assert len(later) >= 2
    JointGate().update(later, rig, {}, skeleton=cluster.skeleton)
    assert len(calls) == 5
    # 시각·카메라는 같고 keypoint 하나만 다르면(낡은 결과) 다시 계산하고, 새로 계산한 결과와 같다
    view = views[0]
    j = next(j for j, kp in enumerate(view.keypoints) if kp is not None)
    u, v, c = view.keypoints[j]
    moved = list(view.keypoints)
    moved[j] = (u + 3.0, v, c)
    edited = [dataclasses.replace(view, keypoints=tuple(moved)), *views[1:]]
    assert edited[0].t == view.t and edited[0].camera_id == view.camera_id
    assert not cluster.skeleton.reusable_for(edited, rig, "COCO17", TriangulationConfig())
    got = JointGate().update(edited, rig, {}, skeleton=cluster.skeleton)
    assert len(calls) == 6
    assert_same_gated(got, JointGate().update(edited, rig, {}), "같은 시각 keypoint 변경")
