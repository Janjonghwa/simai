"""활성 현장 경보 목록 (명세 3.10 현장 경보 재동기화, 7.3, AGENTS.md 규칙 5, S15P31A101-161).

생명주기 추적기의 활성 이벤트 상태 → health.active_site_alerts. 확인(acknowledged)은 지금 경보 단계(마지막 발생·
등급 상향·재개) 이후 상태를 대상으로 한 확인만 센다. 계약 스키마 검증은 tests/services/tier0 가 한다(risk 만 설치한
환경에는 jsonschema 가 없다).
"""

from dataclasses import replace

import pytest

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk import (
    AckActor,
    AlarmStop,
    Evaluation,
    EventIdFactory,
    Finding,
    FixedClock,
    LifecyclePolicy,
    LifecycleTracker,
    Origin,
    Persistence,
    Severity,
    WorldState,
    ack_from_contract,
    alarm_stop_contract_payload,
    build_active_site_alerts,
    default_summary,
    is_site_alert,
)

T0 = 1760342400100
NON_LIVE = [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY]
POLICY = LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}, clear_hold_ms=1000)


def finding(severity=Severity.HIGH, **kwargs):
    return Finding(
        rule_id="R1",
        rule_version=1,
        severity=severity,
        world_ids=("w-0012",),
        zone_id="z-04",
        reasons=("xy_inside_polygon",),
        metrics=kwargs.pop("metrics", {"dwell_s": 1.4}),
        **kwargs,
    )


class Harness:
    def __init__(self, track_json, origin=Origin.LIVE):
        self.clock = FixedClock(T0)
        self.origin = origin
        self.track_json = track_json
        self.tracker = LifecycleTracker(
            origin=origin,
            gateway_id="tier0-01",
            clock=self.clock,
            policy=POLICY,
            id_factory=EventIdFactory(self.clock, randbits=lambda n: 0),
        )

    def step(self, findings=(), dt=100):
        self.clock.advance(dt)
        t_obs = self.clock.now_ms() - 100
        world = WorldState.from_payload(
            {
                "t_latest_global": t_obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [self.track_json() | {"t_last_valid": t_obs}],
            }
        )
        evaluation = Evaluation(
            origin=self.origin,
            evaluated_at=self.clock.now_ms(),
            observation_time=t_obs,
            calibration_version="cal-7",
            scene_version="scene-3",
            rules=(),
            zones=(),
            findings=tuple(findings),
        )
        return self.tracker.apply(evaluation, world, config_version="cfg-12")

    def alerts(self):
        return build_active_site_alerts(self.tracker.active())


@pytest.fixture
def h(track_json):
    return Harness(track_json)


def test_raised_high_is_listed_with_contract_fields(h):
    [t] = h.step([finding()]).transitions
    assert h.alerts() == [
        {
            "event_id": t.event_id,
            "event_sequence": 1,
            "source": "LIVE",
            "acknowledged": False,
            "severity": "HIGH",
            "sound_mode": "REPEAT",
            "summary": "위험구역 진입 z-04: dwell_s 1.40",
            "rule_id": "R1",
            "zone_id": "z-04",
        }
    ]


@pytest.mark.parametrize("severity", [Severity.CAUTION, Severity.INFO])
def test_non_site_severity_is_not_listed(h, severity):
    h.step([finding(severity=severity)])
    assert h.tracker.active() and h.alerts() == []


@pytest.mark.parametrize("origin", NON_LIVE)
@pytest.mark.parametrize("severity", [Severity.CRITICAL, Severity.HIGH])
def test_non_live_events_never_listed(track_json, origin, severity):
    """PLANNED·WHAT_IF·REPLAY 는 CRITICAL 이어도 현장 경보 목록에 못 들어간다(AGENTS.md 규칙 5)."""
    h = Harness(track_json, origin=origin)
    h.step([finding(severity=severity)])
    [state] = h.tracker.active()
    assert not is_site_alert(state)
    assert h.alerts() == []


def test_mixed_trackers_only_live_listed(track_json):
    live, what_if = Harness(track_json), Harness(track_json, origin=Origin.WHAT_IF)
    live.step([finding(severity=Severity.CRITICAL)])
    what_if.step([finding(severity=Severity.CRITICAL)])
    alerts = build_active_site_alerts([*live.tracker.active(), *what_if.tracker.active()])
    assert [a["source"] for a in alerts] == ["LIVE"]


def test_ack_marks_acknowledged_with_latest_sequence(h):
    [t] = h.step([finding()]).transitions
    step = h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=1)
    [stop] = step.local_signals
    assert isinstance(stop, AlarmStop) and stop.target_sequence == 1 and stop.event_sequence == 2
    [item] = h.alerts()
    assert item["acknowledged"] is True and item["event_sequence"] == 2


def test_ack_without_target_uses_current_state(h):
    [t] = h.step([finding()]).transitions
    [stop] = h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0).local_signals
    assert stop.target_sequence == 1 and h.alerts()[0]["acknowledged"] is True


def test_target_beyond_current_is_clamped(h):
    [t] = h.step([finding()]).transitions
    [stop] = h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=99).local_signals
    assert stop.target_sequence == 1


def test_escalation_after_ack_sounds_again_in_list(h):
    """확인 뒤 HIGH → CRITICAL 이면 목록의 acknowledged 는 false (I1 acknowledged 는 저널 그대로 true)."""
    [t] = h.step([finding()]).transitions
    h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0)  # seq 2
    [up] = h.step([finding(severity=Severity.CRITICAL)]).transitions  # seq 3, 다시 울림
    assert up.state.acknowledged and not up.state.site_acknowledged and up.state.site_alarm_sequence == 3
    [item] = h.alerts()
    assert (item["severity"], item["event_sequence"], item["acknowledged"]) == ("CRITICAL", 3, False)


def test_late_ack_for_pre_escalation_state_does_not_silence(h):
    """-90 남은 일: 상향 전 상태에 대한 늦은 확인(site_ack 가 상향 뒤 도착)은 상향된 경보 소리를 멈추지 않는다."""
    [t] = h.step([finding()]).transitions  # HIGH seq 1, 노트북이 이 상태에서 현장 확인을 누름
    h.step([finding(severity=Severity.CRITICAL)])  # seq 2 상향
    step = h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=1)
    [stop] = step.local_signals
    assert stop.target_sequence == 1 and stop.event_sequence == 3  # 노트북은 target 1 < 경보 단계 2 라 무시한다
    assert step.transitions[0].state.acknowledged  # 저널에는 남는다
    assert h.alerts()[0]["acknowledged"] is False
    h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=2)
    assert h.alerts()[0]["acknowledged"] is True


def test_same_or_lower_severity_keeps_ack(h):
    [t] = h.step([finding(severity=Severity.CRITICAL)]).transitions
    h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0)
    h.step([finding(severity=Severity.HIGH)])  # 하향은 다시 울리지 않는다
    assert h.alerts()[0]["acknowledged"] is True and h.alerts()[0]["severity"] == "HIGH"


def test_downgrade_to_caution_leaves_list(h):
    """목록은 지금 등급 기준이다(계약: 항목 severity 는 CRITICAL·HIGH 만). CAUTION 으로 내려가면 빠진다."""
    h.step([finding()])
    h.step([finding(severity=Severity.CAUTION)])
    assert h.tracker.active() and h.alerts() == []


def test_resolved_event_leaves_list(h):
    h.step([finding()])
    for _ in range(12):
        h.step()
    assert h.tracker.active() == () and h.alerts() == []


def test_resume_is_unacknowledged_again(h):
    [t] = h.step([finding()]).transitions
    h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0)
    for _ in range(12):
        h.step()
    assert h.alerts() == []
    h.step([finding()])
    [item] = h.alerts()
    assert item["event_id"] == t.event_id and item["acknowledged"] is False


def test_duplicate_event_ids_keep_latest_state(h):
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])
    [current] = h.tracker.active()
    alerts = build_active_site_alerts([current, t.state, t.state])
    assert len(alerts) == 1 and alerts[0]["event_sequence"] == current.event_sequence


def test_latest_state_not_site_alert_is_dropped_even_if_older_was(h):
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CAUTION)])
    [current] = h.tracker.active()
    assert build_active_site_alerts([t.state, current]) == []


def test_summary_is_injectable_and_never_empty(h):
    h.step([finding(metrics={})])
    [state] = h.tracker.active()
    assert default_summary(state) == "위험구역 진입 z-04"
    assert build_active_site_alerts([state], summarize=lambda s: "")[0]["summary"] == "R1"
    assert build_active_site_alerts([state], summarize=lambda s: "규칙 근거")[0]["summary"] == "규칙 근거"


def test_site_alarm_sequence_none_falls_back_to_acks(h):
    [t] = h.step([finding()]).transitions
    h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0)
    [state] = h.tracker.active()
    assert replace(state, site_alarm_sequence=None).site_acknowledged


# --- 저널 복원·계약 payload (리뷰 R1·R2) ---------------------------------------------------------------------


def test_payload_carries_alarm_stage_and_ack_target(h):
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])  # seq 2 상향
    step = h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=1)
    payload = step.transitions[0].state.to_payload()
    assert payload["site_alarm_sequence"] == 2
    assert [a["target_sequence"] for a in payload["acks"]] == [1]
    assert step.transitions[0].to_payload()["ack"]["target_sequence"] == 1


def test_restored_tracker_keeps_pre_escalation_ack_from_silencing(h, track_json):
    """체크포인트·저널에서 되살린 뒤에도 상향 전 확인은 상향된 경보 소리를 멈추지 않는다."""
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])
    h.tracker.acknowledge(t.event_id, AckActor.SITE_BUTTON, "laptop-01", T0, target_sequence=1)
    saved = h.tracker.active()
    restarted = Harness(track_json)
    restarted.clock.set(h.clock.now_ms())
    restarted.tracker.restore(saved)
    [item] = restarted.alerts()
    assert item["acknowledged"] is False and item["event_sequence"] == saved[0].event_sequence + 1
    restarted.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0, target_sequence=1)  # 또 늦은 확인
    assert restarted.alerts()[0]["acknowledged"] is False
    restarted.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0)  # 지금 상태 대상
    assert restarted.alerts()[0]["acknowledged"] is True


@pytest.mark.parametrize(
    ("actor", "channel", "role"),
    [(AckActor.ADMIN, "CONTROL_ROOM", "ADMIN"), (AckActor.FOREMAN, "FOREMAN_PWA", "FOREMAN"),
     (AckActor.SITE_BUTTON, "SITE_BUTTON", "SITE")],
)
def test_contract_alarm_stop_uses_target_not_ack_transition_sequence(h, actor, channel, role):
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])  # seq 2
    [stop] = h.tracker.acknowledge(t.event_id, actor, "id-1", T0 + 5, target_sequence=1).local_signals
    assert stop.event_sequence == 3 and stop.target_sequence == 1  # 내부: ACK 전이 3, 대상 1
    payload = alarm_stop_contract_payload(stop, ack_id="6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f")
    assert payload == {
        "event_id": t.event_id,
        "event_sequence": 1,
        "ack": {"ack_id": "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f", "channel": channel, "actor_role": role,
                "actor_id": "id-1", "acked_at": T0 + 5},
    }


def test_contract_alarm_stop_without_target_or_time(h):
    [t] = h.step([finding()]).transitions
    [stop] = h.tracker.acknowledge(t.event_id, AckActor.ADMIN, "admin-1", T0).local_signals
    assert "event_sequence" not in alarm_stop_contract_payload(replace(stop, target_sequence=None), ack_id="a")
    with pytest.raises(ValueError):
        alarm_stop_contract_payload(replace(stop, acked_at=None), ack_id="a")


# --- 계약 확인 메시지(event_ack·site_ack) → acknowledge (사용자 승인 I1 event_ack.event_sequence) -----------


def _event_ack(event_id, target=None, channel="CONTROL_ROOM", role="ADMIN"):
    payload = {"event_id": event_id,
               "ack": {"ack_id": "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f", "channel": channel, "actor_role": role,
                       "actor_id": "user-0003", "acked_at": T0 + 7}}
    if target is not None:
        payload["event_sequence"] = target
    return payload


def test_late_event_ack_for_pre_escalation_state_does_not_silence(h):
    """관제 화면이 HIGH(seq 1)를 보고 누른 확인이 상향(seq 2) 뒤에 도착해도 상향된 경보를 멈추지 않는다."""
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])
    ack = ack_from_contract("event_ack", _event_ack(t.event_id, target=1))
    assert (ack.actor, ack.target_sequence) == (AckActor.ADMIN, 1)
    step = h.tracker.acknowledge(**ack.acknowledge_args())
    assert step.transitions[0].state.acknowledged  # 저널에는 남는다
    assert h.alerts()[0]["acknowledged"] is False
    payload = alarm_stop_contract_payload(step.local_signals[0], ack_id=ack.ack_id)
    assert payload["event_sequence"] == 1 and payload["ack"]["ack_id"] == ack.ack_id  # 노트북이 늦은 정지로 거른다
    h.tracker.acknowledge(**ack_from_contract("event_ack", _event_ack(t.event_id, target=2)).acknowledge_args())
    assert h.alerts()[0]["acknowledged"] is True


def test_event_ack_without_target_uses_state_at_recording(h):
    [t] = h.step([finding()]).transitions
    h.step([finding(severity=Severity.CRITICAL)])
    ack = ack_from_contract("event_ack", _event_ack(t.event_id, channel="FOREMAN_PWA", role="FOREMAN"))
    assert ack.actor is AckActor.FOREMAN and ack.target_sequence is None
    h.tracker.acknowledge(**ack.acknowledge_args())  # 이전 송신 측: 지금 상태 대상
    assert h.alerts()[0]["acknowledged"] is True


def test_site_ack_maps_to_site_button(h):
    [t] = h.step([finding()]).transitions
    payload = _event_ack(t.event_id, target=1, channel="SITE_BUTTON", role="SITE")
    ack = ack_from_contract("site_ack", payload)
    assert ack.actor is AckActor.SITE_BUTTON and ack.target_sequence == 1


@pytest.mark.parametrize(
    ("msg_type", "payload"),
    [
        ("event_ack", _event_ack("e", channel="SITE_BUTTON", role="SITE")),  # 현장 버튼은 event_ack 로 받지 않는다
        ("site_ack", _event_ack("e")),  # 관제 화면 확인은 site_ack 로 받지 않는다
        ("event_ack", _event_ack("e", channel="CONTROL_ROOM", role="SITE")),  # operator_ack: 현장 역할은 현장 버튼만
        ("event_ack", _event_ack("e", channel="FOREMAN_PWA", role="SITE")),
        ("site_ack", _event_ack("e", channel="SITE_BUTTON", role="ADMIN")),
        ("event_ack", _event_ack("e", target=0)),
        ("alarm_stop", _event_ack("e")),
    ],
)
def test_contract_ack_rejects_mismatch(msg_type, payload):
    """계약(스키마)이 거부하는 조합만 거부한다."""
    with pytest.raises(RiskInputError):
        ack_from_contract(msg_type, payload)


@pytest.mark.parametrize(
    ("channel", "role", "actor"),
    [("CONTROL_ROOM", "ADMIN", AckActor.ADMIN), ("FOREMAN_PWA", "FOREMAN", AckActor.FOREMAN),
     ("CONTROL_ROOM", "FOREMAN", AckActor.FOREMAN), ("FOREMAN_PWA", "ADMIN", AckActor.ADMIN)],
)
def test_contract_valid_event_ack_combinations_accepted_and_channel_kept(h, channel, role, actor):
    """반장이 관제 화면으로, 관리자가 PWA 로 확인해도 계약상 유효하다. 주체는 역할로, 채널은 받은 값 그대로."""
    [t] = h.step([finding()]).transitions
    ack = ack_from_contract("event_ack", _event_ack(t.event_id, target=1, channel=channel, role=role))
    assert (ack.actor, ack.channel) == (actor, channel)
    step = h.tracker.acknowledge(**ack.acknowledge_args())
    assert step.transitions[0].ack.channel == channel
    assert step.transitions[0].to_payload()["ack"]["channel"] == channel
    payload = alarm_stop_contract_payload(step.local_signals[0], ack_id=ack.ack_id)
    assert (payload["ack"]["channel"], payload["ack"]["actor_role"]) == (channel, role)
    assert h.alerts()[0]["acknowledged"] is True


def test_event_ack_without_target_logs_warning(h, caplog):
    [t] = h.step([finding()]).transitions
    with caplog.at_level("WARNING", logger="simai_ai.risk.alerting"):
        ack_from_contract("event_ack", _event_ack(t.event_id))
    assert "대상 미지정 확인" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="simai_ai.risk.alerting"):
        ack_from_contract("event_ack", _event_ack(t.event_id, target=1))
    assert caplog.text == ""


# --- I0 local_alert payload (S15P31A101-171) ---


def test_local_alert_payload_matches_signal_state_and_list_summary(h):
    from simai_ai.risk import local_alert_payload

    step = h.step([finding()])
    [t] = step.transitions
    payload = local_alert_payload(t.state)
    assert payload["lifecycle"] == "RAISED" and payload["event_sequence"] == 1 and payload["source"] == "LIVE"
    assert payload["summary"] == h.alerts()[0]["summary"] and payload["acknowledged"] is False
    assert payload["observation_time"] == t.state.observation_time and payload["evaluated_at"] == t.state.evaluated_at


@pytest.mark.parametrize("severity", [Severity.CAUTION, Severity.INFO])
def test_local_alert_payload_refuses_non_site_severity(h, severity):
    from simai_ai.risk import local_alert_payload

    [t] = h.step([finding(severity=severity)]).transitions
    with pytest.raises(ValueError):
        local_alert_payload(t.state)


@pytest.mark.parametrize("origin", NON_LIVE)
def test_local_alert_payload_refuses_non_live(track_json, origin):
    from simai_ai.risk import local_alert_payload

    harness = Harness(track_json, origin=origin)
    [t] = harness.step([finding()]).transitions
    with pytest.raises(ValueError):
        local_alert_payload(t.state)
