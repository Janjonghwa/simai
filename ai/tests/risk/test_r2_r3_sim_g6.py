"""G6 R2·R3 검출률을 시뮬레이터 40 cm 단상으로 잰다 (명세 15.1 G6 'R2·R3 검출률(40cm 단상, 등록 작업면) 95% 이상(60회 이상)',
7.2 R2·R3, 7.4 교실 시험 프로파일, 부록 E 시연 4). **결과는 SIM 상한이고 G6 판정이 아니다.**

입력은 시뮬레이터 `observations/`·`calibration.json` 만 쓰고 Tier 0 하류 전체를 돈다(tests/contact/sim_contact.py 와 같은 조립):
LocalTrackletBuffer → CrossViewAssociator → WorldTrackManager → ContactEstimator(등록 작업면 = 시나리오 단상) → world_state
→ RiskEngine(R2·R3 교실 시험 프로파일 v1001, 같은 작업면) → LifecycleTracker(LIVE, 기본 정책: 3프레임 중 2, 해소 3초, 재개 10초).

시나리오: 내장 podium 의 단상(가로 4.0 × 세로 1.1 m)을 0.4 m 로 바꾸고 두 사람을 둔다.
- climber: 시도마다 바닥에서 단상에 올라 3초 서 있다 내려온다(세 x 위치의 앞변, 네 번째마다 왼쪽 옆변). 단상 위는 어디든 가장자리
  0.55 m 안이라 단상 위에 있는 동안이 R2·R3 정답 구간이다.
- floorwalker: 단상 오른쪽 옆변 0.5 m 밖 바닥(범위 0.4 m 는 footprint 에 닿지 않음)과 앞쪽 바닥을 오간다. 바닥 사람은 R2·R3 가
  아니므로 이벤트가 나면 모두 오경보다.
변형: podium40(그대로), occluded(단상 위에 서 있는 동안 1.5초씩 cam1 에만 보이게 가림: ContactEstimator 가 마지막 지지 표면을 유지하는
HELD 경로).

지표:
- 검출: 정답 구간 [올라선 tick − 1초, 내려온 tick + 0.5초] 안에 그 사람의 R2(R3) 이벤트가 활성이 됨(RAISED 또는 재개 RESUMED).
  검출률 = 검출 시도 / 시도. 기준 95%(G6). 지연 = 이벤트 관측 시각 − 올라선 tick.
- 단상 위 해소: 정답이 단상 위인데 RESOLVED 가 난 수(모르는 상태로 해소되지 않는지). 목표 0.
- 오경보: floorwalker 이벤트 수, climber 의 정답 구간 밖 이벤트 수.
- 확인 필요 발생: 발생·재개 때 confidence_reasons 가 비지 않은 수(측정·기록).
- 이전 이벤트 미해소: 시도 1초 전에 그 사람의 R2(R3) 이벤트가 아직 활성인 수(측정·기록). 추적이 같은 사람에게 중복 World ID 를 잠깐
  만들면 그 ID 의 이벤트는 트랙이 사라져 해소를 확인할 수 없어 활성으로 남는다(lifecycle 6.4: 관측 상실은 자동 해소하지 않음).
  그런 이벤트가 하나 남으면 그 뒤 시도마다 다시 센다. 규칙이 아니라 추적·ID 교정의 몫이라 기준을 두지 않는다.
- 유지(HELD) 판정 tick: 단상 위 사람의 판정이 '가려진 동안 유지한 지지 표면'(standing_surface_held)으로 난 tick 수. occluded 변형이
  그 경로를 실제로 지나는지 확인한다.
게이트 상태는 R2·R3 모두 기준을 넘으면 `SIM_UPPER_BOUND_MET`(판정 아님, 실영상 G6 에서 판정), 못 넘으면 `FAILED`(미통과)다.
기본 시험은 짧게(시도 8회) 돈다. 판정 표본(60회 이상)으로 재려면:

    SIMAI_G6R2R3_TRIALS=60 uv run pytest tests/risk/test_r2_r3_sim_g6.py -s
"""

import copy
import importlib.util
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pytest

from simai_ai.contact import ContactEstimator, SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.risk import (
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    WorldState,
    default_registry,
    work_surfaces_from_operational_geometry,
)
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalTrackletBuffer
from simai_ai.tracking.world_state import world_state_payload
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackManager

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
CYCLE_MS = 16_000
WALK_MPS = 0.8
STAND_ON_MS = 3000
HIDE_FROM_MS, HIDE_MS = 800, 1500  # occluded: 올라선 뒤 0.8초부터 1.5초 동안 cam1 에만 보임
ARRIVAL_LAG_MS = 60
DECISION_LAG_MS = 100
PRE_WINDOW_MS, POST_WINDOW_MS = 1000, 500
PASS_RATIO = 0.95  # G6 R2·R3 검출률
MIN_TRIALS_FOR_GATE = 60  # 15.1 최소 표본
STATUS_MET = "SIM_UPPER_BOUND_MET"
STATUS_FAILED = "FAILED"
RULES = (("R2", 1001), ("R3", 1001))
FRONT_XS = (3.2, 4.5, 5.8)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sim_contact = _load("sim_contact_for_r2r3", AI_DIR / "tests" / "contact" / "sim_contact.py")
wt, xv = sim_contact.wt, sim_contact.xv


def _ms(a, b):
    return round(math.dist(a, b) / WALK_MPS * 1000)


def scenario(trials: int, seed: int) -> tuple[dict, list[tuple[int, int]]]:
    """시나리오 문서와 occluded 변형의 가림 구간(시작·끝 ms, 시나리오 시작 기준)."""
    doc = copy.deepcopy(sim_contact.scenario_doc())
    doc["name"] = f"r2r3-g6-{seed}"
    doc["description"] = "G6 R2·R3: 0.4 m 단상 오르내리기 반복 + 단상 옆 바닥 사람"
    doc["seed"] = seed
    doc["duration_ms"] = trials * CYCLE_MS
    climber, walker, hides = [], [], []
    for i in range(trials):
        t = i * CYCLE_MS
        # 시도마다 앞변 1.2 m 앞 바닥(y = 5.0)의 출발점에서 시작해 돌아온다. 출발점 사이 이동도 y = 5.0 위라 단상에 닿지 않는다.
        if i % 4 == 3:
            home = (1.3, 5.0)
            path = [(1.3, 6.75), (3.2, 6.75)]  # 왼쪽 옆변(x = 2.5)으로 올라 0.7 m 안쪽
        else:
            home = (FRONT_XS[i % 3], 5.0)
            path = [(home[0], 6.75)]  # 앞변(y = 6.2)으로 올라 가운데(앞·뒤 변 0.55 m)
        points = [home, *path]
        times = [t + 2000]
        for a, b in pairwise(points):
            times.append(times[-1] + _ms(a, b))
        up = times[-1]
        down = up + STAND_ON_MS
        back = [down]
        rev = points[::-1]
        for a, b in pairwise(rev):
            back.append(back[-1] + _ms(a, b))
        climber += [{"t_ms": t, "xy": list(home)}]
        climber += [{"t_ms": tt, "xy": list(pt)} for tt, pt in zip(times, points, strict=True)]
        climber += [{"t_ms": tt, "xy": list(pt)} for tt, pt in zip(back, rev, strict=True)]
        hides.append((up + HIDE_FROM_MS, up + HIDE_FROM_MS + HIDE_MS))
        beside, away = (7.0, 6.75), (8.2, 5.4)
        go = t + 3000 + _ms(away, beside)
        walker += [
            {"t_ms": t, "xy": list(away)},
            {"t_ms": t + 3000, "xy": list(away)},
            {"t_ms": go, "xy": list(beside)},
            {"t_ms": go + 4000, "xy": list(beside)},
            {"t_ms": go + 4000 + _ms(beside, away), "xy": list(away)},
        ]
    doc["people"] = [
        {"id": "climber", "height_m": 1.72, "waypoints": climber},
        {"id": "floorwalker", "height_m": 1.66, "waypoints": walker},
    ]
    return doc, hides


@dataclass
class G6R2R3Result:
    variant: str
    trials: int = 0
    detected: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    latencies_ms: dict[str, list[int]] = field(default_factory=lambda: {"R2": [], "R3": []})
    on_podium_resolved: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    stale_active: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    false_events: Counter = field(default_factory=Counter)  # (사람, 규칙)
    needs_check_raises: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    raises: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    held_ticks: dict[str, int] = field(default_factory=lambda: {"R2": 0, "R3": 0})
    input_provenance: dict | None = None

    def merge(self, other: "G6R2R3Result") -> None:
        self.trials += other.trials
        for r in ("R2", "R3"):
            self.detected[r] += other.detected[r]
            self.latencies_ms[r] += other.latencies_ms[r]
            self.on_podium_resolved[r] += other.on_podium_resolved[r]
            self.stale_active[r] += other.stale_active[r]
            self.needs_check_raises[r] += other.needs_check_raises[r]
            self.raises[r] += other.raises[r]
            self.held_ticks[r] += other.held_ticks[r]
        self.false_events.update(other.false_events)
        self.input_provenance = self.input_provenance or other.input_provenance

    def ratio(self, rule: str) -> float:
        return self.detected[rule] / self.trials if self.trials else math.nan

    @property
    def status(self) -> str:
        return STATUS_MET if all(self.ratio(r) >= PASS_RATIO for r in ("R2", "R3")) else STATUS_FAILED

    def quantile(self, rule: str, q: float) -> float:
        values = sorted(self.latencies_ms[rule])
        if not values:
            return math.nan
        return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]


def measure(out: Path, hides: list[tuple[int, int]] | None, variant: str) -> G6R2R3Result:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    frames, truth = xv.load_frames(out, "stable")
    for start, end in hides or ():
        frames = wt.hide(frames, truth, "climber", start, end, ("cam1",))
    geometry = sim_contact.operational_geometry(sim_contact.scenario_doc())
    surfaces = SurfaceSet.from_operational_geometry(geometry)
    work_surfaces = work_surfaces_from_operational_geometry(geometry).surfaces
    truth_rows = {
        row["t"]: {p["person_id"]: p for p in row["people"]} for row in xv._jsonl(out / "truth" / "world_truth.jsonl")
    }
    provenance = build_input_provenance(
        {"source": "SIM", "camera_ids": sorted(rig.cameras)}, calibration=rig.calibration_provenance
    )
    associator = CrossViewAssociator(rig)
    buffer = LocalTrackletBuffer()
    manager = WorldTrackManager(WorldIdIssuer("sim1"), input_provenance=provenance)
    estimator = ContactEstimator()
    registry = default_registry()
    ticks = sorted(truth_rows)
    clock = FixedClock(ticks[0])
    engine = RiskEngine([registry.create(r, v) for r, v in RULES], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="sim", clock=clock)

    track_person: dict[str, str] = {}
    held_ticks: Counter = Counter()
    starts: list[tuple[int, str, str, bool]] = []  # (관측 시각, 규칙, 사람, 확인 필요)
    resolved: list[tuple[int, str, str]] = []
    active_at: dict[int, set[tuple[str, str]]] = {}
    latest = None
    i = 0
    for t in ticks:
        while i < len(frames) and frames[i].estimated_capture_time_ms <= t + ARRIVAL_LAG_MS:
            buffer.add(frames[i])
            latest = max(latest or 0, frames[i].estimated_capture_time_ms)
            i += 1
        views, _ = buffer.views_at(t)
        assoc = associator.associate(t, views)
        buffer.prune(t)
        result = manager.update(assoc, rig)
        contact_frame = estimator.update(result, surfaces)
        t_latest = max(latest or t, t)
        payload = world_state_payload(result, t_latest, "scene-sim", contacts=contact_frame.contacts())
        clusters = {c.association_id: c for c in assoc.clusters}
        for tr in result.tracks:
            if tr.association_id is not None:
                votes = Counter(truth[v.key] for v in clusters[tr.association_id].views).most_common()
                if len(votes) == 1 or votes[0][1] > votes[1][1]:
                    track_person[tr.world_id] = votes[0][0]
        world = WorldState.from_payload(payload)
        clock.set(t_latest + DECISION_LAG_MS)
        evaluation = engine.evaluate(world, [], Origin.LIVE, work_surfaces=work_surfaces)
        for f in evaluation.findings:
            if "standing_surface_held" in f.reasons and track_person.get(f.world_ids[0]) == "climber":
                held_ticks[f.rule_id] += 1
        step = tracker.apply(evaluation, world, config_version="sim")
        for tran in step.transitions:
            state = tran.state
            person = track_person.get(state.world_ids[0], "?")
            if tran.transition is Lifecycle.RAISED or tran.resumed:
                starts.append((state.observation_time, state.rule_id, person, state.needs_check))
            elif tran.transition is Lifecycle.RESOLVED:
                resolved.append((state.observation_time, state.rule_id, person))
        active_at[t] = {(s.rule_id, track_person.get(s.world_ids[0], "?")) for s in tracker.active()}

    # 정답 구간: climber 가 단상 위인 tick 의 연속 구간
    intervals: list[tuple[int, int]] = []
    on_since = last_on = None
    for t in ticks:
        on = truth_rows[t]["climber"]["support_surface_id"] != "floor"
        if on and on_since is None:
            on_since = t
        if on:
            last_on = t
        if not on and on_since is not None:
            intervals.append((on_since, last_on))
            on_since = None
    if on_since is not None:
        intervals.append((on_since, last_on))

    res = G6R2R3Result(variant=variant, trials=len(intervals), input_provenance=provenance)
    res.held_ticks = {r: held_ticks[r] for r in ("R2", "R3")}
    used: set[int] = set()
    for t_on, t_off in intervals:
        before = max((t for t in ticks if t <= t_on - PRE_WINDOW_MS), default=None)
        for rule in ("R2", "R3"):
            if before is not None and (rule, "climber") in active_at[before]:
                res.stale_active[rule] += 1
            hits = [
                k
                for k, (ts, r, p, _) in enumerate(starts)
                if r == rule and p == "climber" and t_on - PRE_WINDOW_MS <= ts <= t_off + POST_WINDOW_MS
            ]
            if hits:
                res.detected[rule] += 1
                res.latencies_ms[rule].append(starts[hits[0]][0] - t_on)
                used.update(hits)
            res.on_podium_resolved[rule] += sum(
                1 for ts, r, p in resolved if r == rule and p == "climber" and t_on <= ts <= t_off
            )
    for k, (_, rule, person, needs_check) in enumerate(starts):
        res.raises[rule] += 1
        res.needs_check_raises[rule] += int(needs_check)
        if k not in used:
            res.false_events[(person, rule)] += 1
    return res


def report(r: G6R2R3Result) -> str:
    parts = []
    for rule in ("R2", "R3"):
        parts.append(
            f"{rule} 검출 {r.detected[rule]}/{r.trials} ({r.ratio(rule):.1%}, 기준 {PASS_RATIO:.0%}), 지연 중앙 "
            f"{r.quantile(rule, 0.5):.0f} / p95 {r.quantile(rule, 0.95):.0f} / 최대 {r.quantile(rule, 1.0):.0f} ms, 단상 위 해소 "
            f"{r.on_podium_resolved[rule]}, 이전 이벤트 미해소 {r.stale_active[rule]}, 확인 필요 발생 "
            f"{r.needs_check_raises[rule]}/{r.raises[rule]}, 유지(HELD) 판정 tick {r.held_ticks[rule]}"
        )
    false = ", ".join(f"{p}/{rule} {n}" for (p, rule), n in sorted(r.false_events.items())) or "0"
    sample = "" if r.trials >= MIN_TRIALS_FOR_GATE else f", 표본 {r.trials}회 < {MIN_TRIALS_FOR_GATE}회(판정 표본 아님)"
    return (
        f"G6 R2·R3 SIM [{r.variant}]: 게이트 {r.status}(SIM 상한, 판정 아님){sample}. "
        + "; ".join(parts)
        + f"; 오경보(사람/규칙) {false} [SIM 상한, is_upper_bound={r.input_provenance['is_upper_bound']}]"
    )


@pytest.fixture(scope="module")
def sim_outs(tmp_path_factory):
    total = int(os.environ.get("SIMAI_G6R2R3_TRIALS", "8"))
    chunk = 10
    outs = []
    for k in range(math.ceil(total / chunk)):
        n = min(chunk, total - k * chunk)
        doc, hides = scenario(n, seed=93 + k)
        outs.append((xv.run_sim(doc, 93 + k, tmp_path_factory.mktemp(f"r2r3-g6-{k}")), hides))
    return outs


@pytest.mark.parametrize("variant", ["podium40", "occluded"])
def test_g6_r2_r3_on_simulator(sim_outs, variant):
    total = G6R2R3Result(variant=variant)
    for out, hides in sim_outs:
        total.merge(measure(out, hides if variant == "occluded" else None, variant))
    print(report(total))
    assert total.input_provenance["is_upper_bound"] is True  # SIM 상한(제품 성능 아님, AGENTS 규칙 7)
    assert total.trials >= 8
    # SIM 상한에서도 기준을 못 넘으면 규칙·조립이 틀렸다. 실영상 판정은 G6 검증 이슈에서 한다.
    assert total.status == STATUS_MET
    assert total.on_podium_resolved == {"R2": 0, "R3": 0}  # 단상 위에서 모르는 상태로 해소되지 않는다
    if variant == "occluded":
        assert min(total.held_ticks.values()) > 0  # 가려진 동안 유지(HELD) 경로를 실제로 지났다
    assert sum(n for (p, _), n in total.false_events.items() if p == "floorwalker") == 0  # 바닥 사람은 R2·R3 아님
