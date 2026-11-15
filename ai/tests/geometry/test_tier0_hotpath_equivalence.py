"""판정 틱 기하 출력 동일 최적화(S15P31A101-726)가 바꾸기 직전 구현과 결과가 같은지 본다 (명세 5.2,
docs/design/S15P31A101-726-tier0-hotpath.md, 기준은 docs/design/tier0-geometry-budget.md).

기준은 바꾸기 직전(dev bd706a9f) 고정 사본이다: `legacy_726_triangulation.py`, `legacy_726_cross_view.py`(그 삼각측량
사본을 쓴다), `legacy_726_world.py`(World Track 의 `_others`, 관절 게이트의 경쟁 후보 검사. `legacy_world()` 안에서 지금
모듈이 이 사본을 부른다).

비교는 비트 단위다(실수도 == , NaN 은 NaN 끼리). 범주형(관절 상태·사유·쓴/뺀 카메라·클러스터 구성·association_id·쌍
탈락 사유·IDENTITY_UNCERTAIN 사유·World ID·생명주기)은 물론이고 XYZ·재투영 오차·비용도 같아야 한다. geometry-budget 의
허용 오차(XYZ 1e-6 m, 재투영 1e-5 px)보다 엄격하다. 바꾼 계산은 같은 식을 같은 카메라끼리 묶거나 같은 입력의 결과를 다시
쓰는 것뿐이라 비트 단위로 같다. 단 순차 삼각측량의 첫 반복은 1차 묶음 풀이 값을 다시 쓰므로, 판단이 그 마지막 자리 차이 안의
경계(이상치 기준 20 px 와의 차이가 약 1e-6 px 이하)에 걸리는 입력에서만 달라질 수 있다(설계 문서 "남은 위험").

장면: bench_tier0_hotpath_ab 의 clean·s17(17차 재생형: 카메라 2대 촬영 시각 200·300 ms 어긋남, 이상치 8%, 가림 10%,
로컬 트랙 조각남), test_geometry_speedup_regression 과 같은 거친 입력(이상치 15%, NaN 5%, 영상 모서리 3%, 카메라 통째 가림
25%), 몰린 장면(2.4 × 2.4 m 에 0.5 m 간격, 잡음 7 px), 렌즈(광각·접히는 렌즈·어안·DRIFTED). VIEW_SPLIT 은 자연 장면에서
거의 생기지 않아 섞인 클러스터를 직접 만들어 본다.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import math
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from simai_ai.geometry import triangulation as tri
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking import cross_view as cv
from simai_ai.tracking import world_track as wt
from simai_ai.tracking.diagnostics import TrackingDiagnostics, window_log_line
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackManager

HERE = Path(__file__).resolve().parent
SKIP_FIELDS = {"camera", "inputs", "work"}  # 카메라 모델 객체, 다시 쓰기 확인용 입력, 계측용 계산량(-726 에서 더함)


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ab = _load("bench_tier0_hotpath_ab")
geo = ab.geo
legacy_tri = _load("legacy_726_triangulation")
legacy_cv = _load("legacy_726_cross_view")
legacy_world = _load("legacy_726_world")

LENSES = {
    "wide": ("OPENCV_PINHOLE", geo.WIDE_COEFFS, "PROVISIONAL"),
    "fold": ("OPENCV_PINHOLE", (-0.30, 0.0, 0.0, 0.0, 0.0), "PROVISIONAL"),
    "fisheye": ("OPENCV_FISHEYE", (-0.02, 0.003, -0.001, 0.0002), "PROVISIONAL"),
    "drifted": ("OPENCV_PINHOLE", geo.WIDE_COEFFS, "DRIFTED"),
}
HARSH = {"outlier_rate": 0.15, "nan_rate": 0.05, "corner_rate": 0.03, "camera_drop_rate": 0.25}


def assert_identical(new: Any, old: Any, path: str = "") -> None:
    """두 구현의 결과가 비트 단위로 같은지(필드마다, 실수는 ==, NaN 은 NaN 끼리)."""
    if dataclasses.is_dataclass(new) and not isinstance(new, type):
        assert dataclasses.is_dataclass(old) and type(new).__name__ == type(old).__name__, path
        for f in dataclasses.fields(new):
            if f.name not in SKIP_FIELDS:
                assert_identical(getattr(new, f.name), getattr(old, f.name), f"{path}.{f.name}")
        return
    if isinstance(new, (str, Enum)) or isinstance(old, (str, Enum)):
        assert str(new) == str(old), f"{path}: {new!r} != {old!r}"
        return
    if new is None or old is None or isinstance(new, (bool, np.bool_)):
        assert (new is None) == (old is None) and (new is None or bool(new) == bool(old)), f"{path}: {new} != {old}"
        return
    if isinstance(new, (float, np.floating, int, np.integer)):
        a, b = float(new), float(old)
        assert a == b or (math.isnan(a) and math.isnan(b)), f"{path}: {a!r} != {b!r}"
        return
    if isinstance(new, np.ndarray):
        assert new.shape == np.shape(old) and np.array_equal(new, old, equal_nan=True), path
        return
    if isinstance(new, dict) or (hasattr(new, "keys") and hasattr(new, "items")):
        assert sorted(map(str, new.keys())) == sorted(map(str, old.keys())), path
        for k in new:
            assert_identical(new[k], old[k], f"{path}[{k}]")
        return
    if isinstance(new, (tuple, list, frozenset, set)):
        if isinstance(new, (frozenset, set)):
            new, old = sorted(new), sorted(old)
        assert len(new) == len(old), f"{path}: 길이 {len(new)} != {len(old)}"
        for k, (a, b) in enumerate(zip(new, old)):
            assert_identical(a, b, f"{path}[{k}]")
        return
    assert new == old, f"{path}: {new!r} != {old!r}"


def _rig(lens: str = "wide") -> CameraRig:
    model, coeffs, state = LENSES[lens]
    return geo.make_rig(coeffs, state, model)


def _harsh_frames(rig: CameraRig, n_people: int, ticks: int, seed: int, crowd: bool = False) -> list[tuple[int, list]]:
    rng = np.random.default_rng(seed)
    people = geo.make_people(n_people, rng)
    if crowd:  # 2.4 × 2.4 m 안에 0.5 m 간격으로 몰아 세운다(애매한 배정·잔차 경계·2대 경쟁 후보)
        spots: list[np.ndarray] = []
        while len(spots) < n_people:
            xy = np.array([rng.uniform(3.3, 5.7), rng.uniform(2.5, 4.9)])
            if all(np.linalg.norm(xy - q) >= 0.5 for q in spots):
                spots.append(xy)
        for person, xy in zip(people, spots):
            person.xy, person.velocity = xy, person.velocity * 0.3
    kwargs: dict[str, float] = {"noise_px": 7.0} if crowd else dict(HARSH)
    out = []
    for k in range(ticks):
        inp = geo.tick_input(rig, people, geo.T0 + k * geo.TICK_MS, rng, **kwargs)
        geo.step_people(people, rng)
        out.append((inp.t, inp.local_views))
    return out


def _frames(kind: str, rig: CameraRig, n_people: int, ticks: int, seed: int) -> list[tuple[int, list]]:
    if kind in ("clean", "s17"):
        return ab.scene(rig, n_people, ticks, seed, kind)
    return _harsh_frames(rig, n_people, ticks, seed, crowd=kind == "crowd")


# ---------------------------------------------------------------- 삼각측량


def _random_jobs(rig: CameraRig, rng: np.random.Generator, count: int) -> list[tuple[Any, Any]]:
    """(지금 `_Observations`, 이전 `_Observations`) 쌍. 카메라 2~4대, 이상치·카메라 뒤·같은 직선 시선·NaN 을 섞는다."""
    cams = list(rig.cameras.values())
    jobs = []
    for _ in range(count):
        n = int(rng.integers(1, 5))
        chosen = [cams[i] for i in sorted(rng.choice(len(cams), size=n, replace=False))]
        X = np.array([rng.uniform(1.0, 8.0), rng.uniform(1.0, 6.5), rng.uniform(0.0, 2.0)])
        kind = rng.random()
        normalized, targets = [], []
        for c in chosen:
            xc = c.R @ X + c.t
            if kind < 0.05:
                xc = -xc  # 카메라 뒤
            xn = xc[:2] / xc[2]
            if kind < 0.6 and rng.random() < 0.35:
                xn = xn + rng.normal(0.0, 0.05, 2)  # 이상치 (정규화 좌표 0.05 ≈ 44 px)
            else:
                xn = xn + rng.normal(0.0, 0.001, 2)
            normalized.append(xn)
            targets.append(c.ideal_pixels(xn[None])[0])
        normalized_a, targets_a = np.array(normalized), np.array(targets)
        if 0.05 <= kind < 0.08 and n >= 2:
            normalized_a[1] = normalized_a[0]  # 같은 정규화 좌표(퇴화 가능)
            targets_a[1] = targets_a[0]
        if 0.08 <= kind < 0.1:
            normalized_a[0, 0] = math.nan
        weights = rng.uniform(0.3, 1.0, n)
        jobs.append(
            (
                tri._Observations(chosen, normalized_a, targets_a, weights),
                legacy_tri._Observations(chosen, normalized_a.copy(), targets_a.copy(), weights.copy()),
            )
        )
    return jobs


@pytest.mark.parametrize("lens", ["wide", "drifted"])
def test_triangulate_joints_matches_per_joint_path(lens: str) -> None:
    """라운드 묶음 순차 경로 = 관절 하나씩 순차 경로(`_triangulate_joint`·`_worst_view`), 비트 단위."""
    rig = _rig(lens)
    rng = np.random.default_rng(726)
    cfg = tri.TriangulationConfig()
    jobs = _random_jobs(rig, rng, 600)
    trusted = rig.state not in tri.UNTRUSTED_CALIBRATION_STATES
    new = tri._triangulate_joints([j for j, _ in jobs], trusted, cfg)
    old = [legacy_tri._triangulate_joint(o, trusted, legacy_tri.TriangulationConfig()) for _, o in jobs]
    assert_identical(new, old)
    issues = {str(i) for e in new for i in e.issues}
    rejected = sum(1 for e in new if e.rejected_camera_ids)
    # 반례가 실제로 섞였는지(이상치 빼기, 최소 관측 이상치, 해 없음, 관측 부족)
    assert {"INCONSISTENT_VIEWS", "TOO_FEW_VIEWS"} <= issues and rejected > 50


@pytest.mark.parametrize(
    ("kind", "lens", "n_people"),
    [("s17", "wide", 3), ("s17", "wide", 5), ("harsh", "fold", 5), ("harsh", "fisheye", 3), ("crowd", "drifted", 5)],
)
def test_triangulate_skeletons_matches_previous(kind: str, lens: str, n_people: int) -> None:
    """사람 묶음 삼각측량(1차 + 순차 경로) = 이전 구현. 정답 연결과, 다른 사람 view 를 섞은 묶음(이상치 경로)."""
    rig = _rig(lens)
    fallback = 0
    for t, views in _frames(kind, rig, n_people, 12, 31 + n_people):
        people: dict[str, dict[str, InterpolatedView]] = {}
        for lv in views:
            people.setdefault(lv.local_track_id.split("-")[1], {})[lv.camera_id] = lv.view
        groups = [v for v in people.values() if len(v) >= 2]
        ids = sorted(people)
        for a, b in itertools.pairwise(ids):  # 사람 a 의 cam1·cam2 + 사람 b 의 cam3·cam4
            mixed = {c: v for c, v in people[a].items() if c in ("cam1", "cam2")}
            mixed.update({c: v for c, v in people[b].items() if c in ("cam3", "cam4")})
            if len(mixed) >= 2:
                groups.append(mixed)
        stats: dict[str, int] = {}
        new = tri.triangulate_skeletons(groups, rig, stats=stats)
        old = legacy_tri.triangulate_skeletons(groups, rig)
        assert_identical(new, old)
        # 한 사람씩 부르는 경로(관절 게이트가 다시 삼각측량할 때)도 같다. 한 사람 1차 풀이는 사람 묶음과 카메라 열이 달라
        # 마지막 자리가 다를 수 있으므로(-168 부터 그렇다) 같은 경로끼리 비교한다
        for g in groups[:3]:
            assert_identical(tri.triangulate_skeleton(g, rig), legacy_tri.triangulate_skeleton(g, rig))
        fallback += stats.get("tri_fallback_joints", 0)
    assert fallback > 0


def test_joint_consistency_matches_previous() -> None:
    """엉덩이 한 점 재투영 검사(관절 순서대로 None·inf 판단) = 이전 구현."""
    rig = _rig("wide")
    rng = np.random.default_rng(9)
    seen = set()
    for t, views in _frames("s17", rig, 4, 25, 3):
        und = {lv.key: rig.cameras[lv.camera_id].undistort_keypoints(lv.view.keypoints) for lv in views}
        keys = sorted(und)
        for _ in range(12):
            pick = [keys[i] for i in rng.permutation(len(keys))[: int(rng.integers(2, 5))]]
            cams = [k[0] for k in pick]
            if len(set(cams)) != len(cams):
                continue
            split = int(rng.integers(1, len(pick)))
            groups = [[und[k] for k in pick[:split]], [und[k] for k in pick[split:]]]
            joints = tuple(rng.choice([11, 12, 5, 6, 15], size=int(rng.integers(1, 4)), replace=False).tolist())
            for g in (groups, [groups[0] + groups[1]]):
                new = tri.joint_consistency_px(g, joints, 0.3)
                old = legacy_tri.joint_consistency_px(g, joints, 0.3)
                assert_identical(new, old)
                seen.add("none" if new is None else ("inf" if math.isinf(new) else "value"))
    assert {"none", "value"} <= seen


# ---------------------------------------------------------------- 교차 시점


def _compare_associate(rig: CameraRig, frames, config_new=None, config_old=None) -> dict[str, int]:
    new = cv.CrossViewAssociator(rig, config=config_new)
    old = legacy_cv.CrossViewAssociator(rig, config=config_old)
    work: dict[str, int] = {}
    for t, views in frames:
        a, b = new.associate(t, views), old.associate(t, views)
        assert_identical(a, b)
        assert set(a.work) == set(cv.WORK_KEYS) and all(v >= 0 for v in a.work.values())
        for k, v in a.work.items():
            work[k] = work.get(k, 0) + v
    return work


@pytest.mark.parametrize(
    ("kind", "lens", "n_people"),
    [
        ("clean", "wide", 2),
        ("s17", "wide", 2),
        ("s17", "wide", 3),
        ("s17", "wide", 5),
        ("harsh", "fold", 5),
        ("harsh", "fisheye", 3),
        ("crowd", "drifted", 5),
        ("crowd", "wide", 6),
    ],
)
def test_associate_matches_previous(kind: str, lens: str, n_people: int) -> None:
    """Cross-view 연결 결과 전체(클러스터·골격·사유·경쟁 후보·association_id·쌍 근거) = 이전 구현, tick 이어서."""
    rig = _rig(lens)
    work = _compare_associate(rig, _frames(kind, rig, n_people, 16, 50 + n_people))
    if kind != "clean":
        assert work["tri_fallback_joints"] > 0
    if kind == "s17" and n_people >= 3:
        assert work["conflict_components"] > 0 and work["partition_search_visits"] > 0


def test_associate_matches_previous_other_configs() -> None:
    """비가중 중앙값 경로(joint_noise_weighted=False)와 탐욕 병합 경로(graph_clustering_enabled=False)도 같다."""
    rig = _rig("wide")
    for kwargs in ({"joint_noise_weighted": False}, {"graph_clustering_enabled": False}):
        frames = _frames("s17", rig, 4, 12, 77)
        _compare_associate(rig, frames, cv.CrossViewConfig(**kwargs), legacy_cv.CrossViewConfig(**kwargs))


def test_view_split_rounds_match_previous() -> None:
    """VIEW_SPLIT 을 라운드마다 묶어 다시 검증해도 결과·떼어 낸 view·표시 순서가 클러스터 하나씩 검증할 때와 같다.

    다른 사람 view 를 섞은 클러스터를 직접 만든다: X(맞는 3대 + 다른 사람 1대) 한 번, Y(맞는 2대 + 서로 다른 사람 2대) 두 번
    떼어 내야 하고(두 번째 라운드), Z 는 그대로다.
    """
    rig = _rig("wide")
    _, views = ab.scene(rig, 5, 1, 4, "clean")[0]
    by = {(lv.camera_id, lv.local_track_id.split("-")[1]): lv for lv in views}
    plan = [
        [("cam1", "p0"), ("cam2", "p0"), ("cam3", "p0"), ("cam4", "p1")],
        [("cam1", "p2"), ("cam2", "p2"), ("cam3", "p3"), ("cam4", "p4")],
        [("cam1", "p1"), ("cam2", "p1"), ("cam3", "p1")],
    ]
    used = {k for cluster in plan for k in cluster}
    results = []
    for module in (cv, legacy_cv):
        assoc = module.CrossViewAssociator(rig)
        prepared = {lv.key: assoc._prepare(lv) for lv in views}
        works = [module._Work([by[k].key for k in cluster]) for cluster in plan]
        works += [module._Work([lv.key]) for k, lv in sorted(by.items()) if k not in used]
        flags: list[Any] = []
        skeletons = assoc._validate(works, prepared, flags)
        results.append((skeletons, [w.keys for w in works], [(str(f.reason), f.flagged, f.involved) for f in flags]))
    (sk_new, works_new, flags_new), (sk_old, works_old, flags_old) = results
    assert works_new == works_old and flags_new == flags_old
    assert sorted(sk_new) == sorted(sk_old)
    for i in sk_new:
        assert_identical(sk_new[i], sk_old[i])
    assert [f[0] for f in flags_new] == ["VIEW_SPLIT"] * 3  # X 한 번, Y 두 번


def test_partition_cache_hits_match_uncached() -> None:
    """충돌 성분 분할 결과 다시 쓰기(-726)를 끈 것과 같다(같은 입력 키면 같은 결과)."""
    rig = _rig("wide")
    frames = _frames("s17", rig, 5, 20, 8)
    cached = cv.CrossViewAssociator(rig)
    uncached = cv.CrossViewAssociator(rig)

    class _NoPartitionMemo(cv._AssignMemo):
        @property
        def partition(self) -> dict:  # type: ignore[override]
            return {}

        @partition.setter
        def partition(self, value: dict) -> None:
            pass

    original = cv._AssignMemo
    for t, views in frames:
        a = cached.associate(t, views)
        cv._AssignMemo = _NoPartitionMemo  # type: ignore[misc]
        try:
            b = uncached.associate(t, views)
        finally:
            cv._AssignMemo = original  # type: ignore[misc]
        assert_identical(a, b)
        assert a.work["partition_search_visits"] <= b.work["partition_search_visits"]


# ---------------------------------------------------------------- World Track


@pytest.mark.parametrize(("kind", "n_people"), [("s17", 3), ("s17", 5), ("crowd", 5), ("harsh", 4)])
def test_world_track_matches_previous(kind: str, n_people: int) -> None:
    """World Track 갱신(경쟁 후보 색인·게이트 카메라 쌍 행렬 다시 쓰기) = 이전 `_others`·경쟁 후보 검사, tick 이어서."""
    rig = _rig("wide")
    associator = cv.CrossViewAssociator(rig)
    new, old = WorldTrackManager(WorldIdIssuer("eq")), WorldTrackManager(WorldIdIssuer("eq"))
    for t, views in _frames(kind, rig, n_people, 20, 90 + n_people):
        frame = associator.associate(t, views)
        a = new.update(frame, rig)
        with legacy_world.legacy_world():
            b = old.update(frame, rig)
        assert_identical(a, b)
    assert dict(new.diag) == dict(old.diag)


def test_others_index_matches_previous() -> None:
    """경쟁 후보 색인 = 클러스터마다 모으던 `_others` (내용·순서). 왜곡 제거 결과가 빠진 카메라, frame 밖 클러스터 포함."""
    rig = _rig("wide")
    associator = cv.CrossViewAssociator(rig)
    checked = 0
    for t, views in _frames("crowd", rig, 5, 6, 12):
        frame = associator.associate(t, views)
        variants = [frame]
        if frame.clusters:  # 한 클러스터의 왜곡 제거 결과 하나를 지운 frame (그 카메라는 왜곡 제거 목록에서 빠져야 한다)
            first = frame.clusters[0]
            drop = next(iter(first.undistorted))
            stripped = dataclasses.replace(first, undistorted={k: v for k, v in first.undistorted.items() if k != drop})
            variants.append(dataclasses.replace(frame, clusters=(stripped, *frame.clusters[1:])))
        for f in variants:
            index = wt._OthersIndex(f)
            outsider = dataclasses.replace(f.clusters[0]) if f.clusters else None
            for c in (*f.clusters, *([outsider] if outsider is not None else [])):
                new_others, new_und = index.others(c)
                old_others, old_und = legacy_world._others(f, c)
                assert new_others == old_others
                assert set(new_und) == set(old_und)
                for cid in old_und:
                    assert [id(u) for u in new_und[cid]] == [id(u) for u in old_und[cid]]
                checked += 1
    assert checked > 10


# ---------------------------------------------------------------- 계측


def test_work_counters_reach_tracking_summary() -> None:
    """계산량이 AssociationFrame.work → 추적 계측 30 s 창·누계·최근 256 tick 요약으로 간다(판정에는 쓰지 않는다)."""
    rig = _rig("wide")
    associator = cv.CrossViewAssociator(rig)
    diag = TrackingDiagnostics()
    frames = ab.scene(rig, 3, 8, 5, "s17")
    for k, (t, views) in enumerate(frames):
        diag.observe_tick(t, ())
        frame = associator.associate(t, views)
        diag.observe_association(frame)
    window = diag.take_window(frames[-1][0])
    assert window["work"]["ticks"] == len(frames)
    assert window["work"]["mean"]["views"] == pytest.approx(np.mean([len(v) for _, v in frames]))
    assert window["work"]["max"]["raw_pairs"] >= window["work"]["mean"]["raw_pairs"] > 0
    line = window_log_line(window)
    assert "교차 시점 계산량" in line and "pair_svd_joints" in line
    recent = diag.work_summary()
    assert set(recent) == set(cv.WORK_KEYS)
    assert recent["views"]["max"] >= recent["views"]["p95"] > 0 and recent["views"]["mean"] > 0
    assert diag.summary()["work"]["ticks"] == len(frames)
