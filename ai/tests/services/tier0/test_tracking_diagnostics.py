"""추적 계측(S15P31A101-413, simai_ai.tracking.diagnostics)이 판정을 바꾸지 않고 사유별로 센다.

test_tier0_fusion_time 과 같은 SIM 장면(한 사람이 4대에 보임, cam3 만 400 ms 늦게 도착)을 Perception 에 넣는다.
- 늦은 카메라 제외 tick·시간 비율이 Perception.lagging_ticks 와 맞고 늦은 카메라만 잡힌다.
- 카메라 쌍 결과·클러스터 카메라 수·World Track 생성·분기 사유가 세어진다. 상태 파일용 summary 는 JSON 으로 쓸 수 있다.
- 판정 불변: 계측을 켠 결과와, 계측이 매번 예외를 내는 결과의 world_state payload 가 같다(계측 실패가 판정에 번지지 않는다).
SIM 상한 결과다(AGENTS 규칙 7).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pytest
import tier0_site as ts
from test_tier0_fusion_time import DURATION, SLOW, START, lag_of, perceive

from simai_ai.tracking.cross_view import AssociationFrame, PairEvidence
from simai_ai.tracking.diagnostics import TrackingDiagnostics, window_log_line


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """한 사람이 교실 가운데에 서 있다(4대 모두 본다). test_tier0_fusion_time 과 같은 장면."""
    doc = ts.scenario(DURATION, start_time_ms=START)
    doc["people"] = [{"id": "stander", "height_m": 1.72, "waypoints": [{"t_ms": 0, "xy": [4.0, 3.5]}]}]
    return ts.run_sim(tmp_path_factory.mktemp("diag"), doc)


def test_counts_lagging_pairs_and_world_lifecycle(run_dir) -> None:
    p, results = perceive(run_dir, lag_of(400))
    s = p.diagnostics.summary()
    assert s["ticks"] == len(results)
    assert s["lagging"][SLOW]["ticks"] == p.lagging_ticks[SLOW] >= 40
    assert set(s["lagging"]) == {SLOW}
    assert 0.5 < s["lagging"][SLOW]["ratio"] <= 1.0
    # cam3 는 융합에서 빠지므로 클러스터는 많아야 3대, 4대 클러스터는 없다
    assert s["cluster_cameras"].get("3", 0) > 0 and "4" not in s["cluster_cameras"]
    passed = sum(o.get("PASS", 0) + o.get("PASS_MARGINAL", 0) for o in s["pair_outcomes"].values())
    assert passed > 0 and all(SLOW not in pair for pair in s["pair_outcomes"])
    w = s["world"]
    assert w["appeared"] >= w["manager"]["new_track.created"] >= 1
    assert w["updated_cameras"].get("3", 0) > 0
    assert sum(w["state_ms"].values()) > 0
    json.dumps(s)  # 상태 파일에 그대로 쓴다
    line = window_log_line(p.diagnostics.take_window(results[-1].t))
    assert line.startswith("추적 계측") and SLOW in line


def test_window_resets_but_total_keeps(run_dir) -> None:
    p, results = perceive(run_dir, lag_of(0))
    d: TrackingDiagnostics = p.diagnostics
    total_ticks = d.summary()["ticks"]
    first = d.take_window(results[-1].t)
    assert first["ticks"] > 0
    assert d.take_window(results[-1].t)["ticks"] == 0
    assert d.summary()["ticks"] == total_ticks


def test_diagnostics_failure_does_not_change_judgment(run_dir) -> None:
    _, normal = perceive(run_dir, lag_of(400))

    class Broken(TrackingDiagnostics):
        def __getattribute__(self, name):
            if name.startswith("observe_") or name in ("window_due", "take_window"):
                raise RuntimeError("계측 고장")
            return super().__getattribute__(name)

    def patch(p) -> None:
        p.diagnostics = Broken()

    _, broken = perceive(run_dir, lag_of(400), patch=patch)
    assert [r.payload for r in normal] == [r.payload for r in broken]
    assert len(normal) > (DURATION // 100) // 2 and normal[0].t >= START


def test_nearest_epipolar_counts_each_view_in_both_directions() -> None:
    # A 하나와 B 둘: A의 최솟값 하나, B 각각의 최솟값 두 개를 센다.
    pairs = (
        PairEvidence(("A", "a"), ("B", "b1"), 4, epipolar_px=4.0),
        PairEvidence(("A", "a"), ("B", "b2"), 4, epipolar_px=30.0),
        PairEvidence(("A", "a2"), ("B", "b1"), 4, epipolar_px=float("nan")),
    )
    d = TrackingDiagnostics()
    d.observe_association(AssociationFrame(0, "cal", (), pairs))
    assert d.summary()["pair_best_epipolar_px"] == {"A/B": {"<=5": 2, "<=30": 1}}
    assert d.take_window()["pair_best_epipolar_px"] == {"A/B": {"<=5": 2, "<=30": 1}}
