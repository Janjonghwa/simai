"""World Track 갱신이 Cross-view 클러스터의 왜곡 제거·삼각측량 결과를 관절 게이트에 넘길 때의 처리 시간을 잰다
(S15P31A101-40 의 -168 연결, docs/design/tier0-world-track.md "처리 시간").

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다. GPU 를 쓰지 않는다.

    uv run python tests/tracking/bench_world_track.py                  # 10명, 30 tick, 표 (Markdown)
    uv run python tests/tracking/bench_world_track.py --people 1 5 10 --ticks 50

장면은 tests/geometry/bench_tier0_geometry.py 와 같다(교실 모서리 카메라 4대, 광각 배럴 왜곡, 픽셀 잡음 σ 1 px,
신뢰도 기준 미만 10%, 검출 없음 3%, 사람은 tick(10 Hz)마다 걷는다). tick 마다 `CrossViewAssociator.associate` 한 번의
결과(AssociationFrame)를 두 WorldTrackManager 에 같이 넣는다.

- world_track_reuse: 지금 구현. 관절 게이트가 `ViewCluster.undistorted`·`ViewCluster.skeleton` 을 다시 쓴다.
- world_track_recompute: 같은 입력에서 다시 쓰기를 끈 경우(클러스터의 왜곡 제거 결과를 비우고
  `SkeletonEstimate.reusable_for` 를 거짓으로). -168 이전처럼 게이트가 왜곡 제거·삼각측량을 다시 한다.
- cross_view: 참고용 Cross-view 연결 시간.

결과는 SIM 합성 입력의 처리 시간이다. 정확도 지표가 아니다. 운영 판정은 Pod CPU 측정으로 한다(명세 6.6).
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.geometry import triangulation
from simai_ai.tracking.cross_view import AssociationFrame, CrossViewAssociator
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackManager

HERE = Path(__file__).resolve().parent


def _load_bench() -> Any:
    name = "bench_tier0_geometry"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE.parent / "geometry" / "bench_tier0_geometry.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass 가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


geo = _load_bench()


@contextmanager
def no_reuse():
    """관절 게이트가 클러스터 삼각측량을 다시 쓰지 않게 한다 (-168 이전 동작과 같은 계산)."""
    original = triangulation.SkeletonEstimate.reusable_for
    triangulation.SkeletonEstimate.reusable_for = lambda self, *a, **k: False  # type: ignore[method-assign]
    try:
        yield
    finally:
        triangulation.SkeletonEstimate.reusable_for = original  # type: ignore[method-assign]


def strip(frame: AssociationFrame) -> AssociationFrame:
    """클러스터의 왜곡 제거 결과를 비운 같은 연결 결과."""
    return dataclasses.replace(frame, clusters=tuple(dataclasses.replace(c, undistorted={}) for c in frame.clusters))


def scene_frames(n_people: int, ticks: int, seed: int) -> tuple[Any, list[AssociationFrame], list[float]]:
    """(rig, tick 마다 Cross-view 결과, Cross-view 시간 ms)."""
    rng = np.random.default_rng(seed)
    rig = geo.make_rig()
    people = geo.make_people(n_people, rng)
    associator = CrossViewAssociator(rig)
    frames, cross = [], []
    for k in range(ticks):
        inp = geo.tick_input(rig, people, geo.T0 + k * geo.TICK_MS, rng)
        geo.step_people(people, rng)
        s = time.perf_counter()
        frames.append(associator.associate(inp.t, inp.local_views))
        cross.append((time.perf_counter() - s) * 1000.0)
    return rig, frames, cross


def run_manager(rig: Any, frames: list[AssociationFrame], reuse: bool) -> tuple[list[Any], list[float]]:
    """WorldTrackManager 결과와 tick 마다의 갱신 시간 ms."""
    manager = WorldTrackManager(WorldIdIssuer("bench"))
    out, times = [], []
    for frame in frames:
        if reuse:
            s = time.perf_counter()
            out.append(manager.update(frame, rig))
            times.append((time.perf_counter() - s) * 1000.0)
        else:
            stripped = strip(frame)
            with no_reuse():
                s = time.perf_counter()
                out.append(manager.update(stripped, rig))
                times.append((time.perf_counter() - s) * 1000.0)
    return out, times


def bench(n_people: int, ticks: int, seed: int, warmup: int = 2) -> dict[str, Any]:
    rig, frames, cross = scene_frames(n_people, warmup + ticks, seed)
    _, reuse = run_manager(rig, frames, reuse=True)
    _, recompute = run_manager(rig, frames, reuse=False)

    def stats(v: list[float]) -> dict[str, float]:
        v = v[warmup:]
        return {"mean": statistics.fmean(v), "p95": float(np.percentile(v, 95)), "max": max(v)}

    return {
        "people": n_people,
        "ticks": ticks,
        "stages_ms": {
            "cross_view": stats(cross),
            "world_track_reuse": stats(reuse),
            "world_track_recompute": stats(recompute),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people", type=int, nargs="+", default=[10])
    parser.add_argument("--ticks", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    env = geo.environment()
    print(
        f"환경: {env['platform']}, {env['processor']}, CPU {env['cpu_count']}, "
        f"Python {env['python']}, numpy {env['numpy']}"
    )
    print()
    print("| 사람 | 단계 | 평균 ms | p95 ms | 최대 ms |")
    print("|---:|---|---:|---:|---:|")
    for n in args.people:
        r = bench(n, args.ticks, args.seed + n)
        for name, s in r["stages_ms"].items():
            print(f"| {r['people']} | {name} | {s['mean']:.1f} | {s['p95']:.1f} | {s['max']:.1f} |")


if __name__ == "__main__":
    main()
