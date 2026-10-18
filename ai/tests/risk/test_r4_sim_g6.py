"""G6-R4 지표를 시뮬레이터로 잰다 (명세 7.2 R4, 15.1·15.3 최소 표본, 부록 D-G6-02). 결과는 SIM 상한이며 판정이 아니다.

시뮬레이터(ai/sim)의 자세 키로 낙상과 정상 동작을 만들고, world_tracks 를 등록부 R4 와 기본 생명주기(발생 지속 조건 3프레임 중 2,
해소 3초, 재개 10초)에 넣어 정답(시나리오의 넘어짐 시각)과 비교한다.

- 낙상(정답): 트인 곳에서 앞으로 넘어짐(0.8초, 가속), 기절하듯 천천히 쓰러짐(1.5초), 40 cm 단상 위에서 넘어짐, 장 뒤로
  넘어져 머리가 가려짐(급하강 직후 관측 상실 경로). 넘어진 뒤 7초 누워 있다가 일어난다.
- 정상 동작(오경보 대조): 빠르게 의자에 앉기, 쪼그려 줍기, 허리 굽혀 줍기, 웅크림 유지, 빠르게 바닥에 앉기, 의도적으로 눕기,
  장 뒤에서 쪼그려 줍기(머리 가려짐), 단상 오르내리기(머리와 접지가 함께 내려감).
- 검출: 넘어짐 시작부터 8초 안에 그 사람의 R4 이벤트(RAISED 또는 재개 RESUMED)가 생김. 지연 = 이벤트의 관측 시각 − 넘어짐 시작.
- 오경보: 정상 동작 사람의 R4 이벤트, 그리고 낙상 사람의 검출 창 밖 R4 이벤트. 2시간당 건수는 정상 동작 사람·시간(동시에
  정상 동작하는 사람 수 × 경과 시간)으로 나눈다. 시나리오 경과 시간도 함께 적는다. 사람·시간은 15.3 의 '정상 영상 6시간'과
  다른 단위라 실영상 표본을 대신하지 않는다.
- G6-R4 기준(D-G6-02): 검출률 90% 이상, 오경보 2시간당 1회 이하(정상 영상 6시간 이상, 15.3). 기준을 넘으면
  `SIM_UPPER_BOUND_MET`(판정 아님, 실영상 G6 에서 판정), 못 넘으면 `FAILED`. 정상 동작 사람 시간이 6시간 미만이면 오경보율은
  표본 부족(`sample_short`)으로 함께 적는다. SIM 지표는 제품 성능으로 쓰지 않는다(AGENTS.md 규칙 7).

변형: 정답 머리 높이, 잡음(머리 σ 0.05 m·접지 Z σ 0.02 m, G2 p95 10 cm 수준), 잡음 + 표본 30% 의 머리 근거 UNCERTAIN(head_z
null, skeleton 귀·눈 UNCERTAIN → R4 가 귀·눈 + 오프셋으로 다시 만들고 JOINT_UNCERTAIN). 시뮬레이터 출력은 head_z_validity 가
없는 이전 버전 형식이라, 같은 변형을 world_state 1.4.0 형식(head_z_validity VALID·UNCERTAIN·UNAVAILABLE, 송신 측
tracking.world_state 와 같은 규칙)으로도 돌려 결과가 같은지 본다(S15P31A101-177). 기본 시험은 짧게(2주기) 돈다. 길게 재려면:

    SIMAI_G6R4_CYCLES=60 uv run pytest tests/risk/test_r4_sim_g6.py -s
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
    ConfidenceReason,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    WorldState,
    default_registry,
)
from simai_ai.risk.rules.r4_fall_suspect import REASON_LOST_AFTER_DROP

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = AI_DIR / "sim" / "scenarios" / "basic.json"
CYCLE_MS = 25_000  # 동작 1회(넘어짐 → 누움 7초 → 일어남 → 해소 3초 뒤에도 재개 10초보다 길게)
FALL_AT_MS = 3_000  # 주기 안 넘어짐(또는 정상 동작) 시작
DETECT_WINDOW_MS = 8_000
DETECTION_REQUIRED = 0.9  # D-G6-02
FALSE_PER_2H_LIMIT = 1.0  # D-G6-02
NORMAL_HOURS_REQUIRED = 6.0  # 15.3 최소 표본(R4 의 2시간당 1회는 6시간 이상)
CHUNK = 4  # 시뮬레이터 한 번에 넣는 주기 수
STATUS_MET = "SIM_UPPER_BOUND_MET"
STATUS_FAILED = "FAILED"
HALF_PI = math.pi / 2

# 단상(40 cm, 명세 7.4 교실 시험)과 머리를 가리는 장 두 개(높이 1.2 m, 벽에 붙음)
SURFACES = [
    {"id": "podium-1", "box_min": [2.5, 6.2, 0.0], "box_max": [6.5, 7.3, 0.4]},
    {"id": "cabinet-1", "box_min": [4.0, 0.0, 0.0], "box_max": [5.0, 0.7, 1.2]},
    {"id": "cabinet-2", "box_min": [7.6, 0.0, 0.0], "box_max": [8.6, 0.7, 1.2]},
]


def _fall(duration_ms: int) -> list[tuple[int, str, str]]:
    end = FALL_AT_MS + duration_ms
    return [
        (0, "stand", "linear"),
        (FALL_AT_MS, "stand", "linear"),
        (end, "lie", "in"),
        (end + 7000, "lie", "linear"),
        (end + 8500, "sit_floor", "linear"),
        (end + 10000, "stand", "linear"),
    ]


def _pick(posture: str, down_ms: int, hold_ms: int) -> list[tuple[int, str, str]]:
    keys = [(0, "stand", "linear")]
    for start in (FALL_AT_MS, FALL_AT_MS + 9000):
        keys += [
            (start, "stand", "linear"),
            (start + down_ms, posture, "linear"),
            (start + down_ms + hold_ms, posture, "linear"),
            (start + 2 * down_ms + hold_ms, "stand", "linear"),
        ]
    return keys


# 이름 → (낙상인지, 위치, 진행 방향(누울 때 머리 쪽), 키, 주기 안 자세 키 [(ms, 자세, 보간)])
ACTIVITIES: dict[str, tuple[bool, tuple[float, float], float, float, list[tuple[int, str, str]]]] = {
    "fall_open": (True, (1.5, 2.0), 0.0, 1.75, _fall(800)),
    "fall_faint": (True, (1.5, 4.0), 0.0, 1.62, _fall(1500)),
    "fall_podium": (True, (2.8, 6.75), 0.0, 1.70, _fall(800)),
    "fall_hidden": (True, (4.5, 2.2), -HALF_PI, 1.80, _fall(800)),
    "sit_fast": (False, (6.0, 2.0), HALF_PI, 1.70, [
        (0, "stand", "linear"), (FALL_AT_MS, "stand", "linear"), (FALL_AT_MS + 450, "sit", "linear"),
        (FALL_AT_MS + 6000, "sit", "linear"), (FALL_AT_MS + 6800, "stand", "linear"),
    ]),
    "squat_pick": (False, (7.5, 3.0), HALF_PI, 1.65, _pick("squat", 600, 1500)),
    "stoop_pick": (False, (6.0, 4.5), 0.0, 1.85, _pick("stoop", 500, 1000)),
    "crouch": (False, (7.5, 5.0), math.pi, 1.58, [
        (0, "stand", "linear"), (FALL_AT_MS, "stand", "linear"), (FALL_AT_MS + 700, "squat", "linear"),
        (FALL_AT_MS + 12000, "squat", "linear"), (FALL_AT_MS + 12800, "stand", "linear"),
    ]),
    "floor_sit_fast": (False, (4.0, 3.2), 0.0, 1.72, [
        (0, "stand", "linear"), (FALL_AT_MS, "stand", "linear"), (FALL_AT_MS + 900, "sit_floor", "linear"),
        (FALL_AT_MS + 9000, "sit_floor", "linear"), (FALL_AT_MS + 10500, "stand", "linear"),
    ]),
    "lie_down": (False, (1.5, 5.5), 0.0, 1.68, [
        (0, "stand", "linear"), (FALL_AT_MS, "stand", "linear"), (FALL_AT_MS + 1500, "sit_floor", "linear"),
        (FALL_AT_MS + 2500, "sit_floor", "linear"), (FALL_AT_MS + 4500, "lie", "linear"),
        (FALL_AT_MS + 13000, "lie", "linear"), (FALL_AT_MS + 14500, "sit_floor", "linear"),
        (FALL_AT_MS + 16000, "stand", "linear"),
    ]),
    "squat_hidden": (False, (8.1, 1.0), -HALF_PI, 1.75, _pick("squat", 600, 2000)),
}  # fmt: skip
STEPPER = "step_podium"  # 단상(0.4 m) 오르내리기. 자세 키 없이 걷는다


def scenario(cycles: int, seed: int) -> dict:
    base = json.loads(BASIC.read_text(encoding="utf-8"))
    people = []
    for name, (_, xy, heading, height, keys) in ACTIVITIES.items():
        postures = []
        for c in range(cycles):
            postures += [{"t_ms": c * CYCLE_MS + t, "posture": p, "ease": e} for t, p, e in keys]
        people.append(
            {
                "id": name,
                "height_m": height,
                "initial_heading_rad": heading,
                "waypoints": [{"t_ms": 0, "xy": list(xy)}],
                "postures": postures,
            }
        )
    steps = []
    for c in range(cycles):
        t = c * CYCLE_MS
        steps += [
            {"t_ms": t, "xy": [7.0, 5.7]},
            {"t_ms": t + 3000, "xy": [7.0, 5.7]},
            {"t_ms": t + 5000, "xy": [6.0, 6.8]},  # 단상 위
            {"t_ms": t + 9000, "xy": [6.0, 6.8]},
            {"t_ms": t + 11000, "xy": [7.0, 5.7]},
        ]
    people.append({"id": STEPPER, "height_m": 1.66, "waypoints": steps})
    return dict(
        base,
        name="r4-g6",
        description="R4 G6 측정: 낙상 4종 + 정상 동작(앉기·줍기·웅크림·눕기·단상 오르내리기)",
        seed=seed,
        duration_ms=cycles * CYCLE_MS,
        surfaces=SURFACES,
        people=people,
    )


def run_sim(tmp_path: Path, cycles: int, seed: int) -> Path:
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario(cycles, seed), ensure_ascii=False), encoding="utf-8")
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


@dataclass(frozen=True)
class G6R4Result:
    falls: dict[str, int]
    detected: dict[str, int]
    latencies_ms: tuple[int, ...]
    false_alarms: dict[str, int]
    normal_ms: int
    uncertain_events: int
    events: int
    lost_path_events: int = 0
    elapsed_ms: int = 0  # 시나리오 경과 시간(사람 수를 곱하지 않음)

    @classmethod
    def combine(cls, results: "list[G6R4Result]") -> "G6R4Result":
        def add(key: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for r in results:
                for k, v in getattr(r, key).items():
                    out[k] = out.get(k, 0) + v
            return out

        return cls(
            falls=add("falls"),
            detected=add("detected"),
            latencies_ms=tuple(v for r in results for v in r.latencies_ms),
            false_alarms=add("false_alarms"),
            normal_ms=sum(r.normal_ms for r in results),
            uncertain_events=sum(r.uncertain_events for r in results),
            events=sum(r.events for r in results),
            lost_path_events=sum(r.lost_path_events for r in results),
            elapsed_ms=sum(r.elapsed_ms for r in results),
        )

    @property
    def detection_rate(self) -> float:
        total = sum(self.falls.values())
        return sum(self.detected.values()) / total if total else math.nan

    @property
    def normal_hours(self) -> float:
        return self.normal_ms / 3_600_000

    @property
    def false_per_2h(self) -> float:
        return sum(self.false_alarms.values()) / (self.normal_hours / 2)

    @property
    def sample_short(self) -> bool:
        return self.normal_hours < NORMAL_HOURS_REQUIRED

    @property
    def status(self) -> str:
        ok = self.detection_rate >= DETECTION_REQUIRED and self.false_per_2h <= FALSE_PER_2H_LIMIT
        return STATUS_MET if ok else STATUS_FAILED

    def latency(self, q: float) -> float:
        values = sorted(self.latencies_ms)
        return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))] if values else math.nan


def _perturb(
    track: dict, rng: random.Random, head_sigma: float, anchor_sigma: float, uncertain_ratio: float, v14: bool = False
) -> dict:
    track = json.loads(json.dumps(track))
    made_uncertain = False
    if track["head_z"] is not None and head_sigma:
        track["head_z"] += rng.gauss(0.0, head_sigma)
    if track["ground_anchor_xyz"] is not None and anchor_sigma:
        track["ground_anchor_xyz"][2] += rng.gauss(0.0, anchor_sigma)
    skeleton = track.get("skeleton")
    if head_sigma and skeleton is not None:
        # 머리 근거 귀·눈 Z 에도 같은 크기 잡음(UNCERTAIN 경로가 다시 만드는 값)
        for j in (1, 2, 3, 4):
            if skeleton["joints"][j] is not None:
                skeleton["joints"][j][2] += rng.gauss(0.0, head_sigma)
    if uncertain_ratio and skeleton is not None and rng.random() < uncertain_ratio:
        # 관절 게이트가 머리 근거를 UNCERTAIN 으로 둔 표본: head_z 는 null(VALID 일 때만 실림), 귀·눈은 UNCERTAIN
        made_uncertain = track["head_z"] is not None
        track["head_z"] = None
        for j in (1, 2, 3, 4):
            if skeleton["validity"][j] == "VALID":
                skeleton["validity"][j] = "UNCERTAIN"
    if v14:
        # world_state 1.4.0: 값이 있으면 VALID, 게이트가 UNCERTAIN 귀·눈으로만 값을 만들었으면 UNCERTAIN(값 null), 좌표 있는
        # 귀·눈이 없으면 UNAVAILABLE(tracking.world_state._head_z 와 같은 규칙)
        if track["head_z"] is not None:
            track["head_z_validity"] = "VALID"
        else:
            track["head_z_validity"] = "UNCERTAIN" if made_uncertain else "UNAVAILABLE"
            track.pop("head_z_sigma", None)
    return track


def measure(
    out: Path, *, head_sigma: float = 0.0, anchor_sigma: float = 0.0, uncertain_ratio: float = 0.0, seed: int = 0,
    lag_ms: int = 200, v14: bool = False,
) -> G6R4Result:  # fmt: skip
    rng = random.Random(seed)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    names = {p["world_id"]: p["person_id"] for p in manifest["people"]} if "people" in manifest else {}
    rows = [json.loads(line) for line in (out / "world_tracks.jsonl").read_text(encoding="utf-8").splitlines()]
    if not names:
        order = [*ACTIVITIES, STEPPER]
        names = {f"w-{i + 1:04d}": n for i, n in enumerate(order)}
    start = rows[0]["t"]
    clock = FixedClock(start)
    engine = RiskEngine([default_registry().create("R4", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="sim", clock=clock)
    starts: dict[str, list[int]] = {}
    uncertain = 0
    lost_path = 0
    for row in rows:
        tracks = [_perturb(t, rng, head_sigma, anchor_sigma, uncertain_ratio, v14) for t in row["tracks"]]
        world = WorldState.from_payload(
            {"t_latest_global": row["t"], "calibration_version": row["calibration_version"], "scene_version": "sim",
             "tracks": tracks}
        )  # fmt: skip
        clock.set(row["t"] + lag_ms)
        step = tracker.apply(engine.evaluate(world, [], Origin.LIVE), world, config_version="sim")
        for t in step.transitions:
            if t.transition is Lifecycle.RAISED or t.resumed:
                (world_id,) = t.state.world_ids
                starts.setdefault(names[world_id], []).append(t.state.observation_time - start)
                # 머리 근거 상실로 난 발생(관측 상실 경로)은 늘 JOINT_UNCERTAIN 이라 따로 센다
                lost = REASON_LOST_AFTER_DROP in t.state.reasons
                uncertain += not lost and ConfidenceReason.JOINT_UNCERTAIN in t.state.confidence_reasons
                lost_path += lost
    cycles = round((rows[-1]["t"] - start) / CYCLE_MS)
    falls: dict[str, int] = {}
    detected: dict[str, int] = {}
    latencies: list[int] = []
    false_alarms: dict[str, int] = {}
    for name, (is_fall, *_rest) in [*ACTIVITIES.items(), (STEPPER, (False,))]:
        events = starts.get(name, [])
        if not is_fall:
            false_alarms[name] = len(events)
            continue
        used: set[int] = set()
        falls[name] = cycles
        detected[name] = 0
        for c in range(cycles):
            t_fall = c * CYCLE_MS + FALL_AT_MS
            hit = [e for e in events if t_fall <= e <= t_fall + DETECT_WINDOW_MS]
            if hit:
                detected[name] += 1
                latencies.append(min(hit) - t_fall)
                used.update(hit)
        false_alarms[name] = sum(1 for e in events if e not in used)
    normal_people = sum(1 for is_fall, *_ in ACTIVITIES.values() if not is_fall) + 1
    return G6R4Result(
        falls=falls,
        detected=detected,
        latencies_ms=tuple(latencies),
        false_alarms=false_alarms,
        normal_ms=normal_people * (rows[-1]["t"] - start + 100),
        uncertain_events=uncertain,
        events=sum(len(v) for v in starts.values()),
        lost_path_events=lost_path,
        elapsed_ms=rows[-1]["t"] - start + 100,
    )


def report(name: str, r: G6R4Result) -> str:
    per_fall = ", ".join(f"{k} {r.detected[k]}/{r.falls[k]}" for k in sorted(r.falls))
    per_false = ", ".join(f"{k} {v}" for k, v in sorted(r.false_alarms.items()) if v)
    sample = (
        f"시나리오 경과 {r.elapsed_ms / 3_600_000:.2f}시간, 정상 동작 사람·시간 {r.normal_hours:.2f}시간"
        + (" (6시간 미만: 오경보율 표본 부족. 실영상 정상 영상 6시간과는 다른 단위)" if r.sample_short else "")
    )
    return (
        f"{name}: 게이트 {r.status}(SIM 상한, 판정 아님). 낙상 {sum(r.falls.values())}회 검출률 {r.detection_rate:.1%} "
        f"(기준 {DETECTION_REQUIRED:.0%}; {per_fall}), 검출 지연 중앙 {r.latency(0.5):.0f} / p95 {r.latency(0.95):.0f} / "
        f"최대 {r.latency(1.0):.0f} ms, 오경보 {sum(r.false_alarms.values())}건 = 2시간당 {r.false_per_2h:.2f}회 "
        f"(기준 {FALSE_PER_2H_LIMIT:g}; {per_false or '없음'}), {sample}, "
        f"관측 상실 경로 이벤트 {r.lost_path_events}, 그 밖의 JOINT_UNCERTAIN 이벤트 {r.uncertain_events}/{r.events} [SIM 상한]"
    )


@pytest.fixture(scope="module")
def sim_outs(tmp_path_factory):
    total = int(os.environ.get("SIMAI_G6R4_CYCLES", "2"))
    outs = []
    for i in range(math.ceil(total / CHUNK)):
        n = min(CHUNK, total - i * CHUNK)
        outs.append((run_sim(tmp_path_factory.mktemp(f"r4-g6-{i}"), n, seed=96 + i), 96 + i))
    return outs


@pytest.mark.parametrize(
    ("label", "head_sigma", "anchor_sigma", "uncertain_ratio"),
    [("정답", 0.0, 0.0, 0.0), ("잡음", 0.05, 0.02, 0.0), ("잡음+UNCERTAIN 30%", 0.05, 0.02, 0.3)],
)
def test_g6_r4_on_simulator(sim_outs, label, head_sigma, anchor_sigma, uncertain_ratio):
    result = G6R4Result.combine(
        [
            measure(out, head_sigma=head_sigma, anchor_sigma=anchor_sigma, uncertain_ratio=uncertain_ratio, seed=seed)
            for out, seed in sim_outs
        ]
    )
    print(report(f"G6-R4 SIM({label})", result))
    assert sum(result.falls.values()) >= 8
    # SIM 상한에서도 검출 기준을 못 넘거나 정상 동작에 오경보가 나면 규칙이 틀렸다. 실영상 판정은 G6 검증 이슈에서 한다.
    assert result.status == STATUS_MET
    assert sum(result.false_alarms.values()) == 0
    if uncertain_ratio:
        assert result.uncertain_events > 0  # UNCERTAIN 머리 근거로 난 낙상 의심에는 확인 필요가 붙는다
    else:
        assert result.uncertain_events == 0


@pytest.mark.parametrize(
    ("head_sigma", "anchor_sigma", "uncertain_ratio"),
    [(0.0, 0.0, 0.0), (0.05, 0.02, 0.3)],
    ids=["정답", "잡음+UNCERTAIN 30%"],
)
def test_g6_r4_v14_head_z_validity_matches_legacy(sim_outs, head_sigma, anchor_sigma, uncertain_ratio):
    # 같은 SIM 입력을 world_state 1.4.0 형식(head_z_validity)으로 넣어도 결과가 이전 버전 형식과 같다(S15P31A101-177).
    # VALID 는 head_z, UNCERTAIN 은 skeleton UNCERTAIN 귀·눈 + 오프셋, UNAVAILABLE 은 모름으로 읽는다.
    def run_all(v14):
        return G6R4Result.combine(
            [
                measure(out, head_sigma=head_sigma, anchor_sigma=anchor_sigma, uncertain_ratio=uncertain_ratio,
                        seed=seed, v14=v14)
                for out, seed in sim_outs
            ]
        )  # fmt: skip

    new, legacy = run_all(True), run_all(False)
    print(report(f"G6-R4 SIM 1.4.0({'잡음+UNCERTAIN' if uncertain_ratio else '정답'})", new))
    assert new == legacy
    assert new.status == STATUS_MET and sum(new.false_alarms.values()) == 0
