"""G6-R1 지표를 시뮬레이터로 잰다 (명세 15.1 G6, 부록 D-G6-01, FR-66). 결과는 SIM 상한이다.

시뮬레이터(ai/sim)가 낸 world_tracks(Tier 0 출력과 같은 의미로 가린 SIM 트랙)를 등록부 R1(+R6)·생명주기에 넣고,
정답(truth/world_truth.jsonl)의 구역 진입 구간과 비교한다.

- 진입(정답): 정답 위치(position_xy)가 구역 다각형 안에 든 연속 구간.
- 검출률: 진입 구간과 겹쳐 활성인 R1 이벤트가 있는 진입의 비율. 이벤트 하나는 진입 하나에만 센다(해소되지 않고 이어진
  이벤트가 다음 진입까지 덮으면 그 진입은 '같은 이벤트로 덮임'으로 따로 세고 검출에 넣지 않는다).
- 진입 → 이벤트(규칙 단계): RAISED(또는 재개 RESUMED)의 관측 시각 − 정답 진입 시각. 기본 생명주기 정책(발생 지속 조건
  3프레임 중 2, 10초 재개, S15P31A101-156)을 쓴다. 점유 반경 0.3 m 때문에 진입 전에 나면 음수다.
  촬영 → 판정·현장 경보 지연(glass-to-alarm, 6.6)은 들어 있지 않다.
- 오경보: 그 사람의 정답 진입 구간과 전혀 겹치지 않은 이벤트. 정상 동작(구역 0.75 m 옆을 지나감)도 섞는다.
- 깜빡임: 같은 사람의 이벤트가 해소된 뒤 10초 안에 새 이벤트(RAISED)가 난 경우
  (G6 '해소 후 10초 안 재발생 새 이벤트 0건').
  재개(RESUMED)는 따로 센다(같은 이벤트라 깜빡임이 아니다).

입력이 완벽할 때의 상한(`input_provenance.is_upper_bound = true`)이라 제품 성능으로 쓰지 않는다(AGENTS.md 규칙 7).
게이트 판정은 늘 `UNDETERMINED`(판정 불가)로 출력한다: SIM 상한, 정상 영상 3시간 표본이 아님, 지연에 glass-to-alarm 이
빠짐. 실영상 G6 측정은 run_record 와 함께 G6 검증 이슈에서 한다.
기본 시험은 짧은 시나리오(진입 8회)로 돈다. 판정 표본(진입 150회 이상)으로 재려면:

    SIMAI_G6_ENTRIES=150 uv run pytest tests/risk/test_r1_sim_g6.py -s
"""

import json
import math
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from simai_ai.risk import (
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    WorldState,
    Zone,
    default_registry,
)
from simai_ai.risk.zone_geometry import point_in_polygon

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = AI_DIR / "sim" / "scenarios" / "basic.json"
ZONE_POLYGON = [[4.0, 3.0], [5.5, 3.0], [5.5, 4.5], [4.0, 4.5]]
ZONE_CENTER = (4.75, 3.75)
REOPEN_WINDOW_MS = 10_000
GATE_STATUS = "UNDETERMINED"
GATE_STATUS_REASONS = (
    "SIM 상한(is_upper_bound)",
    "정상 영상 3시간 표본 아님",
    "지연에 glass-to-alarm 제외",
)
CHUNK_ENTRIES = 10  # 시뮬레이터 한 번에 넣는 진입 수(경유점이 많으면 시뮬레이터가 느려져 나눠 돌린다)
CYCLE_MS = 18_000  # 진입 1회: 들어가기 2.5초, 안 1초, 나오기 2.5초, 밖 12초(해소 3초 + 해소 후 10초 창보다 길게)


def scenario(entries: int, seed: int) -> dict:
    """기본 교실·카메라 4대에 구역 진입을 반복하는 사람과 구역 옆을 지나가는 사람을 둔다."""
    base = json.loads(BASIC.read_text(encoding="utf-8"))
    # 구역 왼쪽 변(x=4.0)으로 세 각도에서 번갈아 진입한다. 출발점끼리 옮겨 다닐 때도 구역에서 1.8 m 넘게 떨어져 있다.
    approach = [(-2.75, 0.0), (-2.55, -1.15), (-2.55, 1.15)]
    waypoints = []
    for i in range(entries):
        dx, dy = approach[i % len(approach)]
        outside = [round(ZONE_CENTER[0] + dx, 3), round(ZONE_CENTER[1] + dy, 3)]
        t = i * CYCLE_MS
        waypoints += [
            {"t_ms": t, "xy": outside},
            {"t_ms": t + 2500, "xy": list(ZONE_CENTER)},
            {"t_ms": t + 3500, "xy": list(ZONE_CENTER)},
            {"t_ms": t + 6000, "xy": outside},
        ]
    duration = entries * CYCLE_MS
    # 정상 동작: 구역 아래 변(y=3.0)에서 0.75 m 떨어진 선을 왕복한다(구역에 들어가지 않음)
    passer = []
    for i in range(math.ceil(duration / 8000) + 1):
        passer.append({"t_ms": i * 8000, "xy": [2.5 if i % 2 == 0 else 7.0, 2.25]})
    return dict(
        base,
        name="r1-g6",
        description="R1 G6 측정: 구역 진입 반복 + 구역 옆 통과",
        seed=seed,
        duration_ms=duration,
        people=[
            {"id": "entrant", "height_m": 1.72, "waypoints": waypoints},
            {"id": "passer", "height_m": 1.65, "waypoints": passer},
        ],
    )


@dataclass(frozen=True)
class G6Result:
    entries: int
    detected: int
    latencies_ms: tuple[int, ...]
    false_alarms: int
    flickers: int
    duration_ms: int
    covered_by_same_event: int = 0
    resumed: int = 0

    @classmethod
    def combine(cls, results: "list[G6Result]") -> "G6Result":
        return cls(
            entries=sum(r.entries for r in results),
            detected=sum(r.detected for r in results),
            latencies_ms=tuple(v for r in results for v in r.latencies_ms),
            false_alarms=sum(r.false_alarms for r in results),
            flickers=sum(r.flickers for r in results),
            duration_ms=sum(r.duration_ms for r in results),
            covered_by_same_event=sum(r.covered_by_same_event for r in results),
            resumed=sum(r.resumed for r in results),
        )

    @property
    def detection_rate(self) -> float:
        return self.detected / self.entries if self.entries else math.nan

    @property
    def latency_p95_ms(self) -> float:
        values = sorted(self.latencies_ms)
        return values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)] if values else math.nan

    @property
    def false_alarms_per_hour(self) -> float:
        return self.false_alarms / (self.duration_ms / 3_600_000)

    @property
    def flickers_per_minute(self) -> float:
        return self.flickers / (self.duration_ms / 60_000)


def measure(out: Path, noise_sigma_m: float, seed: int) -> G6Result:
    """시뮬레이터 출력 디렉터리 out 에서 G6-R1 지표를 잰다. noise_sigma_m 은 수평 위치에 더하는 잡음(σ)이다."""
    rng = random.Random(seed)
    zone = Zone.from_json(
        {
            "zone_id": "z-g6",
            "zone_version": "zone-1",
            "judge_type": "GROUND_FOOTPRINT",
            "polygon_xy": ZONE_POLYGON,
            "z_range": None,
            "severity": "HIGH",
        }
    )
    rows = [json.loads(line) for line in (out / "world_tracks.jsonl").read_text(encoding="utf-8").splitlines()]
    truth = [
        json.loads(line) for line in (out / "truth" / "world_truth.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    clock = FixedClock(rows[0]["t"])
    engine = RiskEngine([default_registry().create("R1", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="sim", clock=clock)
    # world_id → [[시작(RAISED·RESUMED), 해소, 새 이벤트인지], …]. 재개는 같은 이벤트의 새 활성 구간이다.
    events: dict[str, list[list]] = {}
    by_event: dict[str, list] = {}
    resumed = 0
    for row in rows:
        tracks = []
        for track in row["tracks"]:
            track = dict(track)
            dx, dy = rng.gauss(0.0, noise_sigma_m), rng.gauss(0.0, noise_sigma_m)
            track["position_xy"] = [track["position_xy"][0] + dx, track["position_xy"][1] + dy]
            if track["ground_anchor_xyz"] is not None:
                ax, ay, az = track["ground_anchor_xyz"]
                track["ground_anchor_xyz"] = [ax + dx, ay + dy, az]
            track["position_xy_sigma"] = [noise_sigma_m, noise_sigma_m]
            tracks.append(track)
        world = WorldState.from_payload(
            {"t_latest_global": row["t"], "calibration_version": row["calibration_version"], "scene_version": "sim",
             "tracks": tracks}
        )  # fmt: skip
        clock.set(row["t"])
        step = tracker.apply(engine.evaluate(world, [zone], Origin.LIVE), world, config_version="sim")
        for t in step.transitions:
            (world_id,) = t.state.world_ids
            if t.transition is Lifecycle.RAISED or t.resumed:
                interval = [t.state.observation_time, None, t.transition is Lifecycle.RAISED]
                events.setdefault(world_id, []).append(interval)
                by_event[t.event_id] = interval
                resumed += t.resumed
            elif t.transition is Lifecycle.RESOLVED:
                by_event[t.event_id][1] = t.state.observation_time

    end = rows[-1]["t"]
    episodes: dict[str, list[tuple[int, int]]] = {}
    inside_since: dict[str, int | None] = {}
    for tick in truth:
        for person in tick["people"]:
            wid, inside = person["world_id"], point_in_polygon(person["position_xy"], ZONE_POLYGON)
            since = inside_since.get(wid)
            if inside and since is None:
                inside_since[wid] = tick["t"]
            elif not inside and since is not None:
                episodes.setdefault(wid, []).append((since, tick["t"]))
                inside_since[wid] = None
    for wid, since in inside_since.items():
        if since is not None:
            episodes.setdefault(wid, []).append((since, end))

    def overlaps(interval, episode):
        raised, resolved, _ = interval
        return raised <= episode[1] and (resolved if resolved is not None else end) >= episode[0]

    entries = detected = false_alarms = flickers = covered = 0
    latencies = []
    for wid in sorted(set(episodes) | set(events)):
        person_events = events.get(wid, [])
        credited: set[int] = set()  # 이미 앞 진입의 검출로 센 이벤트(목록 위치)
        for episode in episodes.get(wid, []):
            entries += 1
            matched = [i for i, e in enumerate(person_events) if overlaps(e, episode)]
            fresh = [i for i in matched if i not in credited]
            if fresh:
                detected += 1
                credited.update(fresh)
                latencies.append(min(person_events[i][0] for i in fresh) - episode[0])
            elif matched:
                covered += 1
        false_alarms += sum(not any(overlaps(e, ep) for ep in episodes.get(wid, [])) for e in person_events)
        for previous, current in zip(person_events, person_events[1:], strict=False):
            if current[2] and previous[1] is not None and current[0] - previous[1] <= REOPEN_WINDOW_MS:
                flickers += 1
    return G6Result(entries, detected, tuple(latencies), false_alarms, flickers, end - rows[0]["t"], covered, resumed)


def run_sim(tmp_path: Path, entries: int, seed: int) -> Path:
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario(entries, seed), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(spec), "--out", str(out)],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert done.returncode == 0, done.stderr
    return out


def report(name: str, r: G6Result) -> str:
    return (
        f"{name}: 게이트 {GATE_STATUS}({', '.join(GATE_STATUS_REASONS)}). "
        f"진입 {r.entries}회, 검출 {r.detected} ({r.detection_rate:.1%}), "
        f"같은 이벤트로 덮임 {r.covered_by_same_event}, "
        f"진입→이벤트(규칙 단계) p95 {r.latency_p95_ms:.0f} ms (최소 {min(r.latencies_ms, default=math.nan):.0f}, "
        f"최대 {max(r.latencies_ms, default=math.nan):.0f}), "
        f"오경보 {r.false_alarms}건 ({r.false_alarms_per_hour:.2f}/h), "
        f"깜빡임 {r.flickers}건 ({r.flickers_per_minute:.2f}/분), 재개 {r.resumed}건, "
        f"길이 {r.duration_ms / 60000:.1f}분 [SIM 상한]"
    )


@pytest.fixture(scope="module")
def sim_outs(tmp_path_factory):
    """진입 수(SIMAI_G6_ENTRIES, 기본 8)를 CHUNK_ENTRIES 씩 시드를 바꿔 나눠 돌린 출력 디렉터리들."""
    total = int(os.environ.get("SIMAI_G6_ENTRIES", "8"))
    outs = []
    for i in range(math.ceil(total / CHUNK_ENTRIES)):
        n = min(CHUNK_ENTRIES, total - i * CHUNK_ENTRIES)
        outs.append((run_sim(tmp_path_factory.mktemp(f"r1-g6-{i}"), n, seed=71 + i), 71 + i))
    return outs


@pytest.mark.parametrize("noise_sigma_m", [0.0, 0.05])
def test_g6_r1_on_simulator(sim_outs, noise_sigma_m):
    result = G6Result.combine([measure(out, noise_sigma_m, seed=seed) for out, seed in sim_outs])
    print(report(f"G6-R1 SIM(잡음 σ {noise_sigma_m} m)", result))
    assert result.entries >= 8
    # SIM 상한에서 이것도 못 넘으면 규칙이 틀렸다. 실제 판정 표본·실영상 판정은 G6 에서 따로 한다.
    assert result.detection_rate == 1.0
    assert result.covered_by_same_event == 0
    assert result.false_alarms == 0
    assert result.flickers == 0
    assert result.latency_p95_ms <= 900
