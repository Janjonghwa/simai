"""World Track 관절 게이트가 Cross-view 클러스터의 왜곡 제거·삼각측량 결과를 다시 써도 결과가 같다 (S15P31A101-168 연결).

장면은 bench_tier0_geometry.py 와 같다(광각 배럴 왜곡, 10명, 픽셀 잡음, 신뢰도 기준 미만·검출 없음 관절). 같은 Cross-view
결과를 다시 쓰기를 켠 Manager 와 끈 Manager(클러스터 왜곡 제거 결과를 비우고 `reusable_for` 를 거짓으로)에 넣고 모든
결과(WorldTrackFrame 전체, 관절 게이트 결과의 모든 필드 포함)를 재귀로 비교해 범주형 값은 완전히 같게, 실수는 1e-9 안에서 같게
본다. 다시 쓰기 경로가 실제로 돌았는지도 센다. SIM 합성 입력이다.
"""

import dataclasses
import importlib.util
import math
from collections.abc import Mapping
from pathlib import Path

import pytest

from simai_ai.geometry import triangulation

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("bench_world_track", HERE / "bench_world_track.py")
bw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bw)

TOL = 1e-9


def _same(a, b, path="frame"):
    """dataclass·tuple·dict 를 재귀로 비교한다. 실수는 1e-9 안, 나머지(범주형·문자열·정수)는 완전히 같아야 한다."""
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        assert type(a) is type(b), path
        for fld in dataclasses.fields(a):
            _same(getattr(a, fld.name), getattr(b, fld.name), f"{path}.{fld.name}")
    elif isinstance(a, Mapping):
        assert isinstance(b, Mapping) and a.keys() == b.keys(), path
        for k in a:
            _same(a[k], b[k], f"{path}[{k!r}]")
    elif isinstance(a, (tuple, list)):
        assert isinstance(b, (tuple, list)) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) and not isinstance(a, bool):
        assert isinstance(b, float) and math.isclose(a, b, rel_tol=0.0, abs_tol=TOL), (path, a, b)
    else:
        assert a == b, (path, a, b)


@pytest.mark.parametrize("n_people", [3, 10])
def test_world_track_with_cluster_reuse_matches_recompute(n_people, monkeypatch):
    rig, frames, _ = bw.scene_frames(n_people, 8, seed=11 + n_people)
    calls = {True: 0, False: 0}
    original = triangulation.SkeletonEstimate.reusable_for

    def counting(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        calls[result] += 1
        return result

    monkeypatch.setattr(triangulation.SkeletonEstimate, "reusable_for", counting)
    reuse, _ = bw.run_manager(rig, frames, reuse=True)
    monkeypatch.setattr(triangulation.SkeletonEstimate, "reusable_for", original)
    assert calls[True] > 0 and calls[False] == 0  # 다시 쓰기 경로가 실제로 돌았다
    recompute, _ = bw.run_manager(rig, frames, reuse=False)
    assert sum(len(f.tracks) for f in reuse) > 0
    for fa, fb in zip(reuse, recompute, strict=True):
        _same(fa, fb)
