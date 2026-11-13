"""S15P31A101-663 판정 중심 벤치·동치 시험 공용 합성 장면과 실행기(docs/design/S15P31A101-663-judge-core-optimize.md).

GPU·모델 없이 실제 `Perception`(Cross-view → World Track → 접지 → world_state payload)과 `JudgmentLoop`(구성 묶음·
Risk Engine·LifecycleTracker·저널)를 그대로 돌린다. 검출·로컬 추적·Re-ID 모델만 결정적 합성 입력으로 바꾼다.

- 방(9 × 7.5 m)·카메라 4대·관절 오프셋은 `tests/geometry/bench_tier0_geometry.py` 와 같다(광각 배럴 왜곡, σ 1 px,
  신뢰도 기준 미만 10%, 검출 없음 3%).
- 사람(Actor)은 경유점을 따라 걷고, 카메라별 가림·완전 이탈(재입장 때 로컬 ID 가 바뀐다)·카메라별 위치 어긋남(2+2 분할
  그림자)·넘어짐(누운 자세)을 대본으로 갖는다.
- Re-ID 특징은 사람마다 고정 기준 벡터 + 결정적 잡음이다(3 tick 에 한 번, 1 s 안의 최신 특징을 돌려준다).
- 같은 입력·같은 코드면 trace(world_state payload, ID 교정, RiskEvent 전이, World Track 진단 계수)가 바이트 단위로 같다.
  msg_id·event_id 는 고정 순번·고정 난수로 만든다.

SIM 합성 입력의 처리 시간·동작 비교용이다. 정확도 지표가 아니다.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parents[1] / "geometry"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ai/ 는 뒤에 붙인다: 이전 코드와 비교할 때 PYTHONPATH 로 앞에 둔 그 코드의 services 를 가리지 않게
if str(HERE.parents[2]) not in sys.path:
    sys.path.append(str(HERE.parents[2]))

import bench_tier0_geometry as geo

T0 = geo.T0
TICK_MS = 100
EMBED_DIM = 128
ZONE = {
    "zone_id": "z-r1",
    "zone_version": "zone-663",
    "judge_type": "GROUND_FOOTPRINT",
    "polygon_xy": [[3.6, 3.0], [5.6, 3.0], [5.6, 4.6], [3.6, 4.6]],
    "z_range": None,
    "severity": "HIGH",
}
RULES = (("R1", 1), ("R4", 1), ("R5", 1001), ("R7", 1))


@dataclass
class Actor:
    """대본이 있는 합성 사람 한 명. tick 은 0 부터."""

    name: str
    waypoints: list[tuple[int, float, float]]
    """(tick, x, y). 사이는 직선 보간, 앞뒤는 끝점 고정"""
    yaw: float = 0.0
    hidden: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    """카메라 → 가린 tick 구간 [시작, 끝)"""
    absent: list[tuple[int, int]] = field(default_factory=list)
    """모든 카메라에서 사라진 구간. 다시 나타나면 로컬 ID 세대가 바뀐다(재입장)"""
    shift: dict[str, tuple[float, float]] = field(default_factory=dict)
    """카메라 → World 위치 어긋남 (dx, dy) m. 2+2 분할 그림자를 만든다"""
    shift_ticks: tuple[int, int] = (0, 0)
    fall_at: int | None = None
    """이 tick 부터 5 tick 동안 누운 자세로 바뀐다"""
    surface_z: float = 0.0
    seed: int = 0

    def xy(self, k: int) -> tuple[float, float]:
        pts = self.waypoints
        if k <= pts[0][0]:
            return pts[0][1], pts[0][2]
        for (k0, x0, y0), (k1, x1, y1) in itertools.pairwise(pts):
            if k <= k1:
                a = (k - k0) / max(1, k1 - k0)
                return x0 + a * (x1 - x0), y0 + a * (y1 - y0)
        return pts[-1][1], pts[-1][2]

    def generation(self, k: int) -> int:
        return sum(1 for _s, e in self.absent if k >= e)

    def is_absent(self, k: int) -> bool:
        return any(s <= k < e for s, e in self.absent)

    def is_hidden(self, cid: str, k: int) -> bool:
        return any(s <= k < e for s, e in self.hidden.get(cid, ()))

    def joints(self, k: int, arm: np.ndarray, cid: str | None = None) -> np.ndarray:
        x, y = self.xy(k)
        if cid is not None and self.shift_ticks[0] <= k < self.shift_ticks[1] and cid in self.shift:
            dx, dy = self.shift[cid]
            x, y = x + dx, y + dy
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        right = np.array([s, -c, 0.0])
        forward = np.array([c, s, 0.0])
        base = np.array([x, y, self.surface_z])
        standing = np.array([base + dx * right + np.array([0.0, 0.0, dz]) for dx, dz in geo.OFFSETS]) + arm
        if self.fall_at is None or k < self.fall_at:
            return standing
        # 누운 자세: 서 있을 때 높이를 앞쪽 거리로 눕히고 높이는 0.1~0.25 m
        lying = np.array([base + dx * right + dz * forward + np.array([0.0, 0.0, 0.12 + 0.05 * dz])
                          for dx, dz in geo.OFFSETS]) + arm * 0.5
        a = min(1.0, (k - self.fall_at + 1) / 5.0)
        return (1 - a) * standing + a * lying


@dataclass
class Scene:
    name: str
    actors: list[Actor]
    ticks: int
    seed: int


def crowd(people: int, ticks: int = 120, seed: int = 663) -> Scene:
    """인원수별 벤치 장면: 무작위 경유점 보행, 근접 쌍(0.45 m), 카메라별 가림 창, 한 명은 R1 구역을 지난다."""
    rng = np.random.default_rng(seed + people)
    actors: list[Actor] = []
    while len(actors) < people:
        i = len(actors)
        if i % 3 == 1 and actors:  # 근접 쌍: 앞 사람과 0.45 m 옆에서 같은 경로
            ref = actors[-1]
            off = 0.45
            pts = [(k, x + off * math.sin(ref.yaw), y - off * math.cos(ref.yaw)) for k, x, y in ref.waypoints]
            actors.append(Actor(f"p{i}", pts, yaw=ref.yaw, seed=seed * 31 + i))
            continue
        pts = []
        for k in range(0, ticks + 40, 40):
            pts.append((k, float(rng.uniform(1.2, 7.8)), float(rng.uniform(1.2, 6.3))))
        if i == 0:
            pts = [(0, 1.5, 2.0), (ticks // 3, 4.6, 3.8), (2 * ticks // 3, 4.4, 3.6), (ticks, 7.5, 5.5)]
        hidden: dict[str, list[tuple[int, int]]] = {}
        for cid in geo.CORNERS:
            if rng.random() < 0.5:
                start = int(rng.integers(5, max(6, ticks - 20)))
                hidden[cid] = [(start, start + int(rng.integers(5, 20)))]
        actors.append(Actor(f"p{i}", pts, yaw=float(rng.uniform(-math.pi, math.pi)), hidden=hidden,
                            seed=seed * 31 + i))
    return Scene(f"crowd-{people}", actors, ticks, seed + people)


def mixed(ticks: int = 300, seed: int = 6630) -> Scene:
    """동치 시험용 혼합 장면: 근접 2인, 가림, 2+2 분할 그림자, 병합·분할, 재입장, 낙상(R4), 구역 진입(R1·R5)."""
    actors = [
        # 근접 2인: 0.4 m 옆에서 나란히 R1 구역을 가로지른다
        Actor("close-a", [(0, 1.5, 3.4), (120, 7.5, 3.9), (240, 1.5, 4.2)], yaw=0.08, seed=1),
        Actor("close-b", [(0, 1.5, 3.0), (120, 7.5, 3.5), (240, 1.5, 3.8)], yaw=0.08, seed=2),
        # 가림: 카메라 둘이 번갈아 가려 2대 관측이 되고, 잠깐 3대가 가린다
        Actor("occl", [(0, 2.0, 5.8), (150, 7.0, 5.8), (300, 2.0, 5.8)], yaw=1.2,
              hidden={"cam1": [(30, 70), (180, 200)], "cam2": [(60, 110), (180, 200)], "cam3": [(185, 200)]},
              seed=3),
        # 2+2 분할 그림자: cam3·cam4 가 본 위치가 0.7 m 어긋난다(cam1·cam2 와 다른 클러스터가 된다)
        Actor("shadow", [(0, 6.5, 1.6), (300, 6.0, 2.4)], yaw=-0.5, shift={"cam3": (0.7, 0.3), "cam4": (0.7, 0.3)},
              shift_ticks=(40, 160), seed=4),
        # 병합·분할: 둘이 다가가 겹친 뒤 다시 갈라진다
        Actor("merge-a", [(0, 1.4, 1.4), (90, 3.0, 1.6), (130, 3.0, 1.6), (220, 1.4, 2.6)], yaw=0.3, seed=5),
        Actor("merge-b", [(0, 4.8, 1.4), (90, 3.1, 1.65), (130, 3.1, 1.65), (220, 4.8, 2.4)], yaw=2.9, seed=6),
        # 재입장: 70 tick 사라졌다 돌아온다(로컬 ID 세대가 바뀐다)
        Actor("reentry", [(0, 7.6, 6.2), (80, 6.0, 5.0), (150, 6.0, 5.0), (300, 7.4, 3.0)], yaw=-2.0,
              absent=[(80, 150)], seed=7),
        # 낙상: 구역 옆으로 걸어와 넘어진다
        Actor("fall", [(0, 2.2, 4.8), (150, 4.0, 4.2), (300, 4.0, 4.2)], yaw=-0.3, fall_at=180, seed=8),
    ]
    return Scene("mixed", actors, ticks, seed)


@dataclass
class _Feature:
    t: int
    vec: np.ndarray
    orientation: int


class SceneFeed:
    """tick 마다 카메라 4대의 Observation2DFrame 묶음을 낸다(검출·로컬 추적 대역)."""

    source = "SIM"

    def __init__(self, rig: Any, scene: Scene) -> None:
        from simai_ai.tracking.local_tracker import (
            Observation2DFrame,
            PoseDetection,
            TrackedDetection,
        )

        self.camera_ids = tuple(sorted(rig.cameras))
        self.frames: list[list[Any]] = []
        self.features: dict[tuple[str, str], list[_Feature]] = {}
        self.index = 0
        rng = np.random.default_rng(scene.seed)
        arms = {a.name: np.random.default_rng(a.seed).normal(0.0, 0.03, (17, 3)) for a in scene.actors}
        for a in scene.actors:
            arms[a.name][:5] *= 0.3
        bases = {a.name: np.random.default_rng(10_000 + a.seed).normal(0.0, 1.0, EMBED_DIM) for a in scene.actors}
        for k in range(scene.ticks):
            t = T0 + k * TICK_MS
            batch = []
            for ci, cid in enumerate(self.camera_ids):
                cam = rig.cameras[cid]
                dets = []
                for a in scene.actors:
                    if a.is_absent(k) or a.is_hidden(cid, k):
                        continue
                    joints = a.joints(k, arms[a.name], cid)
                    kps = geo.keypoints_of(cam, joints, rng)
                    pts = np.array([(u, v) for kp in kps if kp is not None for u, v in [kp[:2]]])
                    if len(pts) < 3:
                        continue
                    lo, hi = pts.min(axis=0) - 10, pts.max(axis=0) + 10
                    local = f"{cid}-{a.name}-{a.generation(k)}"
                    dets.append(TrackedDetection(local, PoseDetection(
                        (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])), 0.9, kps)))
                    if (k + ci + a.seed) % 3 == 0:
                        noise = np.random.default_rng(hash((a.seed, ci, k)) & 0xFFFFFFFF).normal(0.0, 0.25, EMBED_DIM)
                        self.features.setdefault((cid, local), []).append(
                            _Feature(t, bases[a.name] + noise, (k // 10 + ci) % 8))
                batch.append(Observation2DFrame(cid, 1, k, t, "SIM", 0.0, tuple(dets)))
            self.frames.append(batch)

    def poll(self, now_ms: int) -> list[Any]:
        if self.index >= len(self.frames):
            return []
        batch = self.frames[self.index]
        self.index += 1
        return batch

    def camera_connected(self, camera_id: str) -> bool:
        return camera_id in self.camera_ids

    def provenance(self) -> dict[str, Any]:
        return {"source": "SIM", "camera_ids": list(self.camera_ids)}

    def close(self) -> None:
        pass

    def lookup(self, camera_id: str, local_track_id: str, t: int) -> Any:
        """t 이하 1 s 안의 최신 특징(재생 모드 캐시와 같은 뜻). 없으면 None."""
        from simai_ai.reid.collector import LocalTrackFeature

        best = None
        for f in self.features.get((camera_id, local_track_id), ()):
            if f.t <= t and t - f.t <= 1000:
                best = f
        if best is None:
            return None
        return LocalTrackFeature(camera_id, local_track_id, (best.t - T0) // TICK_MS, best.t,
                                 best.vec.astype(np.float32), quality_score=0.8, orientation_bin=best.orientation)


class RawTiming:
    """StageTiming 과 같은 측정 + tick 단위 원본 표본(벤치용). 판정에 쓰지 않는다."""

    def __new__(cls):
        from simai_ai.tracking.stage_timing import StageTiming

        class _Raw(StageTiming):
            def __init__(self) -> None:
                super().__init__()
                self.raw: dict[str, list[float]] = {}

            def record(self, stage, people, wall_ms, cpu_ms=0):
                self.raw.setdefault(stage, []).append(wall_ms)
                super().record(stage, people, wall_ms, cpu_ms)

        return _Raw()


def _counter_randbits():
    state = {"n": 0}

    def randbits(k: int) -> int:
        state["n"] += 1
        digest = hashlib.sha256(f"663-{state['n']}".encode()).digest()
        return int.from_bytes(digest, "big") & ((1 << k) - 1)

    return randbits


def run_scene(scene: Scene, work_dir: Path, *, timing: bool = False, profile: Any = None,
              warmup: int = 0) -> dict[str, Any]:
    """장면을 처음부터 끝까지 판정 루프로 돌려 trace(와 선택적으로 단계별 시간)를 돌려준다.

    profile: cProfile.Profile 이면 warmup 뒤 tick 의 judge.tick 만 잰다.
    """
    import time

    import tier0_site as ts

    from services.tier0.judge import JudgmentLoop
    from services.tier0.perception import Perception
    from simai_ai.config import BundleVerifier, ConfigSync
    from simai_ai.eventlog import EventJournal, JournalWriter
    from simai_ai.risk import FixedClock, LifecycleTracker, Origin, default_registry
    from simai_ai.risk.event_id import EventIdFactory

    doc = geo.calibration_doc()
    rig = geo.make_rig()
    feed = SceneFeed(rig, scene)
    perception = Perception(rig, feed, session="eq663", scene_version="scene-663",
                            input_provenance={"source": "SIM", "is_upper_bound": True}, feature_lookup=feed.lookup)
    raw = RawTiming()
    perception.timing = raw
    associations: list[Any] = []
    # 판정에 쓰지 않는 관찰자 자리(보정 감시 표본 공급)에 연결 결과를 남긴다: 클러스터 구성·사유·경쟁 후보·쌍 근거
    perception.observe_association = associations.append
    clock = FixedClock(T0)
    message, fetcher = ts.bundle_message(doc, "cfg-663", T0 - 10, zones=[ZONE], rules=RULES)
    verifier = BundleVerifier(fetcher, default_registry(), lambda v: v == doc["calibration_version"], clock)
    sync = ConfigSync(verifier, clock, source_id=ts.GATEWAY, restored=verifier.verify(message), restored_applied_at=T0)
    journal = EventJournal.open(work_dir / scene.name, clock=clock, new_msg_id=_sequence("j"))
    writer = JournalWriter(journal)
    writer.start()
    tracker = LifecycleTracker(origin=Origin.REPLAY, gateway_id=ts.GATEWAY, clock=clock,
                               id_factory=EventIdFactory(clock, randbits=_counter_randbits()))
    judge = JudgmentLoop(lock=threading.RLock(), now_ms=clock.now_ms, config_sync=sync, perception=perception,
                         tracker=tracker, journal=journal, writer=writer, calibration_state=lambda: "PROVISIONAL",
                         new_msg_id=_sequence("m"))
    ticks: list[dict[str, Any]] = []
    tick_ms: list[float] = []
    people: list[int] = []
    try:
        for k in range(scene.ticks):
            clock.set(T0 + k * TICK_MS + 60)
            measuring = k >= warmup
            if profile is not None and measuring:
                profile.enable()
            start = time.perf_counter()
            result = judge.tick()
            elapsed = (time.perf_counter() - start) * 1000
            if profile is not None and measuring:
                profile.disable()
            if measuring:
                tick_ms.append(elapsed)
                people.append(perception.people)
            perceived = result.perceived
            assoc = associations.pop() if associations else None
            associations.clear()
            ticks.append({
                "k": k,
                "clusters": None if assoc is None else [
                    [c.association_id, list(c.keys), [r.value for r in c.uncertain_reasons], list(c.competitors)]
                    for c in assoc.clusters],
                "pairs": None if assoc is None else [
                    [list(e.a), list(e.b), e.reject.value if e.reject else None, e.cost, e.marginal, e.residual_px]
                    for e in assoc.pairs],
                # Re-ID 내부 판단(world_state 에는 없다): 갤러리 유사도·경쟁 margin·스왑 의심 기한
                "reid": {wid: [tr.last_reid_similarity, tr.last_reid_margin, tr.swap_suspected_until]
                         for wid, tr in sorted(perception.manager._tracks.items())},
                "evidence_ref": result.evidence_ref,
                "payload": None if perceived is None else perceived.payload,
                "revisions": [] if perceived is None else [
                    [r.kind.value, r.previous_world_id, r.world_id, r.identity_revision]
                    for r in perceived.frame.revisions],
                "transitions": [tr.to_payload() for step in result.steps for tr in step.transitions],
                "signals": [repr(s) for step in result.steps for s in step.local_signals],
            })
        trace = {"scene": scene.name, "ticks": ticks, "diag": dict(sorted(perception.manager.diag.items())),
                 "issued": list(perception.manager.issuer.issued)}
        out: dict[str, Any] = {"trace": trace}
        if timing:
            out["timing"] = {"judge_tick_ms": tick_ms, "people": people,
                             "stages": {s: v[warmup:] if len(v) == scene.ticks else v for s, v in raw.raw.items()}}
        return out
    finally:
        writer.stop(5)
        journal.close()


def _sequence(prefix: str):
    state = {"n": 0}

    def nxt() -> str:
        state["n"] += 1
        return f"{prefix}-{state['n']:08d}"

    return nxt


def trace_json(trace: dict[str, Any]) -> str:
    """trace 를 정규 JSON 으로(부동소수는 repr 왕복이라 비트 단위로 같으면 문자열도 같다)."""
    return json.dumps(trace, sort_keys=True, ensure_ascii=False, allow_nan=True, default=_default)


def _default(obj: Any) -> Any:
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return repr(obj)


def scenes_for_equivalence() -> list[Scene]:
    return [mixed(300), *(crowd(n, 200) for n in (3, 5, 7, 9))]


def main() -> int:
    """동치 비교용 CLI: 장면들의 trace 를 JSON 파일로 쓴다. 이전 코드와 비교할 때 PYTHONPATH 로 그 코드를 앞에 둔다."""
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenes", nargs="*", default=None, help="mixed crowd-3 ... (기본: 동치 시험 전체)")
    parser.add_argument("--ticks", type=int, default=None, help="장면 tick 수를 줄여 빨리 돌릴 때")
    args = parser.parse_args()
    scenes = scenes_for_equivalence()
    if args.scenes:
        scenes = [s for s in scenes if s.name in args.scenes]
    if args.ticks:
        for s in scenes:
            s.ticks = min(s.ticks, args.ticks)
    traces = {}
    with tempfile.TemporaryDirectory() as tmp:
        for s in scenes:
            traces[s.name] = json.loads(trace_json(run_scene(s, Path(tmp))["trace"]))
    import services.tier0.judge as judge_module
    import simai_ai

    args.output.write_text(json.dumps(traces, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"services": judge_module.__file__, "simai_ai": simai_ai.__file__}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
