"""Tier 0 판정 틱 기하(교차 시점·World Track) 출력 동일 최적화의 전후 A/B (S15P31A101-726, docs/design/S15P31A101-726-tier0-hotpath.md).

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다. GPU 를 쓰지 않는다.

    uv run python tests/geometry/bench_tier0_hotpath_ab.py                         # 2·3·5명 × clean·s17, 60 tick
    uv run python tests/geometry/bench_tier0_hotpath_ab.py --people 3 --scene s17 --ticks 100

같은 프로세스에서 tick 마다 이전 구현(고정 사본 `legacy_726_*`, dev bd706a9f)과 지금 구현을 같은 입력으로 번갈아 잰다
(짝수 tick 은 이전 먼저, 홀수 tick 은 지금 먼저. -237 의 교대 A/B 와 같은 방법). 단계:
- cross_view: `CrossViewAssociator.associate` 전체. 그 안의 쌍 비교(cross_view_pairs), 묶기(cross_view_clustering),
  클러스터 검증 삼각측량(cross_view_validate_triangulate)은 서비스와 같은 `StageTiming` 계측으로 나눈다.
- world_track: `WorldTrackManager.update`. 두 manager 에 같은 연결 결과(지금 구현의 frame)를 넣고, 이전 쪽은
  `legacy_726_world.legacy_world()`(이전 `_others`·경쟁 후보 검사) 안에서 부른다.

장면:
- clean: `bench_tier0_geometry` 와 같다(픽셀 잡음 σ 1 px, 신뢰도 기준 미만 10%, 검출 없음 3%).
- s17: 17차 재생형. 카메라 cam3·cam4 의 keypoint 가 200·300 ms 전 자세(촬영 시각 어긋남 188~342 ms 흉내), 보행 1 m/s,
  픽셀 잡음 σ 2 px, 이상치 keypoint 8%(30~80 px), view 가림 10%, 카메라마다 tick 당 7% 확률로 로컬 트랙 ID 가 새로
  붙는다(조각남). 1대 클러스터·EPIPOLAR 기각·순차 삼각측량 경로가 늘어난다.

결과는 SIM 합성 입력의 처리 시간이다. 정확도 지표가 아니다. 운영 판정은 라이브 측정으로 한다(명세 6.6).
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalView
from simai_ai.tracking.stage_timing import StageTiming
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackManager

HERE = Path(__file__).resolve().parent
STAGES = ("cross_view_pairs", "cross_view_clustering", "cross_view_validate_triangulate")
S17_DELAY_TICKS = {"cam1": 0, "cam2": 0, "cam3": 2, "cam4": 3}


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))  # legacy 사본끼리의 import
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


geo = _load("bench_tier0_geometry")


def scene(rig: Any, n_people: int, ticks: int, seed: int, kind: str) -> list[tuple[int, list[LocalView]]]:
    """tick 마다 (공통 시각, view 목록). kind 는 clean 또는 s17 (모듈 설명)."""
    rng = np.random.default_rng(seed)
    people = geo.make_people(n_people, rng)
    if kind == "s17":
        for p in people:
            p.velocity = p.velocity / max(float(np.linalg.norm(p.velocity)), 1e-6) * 1.0
    history: list[list[np.ndarray]] = []
    fragment = dict.fromkeys(rig.cameras, 0)
    lag = max(S17_DELAY_TICKS.values())
    out = []
    for k in range(ticks + lag):
        history.append([p.joints().copy() for p in people])
        geo.step_people(people, rng)
        if k < lag:
            continue
        t = geo.T0 + k * geo.TICK_MS
        views = []
        for cid, cam in rig.cameras.items():
            if kind == "s17" and rng.random() < 0.07:
                fragment[cid] += 1
            for i in range(n_people):
                if kind == "s17":
                    kps = geo.keypoints_of(cam, history[-1 - S17_DELAY_TICKS.get(cid, 0)][i], rng, noise_px=2.0, outlier_rate=0.08)
                    if rng.random() < 0.1:
                        continue
                else:
                    kps = geo.keypoints_of(cam, history[-1][i], rng)
                if all(kp is None for kp in kps):
                    continue
                view = InterpolatedView(cid, t, kps, "SIM", 0.0, (t,))
                views.append(LocalView(cid, f"{cid}-p{i}-f{fragment[cid]}", view))
        out.append((t, views))
    return out


def _stats(values: list[float]) -> dict[str, float]:
    return {"mean": statistics.fmean(values), "p95": float(np.percentile(values, 95))}


def bench(n_people: int, ticks: int, seed: int, kind: str, warmup: int = 3) -> dict[str, Any]:
    legacy_cv = _load("legacy_726_cross_view")
    legacy_world = _load("legacy_726_world").legacy_world
    rig = geo.make_rig()
    frames = scene(rig, n_people, warmup + ticks, seed, kind)
    impl = {
        "before": (legacy_cv.CrossViewAssociator(rig), WorldTrackManager(WorldIdIssuer("ab")), legacy_world),
        "after": (CrossViewAssociator(rig), WorldTrackManager(WorldIdIssuer("ab")), None),
    }
    times: dict[str, dict[str, list[float]]] = {name: {} for name in impl}
    work: dict[str, list[int]] = {}
    for k, (t, views) in enumerate(frames):
        order = ("before", "after") if k % 2 == 0 else ("after", "before")
        timings = {name: StageTiming() for name in impl}
        cv_ms: dict[str, float] = {}
        assoc: dict[str, Any] = {}
        for name in order:
            associator = impl[name][0]
            s = time.perf_counter()
            assoc[name] = associator.associate(t, views, timing=timings[name], timing_people=n_people)
            cv_ms[name] = (time.perf_counter() - s) * 1000.0
        wt_ms: dict[str, float] = {}
        for name in order:
            manager, ctx = impl[name][1], impl[name][2]
            frame = assoc["after"]  # 같은 연결 결과(두 구현의 연결 결과는 같다)를 넣는다
            if ctx is None:
                s = time.perf_counter()
                manager.update(frame, rig)
                wt_ms[name] = (time.perf_counter() - s) * 1000.0
            else:
                with ctx():
                    s = time.perf_counter()
                    manager.update(frame, rig)
                    wt_ms[name] = (time.perf_counter() - s) * 1000.0
        if k < warmup:
            continue
        for name in impl:
            row = times[name]
            row.setdefault("cross_view", []).append(cv_ms[name])
            summary = timings[name].summary()
            for stage in STAGES:
                row.setdefault(stage, []).append(summary.get(stage, {}).get(str(n_people) if n_people <= 6 else "7+", {}).get("wall_ms", {}).get("mean", 0.0))
            row.setdefault("world_track", []).append(wt_ms[name])
            row.setdefault("judge_geometry", []).append(cv_ms[name] + wt_ms[name])
        for key, value in assoc["after"].work.items():
            work.setdefault(key, []).append(int(value))
    return {
        "people": n_people,
        "scene": kind,
        "ticks": ticks,
        "views_per_tick": statistics.fmean(len(v) for _, v in frames[warmup:]),
        "stages_ms": {name: {stage: _stats(v) for stage, v in row.items()} for name, row in times.items()},
        "work_mean": {key: statistics.fmean(v) for key, v in work.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--people", type=int, nargs="+", default=[2, 3, 5])
    parser.add_argument("--scene", nargs="+", default=["clean", "s17"], choices=["clean", "s17"])
    parser.add_argument("--ticks", type=int, default=60)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("| 장면 | 사람 | view/tick | 단계 | 이전 평균 / p95 ms | 지금 평균 / p95 ms | 비율(평균) |")
    print("|---|---:|---:|---|---:|---:|---:|")
    works = []
    for kind in args.scene:
        for n in args.people:
            r = bench(n, args.ticks, args.seed + n, kind)
            works.append(r)
            before, after = r["stages_ms"]["before"], r["stages_ms"]["after"]
            for stage in ("judge_geometry", "cross_view", *STAGES, "world_track"):
                b, a = before[stage], after[stage]
                ratio = a["mean"] / b["mean"] if b["mean"] else float("nan")
                print(
                    f"| {kind} | {n} | {r['views_per_tick']:.1f} | {stage} | {b['mean']:.2f} / {b['p95']:.2f} | "
                    f"{a['mean']:.2f} / {a['p95']:.2f} | {ratio:.2f} |"
                )
    print()
    print("계산량(tick 평균, 지금 구현의 AssociationFrame.work):")
    for r in works:
        print(f"- {r['scene']} {r['people']}명: " + ", ".join(f"{k} {v:.1f}" for k, v in r["work_mean"].items()))


if __name__ == "__main__":
    main()
