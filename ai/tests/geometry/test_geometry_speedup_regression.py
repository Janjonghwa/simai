"""Tier 0 기하 단계 지연 줄이기(S15P31A101-237)가 이전 구현과 결과가 같은지 본다 (명세 5.2, docs/design/tier0-geometry-budget.md).

기준은 바꾸기 직전(dev abe9529) 모듈의 고정 사본 `legacy_batch_{triangulation,joint_gate,cross_view}.py` 다
(rig 는 바꾸지 않아 지금 모듈을 같이 쓴다). 같은 보정 문서로 rig 를 두 개 만들고, 같은 무작위 장면을 World Track
갱신 한 번의 기하(Cross-view 연결 → 클러스터마다 관절 게이트, `bench_tier0_geometry._gate_clusters` 와 같은 호출)와 사람별 경로(`triangulate_skeletons`,
`JointGate.update`, others 를 모르는 경우 포함)에 넣어 결과 전체를 비교한다.

장면: 렌즈(광각·왜곡 없음·접히는 렌즈·어안·DRIFTED), 1·5·10명, 거친 입력(이상치 keypoint, NaN, 왜곡 제거 실패 모서리
keypoint, 카메라 통째 가림으로 2대 관측·1대 관측 관절), 사람이 몰린 장면(애매한 배정·잔차 경계·2대 경쟁 후보).

허용 오차: 범주형(관절 상태·사유·쓴/뺀 카메라·클러스터 구성·association_id·쌍 탈락 사유·IDENTITY_UNCERTAIN 사유·
null 여부·joint_source)은 완전히 같아야 한다. 실수는 모두 절대 오차 1e-9 (XYZ m, 픽셀, 라디안, 표준편차 m, 속도 m/s,
쌍 비용, 바닥 가설 거리 m). 바꾼 계산은 같은 식을 묶어 하거나 순서만 바꾼 것이라 실측 차이는 부동소수 마지막 자리다.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import math
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from simai_ai.geometry import joint_gate as joint_gate_module
from simai_ai.geometry import triangulation as triangulation_module
from simai_ai.geometry.joint_gate import JointGate
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import triangulate_skeletons
from simai_ai.tracking import cross_view as cross_view_module
from simai_ai.tracking.cross_view import CrossViewAssociator

HERE = Path(__file__).resolve().parent
ATOL = 1e-9
# 비교하지 않는 필드: 카메라 모델 객체(구현마다 다른 클래스), 다시 쓰기 확인용 입력 묶음
SKIP_FIELDS = {"camera", "inputs"}


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("bench_tier0_geometry")
legacy_tri = _load("legacy_batch_triangulation")
legacy_gate = _load("legacy_batch_joint_gate")
legacy_cv = _load("legacy_batch_cross_view")

LENSES = {
    "wide": ("OPENCV_PINHOLE", bench.WIDE_COEFFS, "PROVISIONAL"),
    "none": ("NONE", (), "PROVISIONAL"),
    "fold": ("OPENCV_PINHOLE", (-0.30, 0.0, 0.0, 0.0, 0.0), "PROVISIONAL"),
    "fisheye": ("OPENCV_FISHEYE", (-0.02, 0.003, -0.001, 0.0002), "PROVISIONAL"),
    "drifted": ("OPENCV_PINHOLE", bench.WIDE_COEFFS, "DRIFTED"),
}
HARSH = {"outlier_rate": 0.15, "nan_rate": 0.05, "corner_rate": 0.03, "camera_drop_rate": 0.25}


def _rigs(lens: str):
    """(지금 구현 rig, 이전 구현 rig). 같은 보정 문서로 만든다."""
    model, coeffs, state = LENSES[lens]
    doc = bench.calibration_doc(coeffs, state, model)
    return bench.make_rig(coeffs, state, model), CameraRig.from_provider(bench._DictProvider(doc))


def _ticks(rig, seed: int, n_people: int, n_ticks: int, harsh: bool, crowd: bool = False):
    rng = np.random.default_rng(seed)
    people = bench.make_people(n_people, rng)
    if crowd:  # 2.4 × 2.4 m 안에 0.5 m 간격으로 몰아 세운다
        spots: list[np.ndarray] = []
        while len(spots) < n_people:
            xy = np.array([rng.uniform(3.3, 5.7), rng.uniform(2.5, 4.9)])
            if all(np.linalg.norm(xy - q) >= 0.5 for q in spots):
                spots.append(xy)
        for person, xy in zip(people, spots):
            person.xy, person.velocity = xy, person.velocity * 0.3
    kwargs: dict[str, float] = dict(HARSH) if harsh else {}
    if crowd:
        kwargs["noise_px"] = 7.0
    out = []
    for k in range(n_ticks):
        out.append(bench.tick_input(rig, people, bench.T0 + k * bench.TICK_MS, rng, **kwargs))
        bench.step_people(people, rng)
    return out


# ---------------------------------------------------------------- 비교


def assert_same(new: Any, old: Any, path: str = "") -> None:
    """두 구현의 결과를 필드마다 비교한다. 범주형은 같아야 하고 실수는 ATOL 안이어야 한다."""
    if dataclasses.is_dataclass(new) and not isinstance(new, type):
        assert dataclasses.is_dataclass(old) and type(new).__name__ == type(old).__name__, path
        for f in dataclasses.fields(new):
            if f.name not in SKIP_FIELDS:
                assert_same(getattr(new, f.name), getattr(old, f.name), f"{path}.{f.name}")
        return
    if isinstance(new, (str, Enum)) or isinstance(old, (str, Enum)):
        assert str(new) == str(old) and type(new) is not bool, f"{path}: {new!r} != {old!r}"
        return
    if new is None or old is None or isinstance(new, (bool, np.bool_)) or isinstance(old, (bool, np.bool_)):
        assert new is old or (new is not None and old is not None and bool(new) == bool(old)), f"{path}: {new} != {old}"
        return
    if isinstance(new, (int, np.integer)) and isinstance(old, (int, np.integer)):
        assert int(new) == int(old), f"{path}: {new} != {old}"
        return
    if isinstance(new, (float, np.floating, int, np.integer)):
        a, b = float(new), float(old)
        if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
            assert (math.isnan(a) and math.isnan(b)) or a == b, f"{path}: {a} != {b}"
        else:
            assert abs(a - b) <= ATOL, f"{path}: {a} != {b} (차이 {abs(a - b):.3g})"
        return
    if isinstance(new, np.ndarray):
        assert new.shape == np.shape(old), path
        np.testing.assert_allclose(new, old, rtol=0, atol=ATOL, equal_nan=True, err_msg=path)
        return
    if isinstance(new, dict) or hasattr(new, "keys") and hasattr(new, "items"):
        assert list(new.keys()) == list(old.keys()), path
        for k in new:
            assert_same(new[k], old[k], f"{path}[{k}]")
        return
    if isinstance(new, (tuple, list, frozenset, set)):
        if isinstance(new, (frozenset, set)):
            new, old = sorted(new), sorted(old)
        assert len(new) == len(old), f"{path}: 길이 {len(new)} != {len(old)}"
        for k, (a, b) in enumerate(zip(new, old)):
            assert_same(a, b, f"{path}[{k}]")
        return
    assert new == old, f"{path}: {new!r} != {old!r}"


def _gate_clusters(frame, rig, gates, joint_gate_cls):
    """`bench_tier0_geometry._gate_clusters` 와 같은 호출(클러스터 결과 다시 쓰기). 결과를 클러스터 순서로 돌려준다."""
    out = []
    for c in frame.clusters:
        if c.skeleton is None:
            continue
        others: dict[str, list[Any]] = {}
        others_und: dict[str, list[Any]] = {}
        for o in frame.clusters:
            if o is not c:
                for lv in o.views:
                    others.setdefault(lv.camera_id, []).append(lv.view.keypoints)
                    others_und.setdefault(lv.camera_id, []).append(o.undistorted[lv.camera_id])
        gate = gates.setdefault(c.association_id, joint_gate_cls())
        views = [lv.view for lv in c.views]
        gated = gate.update(views, rig, others, undistorted=c.undistorted, others_undistorted=others_und, skeleton=c.skeleton)
        out.append(gated)
    return out


def compare_pipeline(lens: str, seed: int, n_people: int, n_ticks: int, harsh: bool, crowd: bool = False) -> set[str]:
    """World Track 갱신의 기하(연결 → 클러스터별 게이트)를 tick 마다 두 구현으로 돌려 비교한다. 본 사유를 돌려준다."""
    rig, old_rig = _rigs(lens)
    new_assoc, old_assoc = CrossViewAssociator(rig), legacy_cv.CrossViewAssociator(old_rig)
    new_gates: dict[str, Any] = {}
    old_gates: dict[str, Any] = {}
    seen: set[str] = set()
    for inp in _ticks(rig, seed, n_people, n_ticks, harsh, crowd):
        where = f"{lens} seed {seed} 사람 {n_people} t {inp.t}"
        new = new_assoc.associate(inp.t, inp.local_views)
        old = old_assoc.associate(inp.t, inp.local_views)
        assert_same(new, old, where)
        assert_same(new_assoc._history, old_assoc._history, where + " 연결 이력")
        new_gated = _gate_clusters(new, rig, new_gates, JointGate)
        old_gated = _gate_clusters(old, old_rig, old_gates, legacy_gate.JointGate)
        assert_same(new_gated, old_gated, where + " 게이트")
        for g in new_gated:
            assert_same(g.head_z(0.12), old_gated[new_gated.index(g)].head_z(0.12), where + " head_z")
        seen.update(str(r) for c in new.clusters for r in c.uncertain_reasons)
        seen.update(str(p.reject) for p in new.pairs if p.reject is not None)
        seen.update(str(r) for g in new_gated for j in g.joints for r in j.reasons)
        seen.update(f"validity:{j.validity}" for g in new_gated for j in g.joints)
    return seen


def compare_per_person(lens: str, seed: int, n_people: int, n_ticks: int, harsh: bool) -> set[str]:
    """정답 연결로 사람마다 삼각측량·관절 게이트(자체 왜곡 제거·삼각측량, others 를 모르는 tick 포함)를 비교한다."""
    rig, old_rig = _rigs(lens)
    ticks = _ticks(rig, seed, n_people, n_ticks, harsh)
    new_gates = [JointGate() for _ in range(n_people)]
    old_gates = [legacy_gate.JointGate() for _ in range(n_people)]
    seen: set[str] = set()
    for k, inp in enumerate(ticks):
        where = f"{lens} seed {seed} 사람 {n_people} tick {k}"
        people = [views for views in inp.per_person if views]
        assert_same(triangulate_skeletons(people, rig), legacy_tri.triangulate_skeletons(people, old_rig), where)
        for i, views in enumerate(inp.per_person):
            if not views:
                continue
            others = None if k % 3 == 2 else bench.others_for(inp.per_person, i)
            new = new_gates[i].update(views, rig, others)
            old = old_gates[i].update(views, old_rig, others)
            assert_same(new, old, f"{where} 사람 {i}")
            seen.update(str(r) for j in new.joints for r in j.reasons)
            seen.update(f"validity:{j.validity}" for j in new.joints)
    return seen


# ---------------------------------------------------------------- 시험


@pytest.mark.parametrize("n_people", [1, 5, 10])
@pytest.mark.parametrize("lens", ["wide", "fold"])
@pytest.mark.parametrize("harsh", [False, True], ids=["clean", "harsh"])
def test_pipeline_matches_previous(lens, n_people, harsh):
    seen = set()
    for seed in range(2):
        seen |= compare_pipeline(lens, 500 + seed, n_people, 5, harsh)
    if harsh and n_people >= 5:
        # 거친 장면이 null·관측 부족·경쟁 후보 확인 경로를 실제로 지난다
        assert {"TOO_FEW_VIEWS", "validity:UNAVAILABLE", "validity:VALID"} <= seen


@pytest.mark.parametrize("lens", ["none", "fisheye", "drifted"])
def test_pipeline_matches_previous_other_lenses(lens):
    compare_pipeline(lens, 510, 6, 4, harsh=True)


def test_pipeline_crowd_matches_previous():
    """사람이 몰린 장면: 애매한 배정·잔차 경계·2대 경쟁 후보 경로까지 같다."""
    seen = set()
    for seed in range(3):
        seen |= compare_pipeline("wide", 520 + seed, 8, 6, harsh=True, crowd=True)
    assert {"AMBIGUOUS_ASSIGNMENT", "MARGINAL_RESIDUAL", "TWO_VIEW_COMPETITOR"} <= seen


@pytest.mark.parametrize("lens", ["wide", "fold", "fisheye"])
def test_per_person_matches_previous(lens):
    seen = compare_per_person(lens, 530, 6, 6, harsh=True)
    assert {"COMPETITOR_CHECK_UNAVAILABLE", "validity:UNAVAILABLE"} <= seen


# ---------------------------------------------------------------- 반례 (경계값·같은 비용·모르는 값)


COSTS = (0.5, 1.0, 1.0, 1.5, 2.5, 3.0)
# 1 ULP 차이로 근접한 비용: 동률 바로 옆, new_cluster_cost 3.0 바로 아래, 애매함 0.5 차이(1.0 ↔ 1.5) 바로 안·밖
ULP_COSTS = (
    1.0,
    float(np.nextafter(1.0, math.inf)),
    float(np.nextafter(1.0, -math.inf)),
    1.5,
    float(np.nextafter(1.5, math.inf)),
    float(np.nextafter(1.5, -math.inf)),
    float(np.nextafter(3.0, -math.inf)),
    3.0,
)


def _synthetic_pairs(rng, keys, module, costs=COSTS):
    """무작위 쌍 근거. 비용은 몇 개 값에서만 골라 같은 비용(동률)·문턱(new_cluster_cost 3.0, 애매함 0.5) 경계를 만든다."""
    pairs = {}
    for a_idx, ka in enumerate(keys):
        for kb in keys[a_idx + 1 :]:
            if ka[0] == kb[0]:
                continue
            roll = rng.random()
            if roll < 0.2:
                ev = module.PairEvidence(ka, kb, 2, reject=module.RejectReason.FEW_COMMON_JOINTS)
            elif roll < 0.45:
                ev = module.PairEvidence(ka, kb, 10, epipolar_px=30.0, reject=module.RejectReason.EPIPOLAR)
            else:
                cost = float(rng.choice(costs))
                ev = module.PairEvidence(ka, kb, 10, epipolar_px=1.0, residual_px=1.0, marginal=rng.random() < 0.1, cost=cost)
            pairs[(ka, kb)] = ev
    return pairs


@pytest.mark.parametrize("costs", [COSTS, ULP_COSTS], ids=["ties", "ulp"])
@pytest.mark.parametrize("exclusive", [True, False], ids=["exclusive", "negative-control-mode"])
def test_assign_ties_blocks_and_vetoes_match_previous(exclusive, costs):
    """같은 비용 동률, 막은 병합 뒤 다시 합치기, 기하가 반대하는 쌍(veto), 근거 없는 쌍, 연결 이력 보너스가 섞인 배정이
    이전 구현과 같은 클러스터·사유를 낸다. 같은 카메라 금지를 끈 음성 대조 모드도 같다."""
    rig, old_rig = _rigs("wide")
    config = cross_view_module.CrossViewConfig(same_camera_exclusive=exclusive)
    merged = 0
    for seed in range(60):
        rng = np.random.default_rng(seed)
        keys = sorted((f"cam{c}", f"cam{c}-p{p}") for c in range(1, 5) for p in range(int(rng.integers(1, 5))))
        new_assoc = CrossViewAssociator(rig, config)
        old_assoc = legacy_cv.CrossViewAssociator(old_rig, legacy_cv.CrossViewConfig(same_camera_exclusive=exclusive))
        for _ in range(int(rng.integers(0, 6))):  # 연결 이력
            i, j = sorted(rng.choice(len(keys), 2, replace=False))
            age = (int(rng.integers(1, 12)), bench.T0)
            new_assoc._history[(keys[i], keys[j])] = old_assoc._history[(keys[i], keys[j])] = age
        new_rng, old_rng = np.random.default_rng(seed), np.random.default_rng(seed)
        new_pairs = _synthetic_pairs(new_rng, keys, cross_view_module, costs)
        old_pairs = _synthetic_pairs(old_rng, keys, legacy_cv, costs)
        new_flags, old_flags = [], []
        new = new_assoc._assign(dict.fromkeys(keys), new_pairs, new_flags)
        old = old_assoc._assign(dict.fromkeys(keys), old_pairs, old_flags)
        assert [w.keys for w in new] == [w.keys for w in old], seed
        assert_same(new_flags, old_flags, f"seed {seed} flags")
        merged += sum(len(w.keys) > 1 for w in new)
    assert merged > 0


def test_pair_batch_matches_single_pair_path():
    """카메라 쌍 묶음 근거(`_pairs`)가 쌍 하나씩(`_pair`, np.median) 계산한 것과 같다. 중앙값의 짝수·홀수 개, 일관된 관절
    수가 문턱 근처인 쌍을 실제로 지난다."""
    parity = set()
    for seed in range(3):
        rig, _ = _rigs("fold")
        for inp in _ticks(rig, 540 + seed, 8, 2, harsh=True, crowd=seed == 2):
            assoc = CrossViewAssociator(rig)
            prepared = {lv.key: assoc._prepare(lv) for lv in inp.local_views}
            batch = assoc._pairs(prepared)
            assert list(batch) == sorted(batch)
            for (ka, kb), ev in batch.items():
                assert_same(ev, assoc._pair(prepared[ka], prepared[kb]), f"{ka} {kb}")
                if ev.residual_px is not None:
                    parity.add(ev.consistent_joints % 2)
    assert parity == {0, 1}


def test_solvable_matches_refined_triangulation_finiteness():
    """경쟁 후보 검사의 해 유무(정밀화 없음)가 정밀화한 삼각측량 결과의 유한함과 같다. 카메라 뒤 교점, 같은 시선(공선),
    NaN(모르는 값) 행을 섞는다."""
    rig, _ = _rigs("wide")
    rng = np.random.default_rng(550)
    cams = list(rig.cameras.values())
    kinds = set()
    for _ in range(200):
        a, b = (cams[k] for k in rng.choice(4, 2, replace=False))
        rows = rng.normal(0.0, 0.6, (12, 2, 2))
        rows[1, 0, 0] = math.nan
        center_b = a.to_camera(b.center[None])[0]
        rows[2, 0] = center_b[:2] / center_b[2]  # a 에서 b 중심을 보는 시선(기선 위, 해가 유일하지 않을 수 있다)
        got = triangulation_module.solvable_normalized_many([a, b], rows)
        want = np.isfinite(triangulation_module.triangulate_normalized_many([a, b], rows)).all(axis=1)
        assert got.tolist() == want.tolist()
        kinds.update(got.tolist())
    assert kinds == {True, False}


def test_refine_active_rows_is_bit_identical():
    """가우스-뉴턴을 멈추지 않은 점만 계산해도 이전 구현(멈춘 점도 같이 계산)과 비트 단위로 같다. 시작점이 카메라
    뒤인 점(다듬지 않음)과 곧 멈추는 점, 오래 도는 점을 섞는다."""
    rig, _ = _rigs("wide")
    stack_new = triangulation_module._CameraStack.of(list(rig.cameras.values()))
    rng = np.random.default_rng(560)
    for _ in range(20):
        M = 60
        X_true = np.column_stack([rng.uniform(1, 8, M), rng.uniform(1, 6.5, M), rng.uniform(0, 2, M)])
        h = stack_new.project(X_true)
        targets = h[..., :2] / h[..., 2:3] + rng.normal(0, rng.choice([0.0, 1.0, 30.0]), (M, 4, 2))
        mask = rng.random((M, 4)) < 0.8
        mask[:, :2] = True
        w = rng.uniform(0.3, 1.0, (M, 4))
        start = X_true + rng.normal(0, 0.05, (M, 3))
        start[:3] = [[-5.0, -5.0, 2.7]] * 3  # cam1 뒤
        new = triangulation_module._refine_batch(stack_new, targets, w, mask, start.copy(), 10)
        old = legacy_tri._refine_batch(stack_new, targets, w, mask, start.copy(), 10)
        assert np.array_equal(new, old, equal_nan=True)
        assert np.array_equal(new[:3], start[:3])


# ---------------------------------------------------------------- 음성 대조: 일부러 이전 구현과 다르게 바꾸면 위 비교가 실패한다


def test_negative_control_competitor_check_dropped(monkeypatch):
    monkeypatch.setattr(joint_gate_module, "solvable_normalized_many", lambda cams, rows: np.zeros(len(rows), dtype=bool))
    with pytest.raises(AssertionError):
        compare_pipeline("wide", 520, 8, 6, harsh=True, crowd=True)


def test_negative_control_floor_gap_changed(monkeypatch):
    original = cross_view_module._floor_gaps
    monkeypatch.setattr(cross_view_module, "_floor_gaps", lambda *a: original(*a) * (1 + 1e-6))
    with pytest.raises(AssertionError):
        compare_pipeline("wide", 500, 5, 2, harsh=False)


def test_negative_control_refine_stops_early(monkeypatch):
    original = triangulation_module._refine_batch
    monkeypatch.setattr(triangulation_module, "_refine_batch", lambda s, t, w, m, X, it: original(s, t, w, m, X, min(it, 2)))
    with pytest.raises(AssertionError):
        compare_pipeline("wide", 500, 5, 2, harsh=False)


def test_negative_control_tie_break_changed(monkeypatch):
    """같은 비용 쌍을 뒤에서부터 고르면 배정 반례 시험이 실패한다."""
    real_min = min

    def last_of_ties(*args, **kwargs):
        if len(args) == 1 and not kwargs:
            items = list(args[0])
            if items and isinstance(items[0], tuple) and len(items[0]) == 3:
                return max(items)
            return real_min(items)
        return real_min(*args, **kwargs)

    monkeypatch.setattr(cross_view_module, "min", last_of_ties, raising=False)
    with pytest.raises(AssertionError):
        test_assign_ties_blocks_and_vetoes_match_previous(True, COSTS)


# ---------------------------------------------------------------- 리뷰 보강: 퇴화·조건수 경계, 실제 계산된 근접 비용


def _baseline_rows(a, b):
    """두 카메라 기선 근처 점의 정규화 좌표 쌍. 기선에서 ε 만큼 떨어진 점(ε = 1 … 1e-13, 0: 두 시선이 정확히 기선 위),
    기선 연장선 위(어느 카메라 뒤), 카메라 앞 교점, 무한·NaN 입력을 담는다."""
    up = np.array([0.0, 0.0, 1.0])
    mid = (a.center + b.center) / 2.0
    beyond = b.center + 0.5 * (b.center - a.center)
    points = [mid + eps * up for eps in [10.0**-k for k in range(14)] + [0.0]]
    points += [beyond + eps * up for eps in (1e-3, 1e-9, 0.0)]
    points += [np.array([4.5, 3.75, 0.9]), np.array([4.5, 3.75, 50.0]), np.array([1e6, 1e6, 0.0])]
    rows = []
    for X in points:
        rows.append([cam.to_camera(X[None])[0, :2] / cam.to_camera(X[None])[0, 2] for cam in (a, b)])
    rows = np.array(rows)
    special = rows[:4].copy()
    special[0, 0, 0] = math.inf
    special[1, 1, 1] = -math.inf
    special[2, 0, 1] = math.nan
    special[3, 1] = special[3, 0]  # 같은 정규화 좌표
    return np.concatenate([rows, special])


def test_degenerate_rays_match_previous():
    """거의 평행한·정확히 기선 위인 시선(DLT 해 유일성 문턱 양쪽), 카메라 뒤 교점, 무한·NaN 입력에서 해 유무·2-view
    연결 삼각측량·정밀화한 삼각측량이 이전 구현과 같다(정해진 입력)."""
    rig, _ = _rigs("wide")
    cams = rig.cameras
    seen = set()
    for ida, idb in (("cam1", "cam2"), ("cam1", "cam3"), ("cam2", "cam4")):
        a, b = cams[ida], cams[idb]
        rows = _baseline_rows(a, b)
        with np.errstate(all="ignore"):
            old_tri = legacy_tri.triangulate_normalized_many([a, b], rows)
            new_tri = triangulation_module.triangulate_normalized_many([a, b], rows)
            got = triangulation_module.solvable_normalized_many([a, b], rows)
        np.testing.assert_array_equal(new_tri, old_tri)
        assert got.tolist() == np.isfinite(old_tri).all(axis=1).tolist(), (ida, idb)
        seen.update(got[:15].tolist())  # 기선 근처 ε 행: 문턱 양쪽
        assert not got[14] and not got[15:18].any()  # 정확히 기선 위, 기선 연장선(카메라 뒤)
        # Cross-view 2-view 연결 삼각측량(이 행들을 공통 관절처럼 넣는다). 공통 관절은 관측 마스크를 통과한 유한한 값뿐이다
        rows = rows[np.isfinite(rows).all(axis=(1, 2))]
        w = np.full(len(rows), 0.8)
        sides = [(rows[:, s], w, cam.ideal_pixels(rows[:, s])) for s, cam in enumerate((a, b))]

        def prepared(cam, xn, wt, ideal):
            und = type("U", (), {"confidence": wt, "ideal_pixels": ideal})()
            return type("P", (), {"camera": cam, "normalized": xn, "undistorted": und})()

        job = (prepared(a, *sides[0]), prepared(b, *sides[1]), np.ones(len(rows), dtype=bool))
        with np.errstate(all="ignore"):
            old_xyz, old_err = legacy_cv._two_view_triangulation_many([job])[0]
            new_xyz, new_err = cross_view_module._two_view_arrays(a, b, sides[0], sides[1])
        np.testing.assert_array_equal(new_xyz, old_xyz)
        np.testing.assert_array_equal(new_err, old_err)
    assert seen == {True, False}


def test_large_condition_number_sigmas_match_previous():
    """기선 근처·아주 먼 관절(공분산 조건수가 1e12 문턱 근처·너머)에서 삼각측량 표준편차가 이전 구현과 같다."""
    rig, _ = _rigs("wide")
    a, b = rig.cameras["cam1"], rig.cameras["cam2"]
    mid = (a.center + b.center) / 2.0
    points = [mid + np.array([0.0, 0.0, eps]) for eps in (1.0, 1e-2, 1e-4, 1e-6, 1e-8)]
    points += [np.array([4.5, 3.75, 0.9]), np.array([4.5, 1e4, 0.9]), np.array([4.5, 1e7, 0.9])]
    joints = [
        triangulation_module.JointEstimate(tuple(map(float, X)), triangulation_module.JointValidity.VALID, ("cam1", "cam2"))
        for X in points
    ]
    joints += [triangulation_module.JointEstimate(None, triangulation_module.JointValidity.UNAVAILABLE)] * (17 - len(joints))
    skeleton = triangulation_module.SkeletonEstimate(bench.T0, "COCO17", tuple(joints), rig.calibration_version)
    with np.errstate(all="ignore"):
        new = joint_gate_module._position_sigmas(skeleton, rig, 2.0)
        old = legacy_gate._position_sigmas(skeleton, rig, 2.0)
    assert_same(new, old, "sigmas")
    sigma_max = [s for s in new[1] if s is not None]
    assert any(math.isinf(s) for s in sigma_max) and any(math.isfinite(s) for s in sigma_max)


def test_floor_gap_batch_is_bit_identical_to_scalar():
    """임시 바닥 가설 거리(쌍 비용에 들어간다)의 묶음 계산이 쌍 하나씩 계산(`_floor_gap`)과 비트 단위로 같다.
    1e-12 ~ 1e12 규모를 섞는다. 이 환경(numpy·CPU)에서의 확인이며 다른 BLAS 의 증명은 아니다."""
    rng = np.random.default_rng(570)
    N = 4000
    scale = 10.0 ** rng.uniform(-12, 12, (N, 4, 1))
    rays = rng.normal(size=(N, 4, 2)) * scale
    rays[:50, 3] = rays[:50, 1]  # dB = 0 (평행한 두 가설)
    stack = lambda part: type("S", (), {"floor_a": rays[:, part], "floor_b": rays[:, part + 1], "has_floor": np.ones(N, dtype=bool)})()
    idx = np.arange(N)
    got = cross_view_module._floor_gaps(stack(0), stack(2), idx, idx, 1.0)
    want = np.array([legacy_cv._floor_gap((rays[n, 0], rays[n, 1]), (rays[n, 2], rays[n, 3]), 1.0) for n in range(N)])
    np.testing.assert_array_equal(got, want)
