"""경보 코어: 등급별 소리, LIVE 아닌 출처 거부, ACK, 해소, 중복·역순, 현장 확인, 시험 경보, 소리 실패."""

from __future__ import annotations

import pytest

from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.core import Outcome, SiteAlertCore
from simai_site_alert.display import render_lines
from simai_site_alert.sound import FakeSoundBackend


def alarm_stop(event_id: str, *, channel: str = "CONTROL_ROOM", role: str = "ADMIN", actor: str = "user-0003",
               msg_id: str | None = None) -> dict:
    import uuid

    return {
        "schema_version": "1.2.0",
        "msg_type": "alarm_stop",
        "msg_id": msg_id or str(uuid.uuid4()),
        "sent_at": 1760342402130,
        "source_id": "tier0-01",
        "payload": {
            "event_id": event_id,
            "ack": {"ack_id": str(uuid.uuid4()), "channel": channel, "actor_role": role, "actor_id": actor,
                    "acked_at": 1760342402100},
        },
    }


# ---- 등급별 동작 -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH"])
def test_raised_starts_severity_pattern_and_shows(core: SiteAlertCore, sound: FakeSoundBackend, severity: str) -> None:
    eid = uuid7()
    result = core.handle(local_alert(eid, 1, "RAISED", severity=severity))
    assert result.outcome is Outcome.APPLIED
    assert sound.calls == [("start", severity)]
    state = core.screen_state()
    assert [a.event_id for a in state.alerts] == [eid]
    assert state.playing == severity
    assert core.alarm_status() == {"sounding_event_ids": [eid], "displayed_event_ids": [eid]}


def test_critical_takes_priority_over_high(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    high, crit = uuid7(), uuid7()
    core.handle(local_alert(high, 1, "RAISED", severity="HIGH"))
    core.handle(local_alert(crit, 1, "RAISED", severity="CRITICAL"))
    assert sound.playing.name == "CRITICAL"
    assert [a.severity for a in core.screen_state().alerts] == ["CRITICAL", "HIGH"]
    core.handle(alarm_stop(crit))  # CRITICAL 확인 → 남은 HIGH 패턴으로
    assert sound.playing.name == "HIGH"


def test_update_escalation_switches_pattern(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    core.handle(local_alert(eid, 2, "UPDATED", severity="CRITICAL", summary="R2 가장자리 0.4m"))
    assert sound.calls == [("start", "HIGH"), ("start", "CRITICAL")]
    assert core.active_alerts()[eid].summary == "R2 가장자리 0.4m"


def test_same_pattern_update_does_not_restart_sound(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(local_alert(eid, 2, "UPDATED"))
    assert sound.calls == [("start", "CRITICAL")]


def test_low_confidence_keeps_severity_and_marks_needs_check(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL", confidence="LOW", needs_check=False))
    alert = core.active_alerts()[eid]
    assert alert.severity == "CRITICAL" and alert.needs_check
    assert sound.playing.name == "CRITICAL"
    assert "확인 필요" in render_lines(core.screen_state())[-1]


# ---- LIVE 만, CRITICAL·HIGH 만 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["PLANNED", "WHAT_IF", "REPLAY", None])
def test_non_live_source_rejected(core: SiteAlertCore, sound: FakeSoundBackend, source: str | None) -> None:
    msg = local_alert(uuid7(), 1, "RAISED", source=source)
    if source is None:
        del msg["payload"]["source"]
    result = core.handle(msg)
    assert result.outcome is Outcome.REJECTED and result.reason == "NOT_LIVE"
    assert sound.calls == [] and core.screen_state().alerts == ()
    assert core.rejected["NOT_LIVE"] == 1


@pytest.mark.parametrize("severity", ["CAUTION", "INFO", None])
def test_non_site_severity_rejected(core: SiteAlertCore, sound: FakeSoundBackend, severity: str | None) -> None:
    result = core.handle(local_alert(uuid7(), 1, "RAISED", severity=severity))
    assert result.outcome is Outcome.REJECTED and result.reason == "NOT_SITE_SEVERITY"
    assert sound.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(event_id="not-a-uuid"),
        lambda p: p.update(event_sequence=0),
        lambda p: p.update(event_sequence=True),
        lambda p: p.update(lifecycle="ACKED"),
    ],
)
def test_malformed_safety_fields_rejected(core: SiteAlertCore, sound: FakeSoundBackend, mutate) -> None:
    msg = local_alert(uuid7(), 1, "RAISED")
    mutate(msg["payload"])
    assert core.handle(msg).outcome is Outcome.REJECTED
    assert sound.calls == []


def test_missing_display_field_still_alarms(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """표시용 필드가 빠진 진짜 CRITICAL 을 조용히 버리지 않는다(경고만 남김)."""
    msg = local_alert(uuid7(), 1, "RAISED")
    del msg["payload"]["summary"], msg["payload"]["world_ids"]
    assert core.handle(msg).outcome is Outcome.APPLIED
    assert sound.playing.name == "CRITICAL"


def test_non_json_rejected(core: SiteAlertCore) -> None:
    assert core.handle_text("{not json").outcome is Outcome.REJECTED


def test_unknown_message_ignored(core: SiteAlertCore) -> None:
    result = core.handle({"msg_type": "clock_sync_request", "msg_id": "x", "payload": {}})
    assert result.outcome is Outcome.IGNORED


# ---- ACK(alarm_stop) -------------------------------------------------------------------------------------------


def test_alarm_stop_stops_sound_keeps_display(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    assert core.handle(alarm_stop(eid)).outcome is Outcome.APPLIED
    assert sound.calls == [("start", "CRITICAL"), ("stop", None)]
    alert = core.active_alerts()[eid]
    assert alert.acknowledged and alert.ack.channel == "CONTROL_ROOM" and alert.ack.actor_id == "user-0003"
    assert core.alarm_status() == {"sounding_event_ids": [], "displayed_event_ids": [eid]}
    assert "확인됨(관제 화면 user-0003)" in render_lines(core.screen_state())[-1]


def test_ack_is_sticky_until_resolved(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """확인 뒤 같은 등급의 acknowledged=false UPDATED 가 와도 다시 울리지 않는다(확인은 이벤트에 붙는다)."""
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(alarm_stop(eid))
    core.handle(local_alert(eid, 2, "UPDATED", acknowledged=False))
    assert sound.playing is None
    assert core.active_alerts()[eid].acknowledged


def test_escalation_after_ack_alarms_again(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """확인 뒤 같은 이벤트가 HIGH → CRITICAL 로 올라가면 확인을 풀고 다시 울린다(새 event_sequence)."""
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    core.handle(alarm_stop(eid))
    assert sound.playing is None
    result = core.handle(local_alert(eid, 2, "UPDATED", severity="CRITICAL", acknowledged=True))
    assert result.outcome is Outcome.APPLIED
    assert sound.playing.name == "CRITICAL"
    alert = core.active_alerts()[eid]
    assert not alert.acknowledged and alert.ack is None
    assert core.alarm_status()["sounding_event_ids"] == [eid]
    core.handle(alarm_stop(eid))  # 상향된 경보도 다시 확인할 수 있다
    assert sound.playing is None


def test_escalation_after_site_ack_drops_pending_and_alarms(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    core.site_ack(eid)
    core.handle(local_alert(eid, 2, "UPDATED", severity="CRITICAL"))
    assert sound.playing.name == "CRITICAL"
    assert core.pending_site_acks() == []  # 상향 전 확인을 다시 보내 상향 경보를 멈추지 않게
    assert core.site_ack(eid) is not None  # 다시 누를 수 있다


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH"])
def test_same_or_lower_severity_after_ack_stays_silent(core: SiteAlertCore, sound: FakeSoundBackend, severity: str) -> None:
    """같은 등급 재전송·하향(CRITICAL → HIGH)은 확인을 유지한다."""
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL"))
    core.handle(alarm_stop(eid))
    core.handle(local_alert(eid, 2, "UPDATED", severity=severity, acknowledged=False))
    assert sound.playing is None
    assert core.active_alerts()[eid].acknowledged


def test_stale_escalation_ignored(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """낮은 sequence 로 늦게 온 상향은 확인을 풀지 않는다."""
    eid = uuid7()
    core.handle(local_alert(eid, 3, "UPDATED", severity="HIGH"))
    core.handle(alarm_stop(eid))
    assert core.handle(local_alert(eid, 2, "UPDATED", severity="CRITICAL")).outcome is Outcome.STALE
    assert sound.playing is None


def test_acknowledged_update_only_displays(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 3, "UPDATED", acknowledged=True))
    assert sound.calls == []
    assert core.active_alerts()[eid].acknowledged


def test_alarm_stop_before_local_alert(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(alarm_stop(eid))
    core.handle(local_alert(eid, 1, "RAISED"))
    assert sound.calls == []
    assert core.active_alerts()[eid].acknowledged


def test_alarm_stop_for_resolved_event_is_stale(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(local_alert(eid, 2, "RESOLVED"))
    assert core.handle(alarm_stop(eid)).outcome is Outcome.STALE
    assert core.screen_state().alerts == ()


# ---- 해소 -----------------------------------------------------------------------------------------------------


def test_resolved_clears_display_and_sound(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(local_alert(eid, 2, "RESOLVED"))
    assert sound.calls == [("start", "CRITICAL"), ("stop", None)]
    assert core.screen_state().alerts == ()
    assert core.alarm_status() == {"sounding_event_ids": [], "displayed_event_ids": []}


def test_acknowledged_display_stays_until_resolved(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(alarm_stop(eid))
    core.handle(local_alert(eid, 2, "UPDATED", acknowledged=True))
    assert eid in core.active_alerts()
    core.handle(local_alert(eid, 3, "RESOLVED", acknowledged=True))
    assert eid not in core.active_alerts()


def test_resolving_one_keeps_other_sounding(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    a, b = uuid7(), uuid7()
    core.handle(local_alert(a, 1, "RAISED", severity="CRITICAL"))
    core.handle(local_alert(b, 1, "RAISED", severity="HIGH"))
    core.handle(local_alert(a, 2, "RESOLVED"))
    assert sound.playing.name == "HIGH"
    assert list(core.active_alerts()) == [b]


def test_reopened_after_resolve_alarms_again(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """해소 후 10초 안의 재발생은 같은 event_id 를 높은 sequence 로 재개한다(명세 6.4). 이전 확인은 넘기지 않는다."""
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.handle(alarm_stop(eid))
    core.handle(local_alert(eid, 2, "RESOLVED"))
    core.handle(local_alert(eid, 3, "UPDATED", acknowledged=False))
    assert sound.playing.name == "CRITICAL"
    assert not core.active_alerts()[eid].acknowledged


# ---- 중복·역순 -------------------------------------------------------------------------------------------------


def test_duplicate_sequence_ignored(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    result = core.handle(local_alert(eid, 1, "RAISED", summary="다른 문구"))
    assert result.outcome is Outcome.STALE
    assert core.active_alerts()[eid].summary != "다른 문구"
    assert sound.calls == [("start", "CRITICAL")]


def test_duplicate_msg_id_ignored(core: SiteAlertCore) -> None:
    eid = uuid7()
    msg = local_alert(eid, 1, "RAISED")
    core.handle(msg)
    assert core.handle(msg).outcome is Outcome.STALE


def test_late_lower_sequence_does_not_roll_back(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 3, "UPDATED", summary="최신"))
    assert core.handle(local_alert(eid, 2, "UPDATED", summary="옛것")).outcome is Outcome.STALE
    assert core.active_alerts()[eid].summary == "최신"
    assert core.active_alerts()[eid].event_sequence == 3


def test_late_raised_after_resolved_does_not_resurrect(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 2, "RESOLVED"))  # RAISED 보다 먼저 도착
    assert core.handle(local_alert(eid, 1, "RAISED")).outcome is Outcome.STALE
    assert core.screen_state().alerts == ()
    assert sound.calls == []


def test_update_before_raised(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """RAISED 를 놓치고 UPDATED 가 먼저 와도 경보를 켠다. 늦게 온 RAISED 는 무시한다."""
    eid = uuid7()
    core.handle(local_alert(eid, 2, "UPDATED", summary="갱신"))
    assert sound.playing.name == "CRITICAL"
    assert core.handle(local_alert(eid, 1, "RAISED", summary="처음")).outcome is Outcome.STALE
    assert core.active_alerts()[eid].summary == "갱신"


# ---- 현장 확인 버튼 --------------------------------------------------------------------------------------------


def test_site_ack_stops_sound_and_builds_message(core: SiteAlertCore, sound: FakeSoundBackend, clock) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    envelope = core.site_ack(eid)
    assert envelope is not None
    assert envelope["msg_type"] == "site_ack" and envelope["source_id"] == "laptop-01"
    ack = envelope["payload"]["ack"]
    assert envelope["payload"]["event_id"] == eid
    assert ack["channel"] == "SITE_BUTTON" and ack["actor_role"] == "SITE" and ack["actor_id"] == "laptop-01"
    assert ack["acked_at"] == clock.now_ms() == envelope["sent_at"]
    assert sound.calls[-1] == ("stop", None)
    alert = core.active_alerts()[eid]
    assert alert.acknowledged and alert.ack.local
    assert core.pending_site_acks() == [envelope]
    assert core.site_ack(eid) is None  # 두 번 눌러도 한 번만


def test_site_ack_cleared_by_alarm_stop(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.site_ack(eid)
    core.handle(alarm_stop(eid, channel="SITE_BUTTON", role="SITE", actor="laptop-01"))
    assert core.pending_site_acks() == []
    assert not core.active_alerts()[eid].ack.local


def test_site_ack_cleared_by_resolve(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    core.site_ack(eid)
    core.handle(local_alert(eid, 2, "RESOLVED"))
    assert core.pending_site_acks() == []


def test_site_ack_unknown_or_acked_event(core: SiteAlertCore) -> None:
    assert core.site_ack(uuid7()) is None
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", acknowledged=True))
    assert core.site_ack(eid) is None


# ---- 시험 경보 -------------------------------------------------------------------------------------------------


def test_test_alert_plays_and_shows_test_label(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    assert core.start_test_alert() is True
    assert sound.calls == [("start", "TEST")]
    lines = render_lines(core.screen_state())
    assert any("시험" in line and "실제 위험이 아닙니다" in line for line in lines)
    assert core.screen_state().alerts == ()  # RiskEvent·경보를 만들지 않는다
    assert core.pending_site_acks() == []
    core.end_test_alert()
    assert sound.calls[-1] == ("stop", None)
    assert any("시험 경보 끝 (소리 재생: 시작됨)" in line for line in render_lines(core.screen_state()))


def test_real_alert_overrides_test_sound(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    core.start_test_alert()
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    assert sound.playing.name == "HIGH"
    core.end_test_alert()
    assert sound.playing.name == "HIGH"  # 시험이 끝나도 실제 경보 소리는 계속


# ---- 소리 실패 -------------------------------------------------------------------------------------------------


def test_startup_device_missing_is_shown_loudly(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    sound.fail_check = True
    assert core.start_test_alert() is False
    state = core.screen_state()
    assert state.sound_fault
    assert render_lines(state)[0].startswith("!!! 소리 장치 오류")
    assert not state.test.sound_started
    core.tick()  # 재생 호출이 돼도 장치 확인이 실패하는 동안은 오류 표시를 내리지 않는다
    assert core.screen_state().sound_fault
    sound.fail_check = False
    core.tick()
    assert core.screen_state().sound_fault is None


def test_start_failure_keeps_screen_alarm_and_retries(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    sound.fail_start = True
    eid = uuid7()
    assert core.handle(local_alert(eid, 1, "RAISED")).outcome is Outcome.APPLIED
    state = core.screen_state()
    assert state.sound_fault and state.playing is None
    assert [a.event_id for a in state.alerts] == [eid]  # 화면 경보는 그대로
    assert core.alarm_status()["sounding_event_ids"] == [eid]
    core.tick()  # 아직 실패
    assert core.screen_state().sound_fault
    sound.fail_start = False
    core.tick()  # 다시 시도해 성공하면 오류 표시를 내린다
    state = core.screen_state()
    assert state.sound_fault is None and state.playing == "CRITICAL"


def test_backend_unexpected_exception_does_not_break_core(core: SiteAlertCore) -> None:
    class Broken(FakeSoundBackend):
        def start(self, pattern):
            raise OSError("장치 사라짐")

    broken = SiteAlertCore(Broken(), laptop_id="laptop-01")
    eid = uuid7()
    assert broken.handle(local_alert(eid, 1, "RAISED")).outcome is Outcome.APPLIED
    assert broken.screen_state().sound_fault == "장치 사라짐"


def test_listener_error_does_not_break_core(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    def bad(_state):
        raise RuntimeError("화면 오류")

    core.add_listener(bad)
    assert core.handle(local_alert(uuid7(), 1, "RAISED")).outcome is Outcome.APPLIED
    assert sound.playing is not None


# ---- 지연 기록 -------------------------------------------------------------------------------------------------


def test_latency_recorded_from_receive_to_start(core: SiteAlertCore, clock) -> None:
    received = clock.monotonic()
    clock.advance(0.004)
    result = core.handle(local_alert(uuid7(), 1, "RAISED"), received)
    assert result.sound_start_latency_s == pytest.approx(0.004, abs=1e-6)
    assert list(core.latency_samples_s) == [pytest.approx(0.004, abs=1e-6)]
