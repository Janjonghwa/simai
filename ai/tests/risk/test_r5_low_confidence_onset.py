"""R5 낮은 신뢰 새 발생 보류: 높이 모름(Z_UNKNOWN) + 위치 예측(PREDICTED)이면 새 R5 를 내지 않는다 (S15P31A101-855).

2026-11-23 15:33 리허설: 작업 시작 1.4초 뒤 한 카메라로만 보이고 접지·머리 높이를 모르는 사람(구역에서 2.16 m)에게
R5 가 났다(confidence_reasons {Z_UNKNOWN, PREDICTED, SINGLE_VIEW}, time_to_entry_s 0.0). R5 규칙은 PREDICTED 트랙을
판정하지 않으므로 그 조합은 관측 프레임의 Z_UNKNOWN 발생 판정 1개 뒤 위치가 예측으로 바뀌어 lifecycle 의 '지속 대기 중
관측 상실' 발생(PREDICTED 를 더함)으로만 생긴다. 42초 뒤의 정상 R5(HIGH, 확인 필요 없음)는 그대로 나야 한다.
실제 규칙(EntryPredictionRule)·엔진·생명주기로 재현한다.
"""

import pytest

from simai_ai.risk import (
    EntryPredictionRule,
    FixedClock,
    Lifecycle,
    LifecycleTracker,
    Origin,
    RiskEngine,
    WorldState,
    Zone,
)
from simai_ai.risk.common import ConfidenceReason as R
from simai_ai.risk.common import Severity
from simai_ai.risk.lifecycle import REASON_PENDING_OBSERVATION_LOST
from simai_ai.risk.rule import Finding
from simai_ai.risk.rules.r5_entry_prediction import (
    REASON_PREDICTED_ENTRY,
    REASON_PREDICTED_TOUCHING_NOW,
    REASON_TRACK_NOT_OBSERVED,
    r5_low_confidence_onset,
)

T0 = 1763879611400  # 2026-11-23 06:33:31.400 UTC(15:33:31.400 KST) 근처, 값 자체는 판정과 무관
SQUARE = [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]]  # 왼쪽 변 x = 3.0
FRAME_MS = 100

# 15:33:32 리허설 형태: 구역 변에서 2.156 m, 0.421 m/s, 한 카메라(깊이 방향 σ 큼), 접지점·머리 높이 모름.
# 범위 x 반경 = 2·0.9 + 0.3 = 2.1 m < 2.156 m(관측 범위는 안 걸림), 판정 시각까지 300 ms × 0.421 m/s = 0.126 m 옮기면 걸림
# → 선행 0 초(predicted_touching_now). zone_distance_m 은 범위 중심(position_xy)에서 잰 2.156 m 다.
REHEARSAL = {
    "xy": (3.0 - 2.156, 5.0), "velocity": (0.421, 0.0, 0.0), "sigma": (0.9, 0.3), "anchor": None, "head_z": None,
    "cameras": ("cam_A",), "lag_ms": 300,
}
# 15:34:13 정상 형태: 구역 변에서 0.438 m, 0.292 m/s, 두 카메라, 접지점·머리 높이 앎.
# 범위 반경 0.4 m, 판정 시각까지 200 ms × 0.292 = 0.058 m 옮기면 걸림 → 선행 0 초.
NORMAL = {
    "xy": (3.0 - 0.438, 5.0), "velocity": (0.292, 0.0, 0.0), "sigma": (0.05, 0.05), "anchor": "same", "head_z": 1.7,
    "cameras": ("cam_A", "cam_C"), "lag_ms": 200,
}


def zone():
    return Zone.from_json(
        {
            "zone_id": "z-04",
            "zone_version": "zone-1",
            "judge_type": "VOLUME_3D",
            "polygon_xy": SQUARE,
            "z_range": [0.0, 2.5],
            "severity": "HIGH",
        }
    )


def track_json(case, *, t_last_valid, state="OBSERVED", anchor="case"):
    xy = case["xy"]
    anchor = case["anchor"] if anchor == "case" else anchor
    anchor_xyz = [xy[0], xy[1], 0.0] if anchor == "same" else anchor
    support = anchor_xyz is not None
    return {
        "world_id": "w-0007",
        "t_last_valid": t_last_valid,
        "observation_state": state,
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH",
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": list(case["sigma"]),
        "pelvis_xyz": [xy[0], xy[1], 0.92],
        "ground_anchor_xyz": anchor_xyz,
        "support_surface_id": "floor" if support else None,
        "support_surface_validity": "VALID" if support else "UNKNOWN",
        "contact_validity": "VALID" if support else "UNAVAILABLE",
        "z_validity": "VALID" if support else "UNAVAILABLE",
        "head_z": case["head_z"],
        "velocity": list(case["velocity"]),
        "cameras": list(case["cameras"]),
    }


class Run:
    """10 Hz 판정 루프. 관측 프레임 k 의 관측 시각은 T0 + k·100 ms, 판정은 그 뒤 lag_ms."""

    def __init__(self, case):
        self.case = case
        self.clock = FixedClock(T0)
        self.engine = RiskEngine([EntryPredictionRule()], self.clock)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=self.clock)
        self.last_observed = None

    def step(self, k, *, state="OBSERVED", anchor="case"):
        t = T0 + k * FRAME_MS
        if state == "OBSERVED":
            self.last_observed = t
        # 예측 프레임의 t_last_valid 는 마지막 관측 시각 그대로다(6.1).
        tj = track_json(self.case, t_last_valid=self.last_observed, state=state, anchor=anchor)
        self.clock.set(t + self.case["lag_ms"])
        world = WorldState.from_payload(
            {"t_latest_global": t, "calibration_version": "cal-7", "scene_version": "scene-3", "tracks": [tj]}
        )
        evaluation = self.engine.evaluate(world, [zone()], Origin.LIVE)
        return evaluation, self.tracker.apply(evaluation, world, config_version="cfg-855").transitions


def test_rehearsal_frame_is_z_unknown_touching_now_prediction():
    """재현 확인: 15:33:32 형태의 관측 프레임은 리허설 수치(선행 0 초, 2.156 m, 0.421 m/s)의 R5 발생 판정이다."""
    run = Run(REHEARSAL)
    evaluation, transitions = run.step(0)
    (f,) = evaluation.findings
    assert f.holding is False and f.reasons == (REASON_PREDICTED_ENTRY, REASON_PREDICTED_TOUCHING_NOW)
    assert f.confidence_reasons == (R.Z_UNKNOWN, R.SINGLE_VIEW)
    assert f.metrics["time_to_entry_s"] == 0.0
    assert f.metrics["speed_mps"] == pytest.approx(0.421)
    assert f.metrics["zone_distance_m"] == pytest.approx(2.156)
    assert transitions == () and list(run.tracker.pending().values()) == [1]


def test_rehearsal_z_unknown_then_predicted_makes_no_event():
    """(a) 15:33:32: Z_UNKNOWN 발생 판정 1프레임 뒤 위치가 예측으로 바뀌면(지금까지는 PREDICTED 를 더해 바로 발생) 새 R5 없음."""
    run = Run(REHEARSAL)
    run.step(0)
    evaluation, transitions = run.step(1, state="PREDICTED")
    (hold,) = evaluation.findings
    assert hold.holding is True and REASON_TRACK_NOT_OBSERVED in hold.reasons
    assert transitions == ()
    assert run.tracker.pending() == {} and run.tracker.active() == ()
    # 예측이 이어져도(최대 500 ms 뒤 UNKNOWN 포함) 새 이벤트 없음
    for k in range(2, 8):
        assert run.step(k, state="PREDICTED" if k < 6 else "UNKNOWN")[1] == ()
    assert run.tracker.active() == ()


def test_after_suppression_observed_z_unknown_still_raises():
    """보류는 그 대기만 버린다. 관측 위치가 돌아와 Z_UNKNOWN 발생 판정이 지속 조건을 채우면 지금처럼 발생한다."""
    run = Run(REHEARSAL)
    run.step(0)
    run.step(1, state="PREDICTED")
    assert run.step(2)[1] == ()
    [t] = run.step(3)[1]
    assert t.transition is Lifecycle.RAISED
    assert t.state.confidence_reasons == (R.Z_UNKNOWN, R.SINGLE_VIEW)


def test_normal_rehearsal_r5_still_raises_without_check():
    """(b) 15:34:13: 두 카메라·높이 앎·관측 위치 R5 는 그대로 발생(확인 필요 없음)."""
    run = Run(NORMAL)
    assert run.step(0)[1] == ()
    [t] = run.step(1)[1]
    assert t.transition is Lifecycle.RAISED and t.state.severity is Severity.CAUTION
    assert t.state.needs_check is False and t.state.confidence_reasons == ()
    assert t.state.metrics["time_to_entry_s"] == 0.0
    assert t.state.metrics["speed_mps"] == pytest.approx(0.292)
    assert t.state.metrics["zone_distance_m"] == pytest.approx(0.438)


def test_z_unknown_only_raises():
    """(c-1) 높이만 모름(관측 위치): Z_UNKNOWN 판정이 지속 조건을 채우면 지금처럼 확인 필요로 발생."""
    run = Run(REHEARSAL)
    assert run.step(0)[1] == ()
    [t] = run.step(1)[1]
    assert t.transition is Lifecycle.RAISED and t.state.needs_check
    assert t.state.confidence_reasons == (R.Z_UNKNOWN, R.SINGLE_VIEW)
    assert REASON_PENDING_OBSERVATION_LOST not in t.state.reasons


@pytest.mark.parametrize("case", [NORMAL, {**REHEARSAL, "anchor": "same", "head_z": 1.7}], ids=["normal", "rehearsal_z_known"])
def test_predicted_only_raises_via_pending_loss(case):
    """(c-2) 위치 예측만(높이 앎): 대기 중 관측 상실 발생은 지금처럼 PREDICTED 를 더해 발생한다."""
    run = Run(case)
    run.step(0)
    [t] = run.step(1, state="PREDICTED")[1]
    assert t.transition is Lifecycle.RAISED
    assert R.PREDICTED in t.state.confidence_reasons and R.Z_UNKNOWN not in t.state.confidence_reasons
    assert t.state.reasons[-1] == REASON_PENDING_OBSERVATION_LOST


@pytest.mark.parametrize("case", [REHEARSAL, NORMAL], ids=["opened_z_unknown", "opened_normal"])
def test_open_r5_is_kept_when_low_confidence_arrives(case):
    """(d) 이미 열린 R5 는 접지 모름 + 예측 위치가 와도 해소되지 않고 유지된다(해소 유지 3초를 넘겨도)."""
    run = Run(case)
    run.step(0)
    [opened] = run.step(1)[1]
    assert opened.transition is Lifecycle.RAISED
    for k in range(2, 2 + 40):  # 4초
        _, transitions = run.step(k, state="PREDICTED", anchor=None)
        assert all(t.transition is not Lifecycle.RESOLVED for t in transitions)
    [state] = run.tracker.active()
    assert state.event_id == opened.event_id and state.key.rule_id == "R5"


def _finding(*reasons):
    return Finding(
        rule_id="R5", rule_version=1, severity=Severity.CAUTION, world_ids=("w-1",), zone_id="z-04",
        confidence_reasons=reasons, reasons=(REASON_PREDICTED_ENTRY,), metrics={},
    )


def test_low_confidence_helper_needs_both_reasons():
    assert r5_low_confidence_onset(_finding(R.Z_UNKNOWN, R.PREDICTED, R.SINGLE_VIEW))
    assert not r5_low_confidence_onset(_finding(R.Z_UNKNOWN, R.SINGLE_VIEW))
    assert not r5_low_confidence_onset(_finding(R.PREDICTED, R.SINGLE_VIEW))
    assert not r5_low_confidence_onset(_finding())
