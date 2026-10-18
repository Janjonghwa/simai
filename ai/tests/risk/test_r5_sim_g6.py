"""G6-R5 지표를 시뮬레이터로 잰다 (명세 7.2 R5 설명, 15.1 G6, 부록 D-G6-03, FR-70). 결과는 SIM 상한이다.

시뮬레이터(ai/sim)가 낸 world_tracks 를 등록부 R1·R5 와 기본 생명주기(발생 지속 조건 3프레임 중 2, 해소 3초, 재개 10초)에
넣고 정답(truth/world_truth.jsonl)의 구역 진입과 비교한다.

- 실제 진입(정답): 정답 위치(position_xy)가 구역 다각형 안에 처음 든 시각.
- 선행 시간: 실제 진입 시각 − R5 이벤트 생성 시각(RAISED·재개 RESUMED 전이의 주입 시계 시각 occurred_at). 명세 R5 설명대로
  이벤트 생성 시점 기준이다. 판정 시계는 관측 시각 + lag_ms(파이프라인 지연 예시)로 둔다. 진입마다 그 접근 구간(앞 진입이
  끝난 뒤 ~ 진입)에서 가장 이른 R5 이벤트를 쓰고, 없으면 놓침(선행 시간 없음, 1초 미만으로 센다).
- G6-R5 기준: 접근의 90% 이상에서 선행 시간 1초 이상(D-G6-03).
- 오예측: 이벤트 생성 뒤 horizon 의 두 배(4초) 안에 그 사람의 실제 진입이 시작되지 않은 R5 이벤트. 사람 유형별로 센다.
  '멈춤'(구역 0.6 m 앞에서 섬)과 '되돌아감'(1.0 m 앞에서 돌아섬)은 진입하지 않는 접근이라 R5 가 나면 모두 오예측이다
  (현재 속도로 내다보는 규칙의 본성. 명세는 예측 성공과 채널별 전달을 나눠 보고하라고 한다). '지나감'(0.75 m 옆을 나란히
  지나감)은 0건이어야 한다.
- 채널별 실제 전달(관제 화면 안내 표시 지연)은 여기서 재지 않는다(EC2·뷰어 경로, 명세 R5 설명).

변형: 정답 속도(SIM 상한 그대로)와, 위치에 잡음 σ 0.05 m 를 더하고 등속 칼만 필터(가속도 σ 2 m/s², World Track 과 같은
초기값)로 위치·속도를 다시 추정한 것. 둘 다 SIM 이라 제품 성능으로 쓰지 않는다(AGENTS.md 규칙 7). 게이트 상태는
기준을 넘으면 `SIM_UPPER_BOUND_MET`(판정 아님, 실영상 G6 에서 판정), 못 넘으면 `FAILED`(미통과)로 출력한다.
기본 시험은 짧은 시나리오(접근 8회)로 돈다. 판정 표본으로 재려면:

    SIMAI_G6R5_APPROACHES=60 uv run pytest tests/risk/test_r5_sim_g6.py -s
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
from simai_ai.risk.rules.r5_entry_prediction import R5_HORIZON_MS
from simai_ai.risk.zone_geometry import point_in_polygon

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = AI_DIR / "sim" / "scenarios" / "basic.json"
ZONE_POLYGON = [[4.0, 3.0], [5.5, 3.0], [5.5, 4.5], [4.0, 4.5]]
LEAD_REQUIRED_MS = 1000  # D-G6-03: 실제 진입보다 1초 이상 앞섬
PASS_RATIO = 0.9  # 접근의 90% 이상
FALSE_WINDOW_MS = 2 * R5_HORIZON_MS
CHUNK = 10  # 시뮬레이터 한 번에 넣는 접근 수
CYCLE_MS = 20_000  # 접근 1회: 서 있기 2초, 다가가기, 안 1초, 돌아가기, 서 있기(해소 3초 + 재개 10초보다 길게)
SPEEDS = (0.6, 0.9, 1.2, 1.5)
SLOW_SPEEDS = (0.3, 0.35, 0.4, 0.45)  # 다가가는 속력 하한(0.4 m/s) 근처 저속 접근. 측정·기록만 한다
STATUS_MET = "SIM_UPPER_BOUND_MET"
STATUS_FAILED = "FAILED"


def scenario(approaches: int, seed: int, speeds: tuple[float, ...] = SPEEDS) -> dict:
    """기본 교실·카메라 4대. 진입하는 사람(왼쪽에서 세 각도·네 속력), 오른쪽에서 다가오다 멈추거나 되돌아가는 사람,
    아래 변 0.75 m 옆을 왕복하는 사람."""
    base = json.loads(BASIC.read_text(encoding="utf-8"))
    entrant, stopper = [], []
    starts = [(1.0, 3.75), (1.0, 2.55), (1.0, 4.95)]
    for i in range(approaches):
        t = i * CYCLE_MS
        speed = speeds[i % len(speeds)]
        # 진입: 왼쪽 변(x = 4.0) 안쪽 0.4 m 지점까지
        sx, sy = starts[i % len(starts)]
        tx, ty = 4.4, 3.75 + 0.3 * (sy - 3.75) / 1.2
        arrive = t + 2000 + round(math.hypot(tx - sx, ty - sy) / speed * 1000)
        back = arrive + 1000 + round(math.hypot(tx - sx, ty - sy) / 1.0 * 1000)
        entrant += [
            {"t_ms": t, "xy": [sx, sy]},
            {"t_ms": t + 2000, "xy": [sx, sy]},
            {"t_ms": arrive, "xy": [tx, ty]},
            {"t_ms": arrive + 1000, "xy": [tx, ty]},
            {"t_ms": back, "xy": [sx, sy]},
        ]
        # 진입 안 함: 오른쪽 변(x = 5.5)으로 다가오다 0.6 m 앞에서 2초 멈춤(짝수) / 1.0 m 앞에서 바로 되돌아감(홀수)
        y = 3.75 + (0.4 if i % 2 else -0.4)
        stop_x = 6.1 if i % 2 == 0 else 6.5
        reach = t + 2000 + round((8.3 - stop_x) / speed * 1000)
        wait = 2000 if i % 2 == 0 else 0
        stopper += [
            {"t_ms": t, "xy": [8.3, y]},
            {"t_ms": t + 2000, "xy": [8.3, y]},
            {"t_ms": reach, "xy": [stop_x, y]},
            *([{"t_ms": reach + wait, "xy": [stop_x, y]}] if wait else []),
            {"t_ms": reach + wait + round((8.3 - stop_x) / 1.0 * 1000), "xy": [8.3, y]},
        ]
    duration = approaches * CYCLE_MS
    turns = range(math.ceil(duration / 8000) + 1)
    passer = [{"t_ms": i * 8000, "xy": [2.5 if i % 2 == 0 else 7.0, 2.25]} for i in turns]
    return dict(
        base,
        name="r5-g6",
        description="R5 G6 측정: 진입 접근 + 멈춤·되돌아감 + 구역 옆 통과",
        seed=seed,
        duration_ms=duration,
        people=[
            {"id": "entrant", "height_m": 1.72, "waypoints": entrant},
            {"id": "stopper", "height_m": 1.68, "waypoints": stopper},
            {"id": "passer", "height_m": 1.65, "waypoints": passer},
        ],
    )


class _AxisKalman:
    """한 축 등속 칼만 필터(상태 위치·속도, 백색 가속도 잡음). 시험 하네스용 순수 파이썬."""

    def __init__(self, z: float, t: int, meas_sigma: float, accel_sigma: float = 2.0, init_v_sigma: float = 1.0):
        self.p, self.v, self.t = z, 0.0, t
        self.P = [[meas_sigma**2, 0.0], [0.0, init_v_sigma**2]]
        self.r, self.q = meas_sigma**2, accel_sigma**2

    def update(self, z: float, t: int) -> None:
        dt = (t - self.t) / 1000.0
        (a, b), (c, d) = self.P
        # 예측
        p, v = self.p + self.v * dt, self.v
        a, b, c, d = (
            a + dt * (b + c) + dt * dt * d + self.q * dt**4 / 4,
            b + dt * d + self.q * dt**3 / 2,
            c + dt * d + self.q * dt**3 / 2,
            d + self.q * dt**2,
        )
        # 갱신
        s = a + self.r
        k0, k1 = a / s, c / s
        y = z - p
        self.p, self.v = p + k0 * y, v + k1 * y
        self.P = [[(1 - k0) * a, (1 - k0) * b], [c - k1 * a, d - k1 * b]]
        self.t = t

    @property
    def sigma(self) -> float:
        return math.sqrt(max(self.P[0][0], 0.0))


@dataclass(frozen=True)
class G6R5Result:
    entries: int
    leads_ms: tuple[int | None, ...]
    events: dict[str, int]
    false_events: dict[str, int]
    non_entry_approaches: int
    lag_ms: int

    @classmethod
    def combine(cls, results: "list[G6R5Result]") -> "G6R5Result":
        people = sorted({p for r in results for p in r.events})
        return cls(
            entries=sum(r.entries for r in results),
            leads_ms=tuple(v for r in results for v in r.leads_ms),
            events={p: sum(r.events.get(p, 0) for r in results) for p in people},
            false_events={p: sum(r.false_events.get(p, 0) for r in results) for p in people},
            non_entry_approaches=sum(r.non_entry_approaches for r in results),
            lag_ms=results[0].lag_ms,
        )

    @property
    def pass_ratio(self) -> float:
        ok = sum(1 for v in self.leads_ms if v is not None and v >= LEAD_REQUIRED_MS)
        return ok / self.entries if self.entries else math.nan

    @property
    def status(self) -> str:
        return STATUS_MET if self.pass_ratio >= PASS_RATIO else STATUS_FAILED

    def lead_quantile(self, q: float) -> float:
        values = sorted(v for v in self.leads_ms if v is not None)
        if not values:
            return math.nan
        return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]

    @property
    def stopper_predicted(self) -> int:
        """진입하지 않는 접근(멈춤·되돌아감)에서 난 R5 이벤트 수."""
        return self.events.get("stopper", 0)

    @property
    def false_ratio(self) -> float:
        total = sum(self.events.values())
        return sum(self.false_events.values()) / total if total else math.nan


def measure(out: Path, *, noise_sigma_m: float, lag_ms: int, seed: int) -> G6R5Result:
    """시뮬레이터 출력 out 에서 G6-R5 지표를 잰다. noise_sigma_m > 0 이면 잡음 + 칼만 필터로 위치·속도를 다시 추정한다."""
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
    truth_lines = (out / "truth" / "world_truth.jsonl").read_text(encoding="utf-8").splitlines()
    truth = [json.loads(line) for line in truth_lines]
    names = {p["world_id"]: p["person_id"] for p in truth[0]["people"]}

    registry = default_registry()
    clock = FixedClock(rows[0]["t"] + lag_ms)
    engine = RiskEngine([registry.create("R1", 1), registry.create("R5", 1)], clock)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="sim", clock=clock)
    filters: dict[str, tuple[_AxisKalman, _AxisKalman]] = {}
    starts: dict[str, list[int]] = {}  # world_id → R5 이벤트 생성 시각(RAISED·RESUMED)
    for row in rows:
        tracks = []
        for track in row["tracks"]:
            track = dict(track)
            if noise_sigma_m > 0:
                x, y = track["position_xy"]
                zx, zy = x + rng.gauss(0.0, noise_sigma_m), y + rng.gauss(0.0, noise_sigma_m)
                wid = track["world_id"]
                if wid not in filters:
                    filters[wid] = (
                        _AxisKalman(zx, row["t"], noise_sigma_m),
                        _AxisKalman(zy, row["t"], noise_sigma_m),
                    )
                else:
                    filters[wid][0].update(zx, row["t"])
                    filters[wid][1].update(zy, row["t"])
                fx, fy = filters[wid]
                dx, dy = fx.p - x, fy.p - y
                track["position_xy"] = [fx.p, fy.p]
                track["position_xy_sigma"] = [fx.sigma, fy.sigma]
                track["velocity"] = [fx.v, fy.v, 0.0]
                if track["ground_anchor_xyz"] is not None:
                    ax, ay, az = track["ground_anchor_xyz"]
                    track["ground_anchor_xyz"] = [ax + dx, ay + dy, az]
            tracks.append(track)
        world = WorldState.from_payload(
            {"t_latest_global": row["t"], "calibration_version": row["calibration_version"], "scene_version": "sim",
             "tracks": tracks}
        )  # fmt: skip
        clock.set(row["t"] + lag_ms)
        step = tracker.apply(engine.evaluate(world, [zone], Origin.LIVE), world, config_version="sim")
        for t in step.transitions:
            if t.state.rule_id == "R5" and (t.transition is Lifecycle.RAISED or t.resumed):
                (wid,) = t.state.world_ids
                starts.setdefault(wid, []).append(t.occurred_at)

    # 정답 진입(다각형 안에 든 시각)과 나온 시각
    entries: dict[str, list[tuple[int, int]]] = {}
    inside_since: dict[str, int | None] = {}
    for tick in truth:
        for person in tick["people"]:
            wid, inside = person["world_id"], point_in_polygon(person["position_xy"], ZONE_POLYGON)
            since = inside_since.get(wid)
            if inside and since is None:
                inside_since[wid] = tick["t"]
            elif not inside and since is not None:
                entries.setdefault(wid, []).append((since, tick["t"]))
                inside_since[wid] = None
    for wid, since in inside_since.items():
        if since is not None:
            entries.setdefault(wid, []).append((since, truth[-1]["t"]))

    leads: list[int | None] = []
    for wid, episodes in entries.items():
        previous_exit = -math.inf
        for entry, exit_ in episodes:
            candidates = [s for s in starts.get(wid, []) if previous_exit < s <= entry]
            leads.append(entry - min(candidates) if candidates else None)
            previous_exit = exit_
    events: dict[str, int] = {}
    false_events: dict[str, int] = {}
    for wid, name in names.items():
        person_starts = starts.get(wid, [])
        events[name] = len(person_starts)
        entry_times = [e for e, _ in entries.get(wid, [])]
        false_events[name] = sum(not any(s <= e <= s + FALSE_WINDOW_MS for e in entry_times) for s in person_starts)
    approaches = round((rows[-1]["t"] - rows[0]["t"]) / CYCLE_MS)
    return G6R5Result(sum(len(v) for v in entries.values()), tuple(leads), events, false_events, approaches, lag_ms)


def run_sim(tmp_path: Path, approaches: int, seed: int, speeds: tuple[float, ...] = SPEEDS) -> Path:
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario(approaches, seed, speeds), ensure_ascii=False), encoding="utf-8")
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


def report(name: str, r: G6R5Result) -> str:
    missed = sum(1 for v in r.leads_ms if v is None)
    by_person = ", ".join(f"{p} {r.false_events[p]}/{r.events[p]}" for p in sorted(r.events))
    return (
        f"{name}: 게이트 {r.status}(SIM 상한, 판정 아님). 진입 접근 {r.entries}회, 선행 1초 이상 {r.pass_ratio:.1%} "
        f"(기준 {PASS_RATIO:.0%}), 놓침 {missed}, 선행 시간 최소 {r.lead_quantile(0.0):.0f} / p10 {r.lead_quantile(0.1):.0f} "
        f"/ 중앙 {r.lead_quantile(0.5):.0f} / 최대 {r.lead_quantile(1.0):.0f} ms, "
        f"오예측 {sum(r.false_events.values())}/{sum(r.events.values())} ({r.false_ratio:.1%}; 사람별 오예측/이벤트 {by_person}), "
        f"진입하지 않는 접근(멈춤·되돌아감) {r.non_entry_approaches}회 중 R5 {r.stopper_predicted}건, lag {r.lag_ms} ms [SIM 상한]"
    )


@pytest.fixture(scope="module")
def sim_outs(tmp_path_factory):
    total = int(os.environ.get("SIMAI_G6R5_APPROACHES", "8"))
    outs = []
    for i in range(math.ceil(total / CHUNK)):
        n = min(CHUNK, total - i * CHUNK)
        outs.append((run_sim(tmp_path_factory.mktemp(f"r5-g6-{i}"), n, seed=98 + i), 98 + i))
    return outs


@pytest.mark.parametrize(("noise_sigma_m", "lag_ms"), [(0.0, 0), (0.0, 200), (0.05, 200)])
def test_g6_r5_on_simulator(sim_outs, noise_sigma_m, lag_ms):
    result = G6R5Result.combine(
        [measure(out, noise_sigma_m=noise_sigma_m, lag_ms=lag_ms, seed=seed) for out, seed in sim_outs]
    )
    print(report(f"G6-R5 SIM(잡음 σ {noise_sigma_m} m)", result))
    assert result.entries >= 8
    # SIM 상한에서도 선행 시간 기준을 못 넘으면 규칙이 틀렸다. 실영상 판정은 G6 검증 이슈에서 한다.
    assert result.status == STATUS_MET
    assert result.stopper_predicted <= result.non_entry_approaches
    if noise_sigma_m == 0.0:
        # 정답 속도면 진입 접근의 이벤트는 모두 진입으로 이어지고, 0.75 m 옆을 나란히 지나가는 사람에게는 나지 않는다.
        # 잡음 변형의 지나감 오예측은 측정·기록만 한다(속도 잡음에 달림).
        assert result.false_events["entrant"] == 0
        assert result.events["passer"] == 0


def test_g6_r5_slow_approaches_report(tmp_path):
    """하한(0.4 m/s) 근처 저속 접근(0.3·0.35·0.4·0.45 m/s)의 선행 시간을 측정·기록한다(합격 기준 아님, 정책 결정 자료).
    0.4 m/s 미만 정면 접근은 다가가는 속력 하한 때문에 놓치는 것이 현재 정책이다."""
    out = run_sim(tmp_path, 8, seed=198, speeds=SLOW_SPEEDS)
    result = measure(out, noise_sigma_m=0.0, lag_ms=200, seed=198)
    by_speed = {}
    for i, lead in enumerate(result.leads_ms):
        by_speed.setdefault(SLOW_SPEEDS[i % len(SLOW_SPEEDS)], []).append(lead)
    print(report("G6-R5 SIM 저속 접근(정답 속도, 측정·기록)", result))
    print("속력별 선행 시간(ms, None=놓침):", {k: v for k, v in sorted(by_speed.items())})
    assert result.entries == 8
    assert all(v is not None and v >= LEAD_REQUIRED_MS for v in by_speed[0.45])
