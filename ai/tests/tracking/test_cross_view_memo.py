"""Cross-view 배정 재사용(S15P31A101-663)이 기대는 성질: 중앙값 비트 동일, 쌍 3D 어긋남의 대칭, 메모의 수명.

전체 동작(클러스터·쌍 근거·World Track·RiskEvent)이 이전 구현과 같은지는 tests/services/tier0/test_judge_core_equivalence.py 가 본다.
"""

import warnings

import numpy as np

from simai_ai.tracking import cross_view as cv


def _bits(x: float) -> bytes:
    return np.float64(x).tobytes()


def test_median_1d_matches_np_median_bitwise():
    rng = np.random.default_rng(663)
    for _ in range(20_000):
        n = int(rng.integers(1, 18))
        values = np.abs(rng.normal(0.0, 1.0, n)) * 10.0 ** float(rng.integers(-6, 3))
        r = rng.random()
        if r < 0.05:
            values[rng.integers(0, n)] = np.nan
        elif r < 0.10:
            values[rng.integers(0, n)] = np.inf
        elif r < 0.20:
            values[:] = values[0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            expected = float(np.median(values))
        assert _bits(cv._median_1d(values)) == _bits(expected)


def _joints(rng: np.random.Generator) -> np.ndarray:
    pts = rng.normal(0.0, 1.0, (17, 3)) + rng.normal(0.0, 3.0, 3)
    pts[rng.random(17) < 0.3] = np.nan
    return pts


def test_pair_3d_distance_is_symmetric_bitwise():
    """메모가 (pk_a, pk_b) 를 정렬한 키로 다시 쓰는 근거: 인자 순서를 바꿔도 비트 단위로 같다."""
    rng = np.random.default_rng(6630)
    for _ in range(5_000):
        ja, jb = _joints(rng), _joints(rng)
        if rng.random() < 0.2:  # 겹치는 관절 없음 → 수평 중심 거리 경로
            jb[~np.isnan(ja[:, 0])] = np.nan
        d_ab, d_ba = cv._pair_3d_distance(ja, jb), cv._pair_3d_distance(jb, ja)
        assert (d_ab is None) == (d_ba is None)
        if d_ab is not None:
            assert _bits(d_ab) == _bits(d_ba)


def test_memos_only_live_inside_one_call():
    """메모는 `_assign_graph`·`_pairs` 한 번 동안만 있다(다음 시각·다른 입력에 새지 않는다)."""
    rig = _rig()
    assoc = cv.CrossViewAssociator(rig)
    seen = {}
    original = cv.CrossViewAssociator._graph_linkage_compute

    def spy(self, *args):
        seen["memo"] = self._memo
        seen["threshold"] = self._threshold_memo
        return original(self, *args)

    cv.CrossViewAssociator._graph_linkage_compute = spy
    try:
        views = _views(rig, 1_760_342_400_000)
        assoc.associate(1_760_342_400_000, views)
    finally:
        cv.CrossViewAssociator._graph_linkage_compute = original
    assert seen["memo"] is not None and seen["threshold"] is None
    assert assoc._memo is None and assoc._threshold_memo is None


def _rig():
    import importlib.util
    import sys
    from pathlib import Path

    name = "bench_tier0_geometry"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "geometry" / "bench_tier0_geometry.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].make_rig()


def _views(rig, t):
    import sys

    geo = sys.modules["bench_tier0_geometry"]
    rng = np.random.default_rng(5)
    people = geo.make_people(3, rng)
    return geo.tick_input(rig, people, t, rng).local_views
