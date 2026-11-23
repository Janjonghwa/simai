"""현장 경보 재동기화 (명세 3.10, FR-137, 부록 D-G3-16, S15P31A101-161).

노트북은 Tier 0 가 (재)접속 직후와 1Hz health 마다 보내는 활성 현장 경보 목록(active_site_alerts)에 맞춰 소리·표시를
켜고 끈다. 제어 연결 단절·재접속, 메시지 유실(local_alert·alarm_stop·해소), 순서 뒤바뀜, 상향 전 확인의 늦은
alarm_stop, as_of 전후 경합 뒤에도 노트북 경보 상태가 Tier 0 활성 경보와 같아야 한다.

뒤쪽의 무작위 시험은 Tier 0 생명주기 규칙(ai/src/simai_ai/risk: 발생·상향·하향·확인·해소·재개, 지금 경보 단계 이후
상태를 대상으로 한 확인만 소리를 멈춤)을 흉내 내는 작은 모형으로 메시지를 만들고, 무작위로 유실·단절을 넣는다.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field

import pytest

from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.clock import FakeClock
from simai_site_alert.core import Outcome, SiteAlertCore
from simai_site_alert.sound import FakeSoundBackend

T = 1_760_342_400_000


def la(event_id: str, seq: int, lifecycle: str = "UPDATED", *, sent_at: int, **kw) -> dict:
    return local_alert(event_id, seq, lifecycle, now_ms=sent_at, **kw)


def stop(event_id: str, target: int | None, *, sent_at: int, ack_id: str | None = None, channel: str = "CONTROL_ROOM",
         role: str = "ADMIN", actor: str = "user-0003") -> dict:
    payload: dict = {
        "event_id": event_id,
        "ack": {"ack_id": ack_id or str(uuid.uuid4()), "channel": channel, "actor_role": role, "actor_id": actor,
                "acked_at": sent_at - 10},
    }
    if target is not None:
        payload["event_sequence"] = target
    return {"schema_version": "2.1.0", "msg_type": "alarm_stop", "msg_id": str(uuid.uuid4()), "sent_at": sent_at,
            "source_id": "tier0-01", "payload": payload}


def item(event_id: str, seq: int, *, acknowledged: bool = False, severity: str = "CRITICAL", **kw) -> dict:
    return {"event_id": event_id, "event_sequence": seq, "source": "LIVE", "acknowledged": acknowledged,
            "severity": severity, "summary": kw.pop("summary", f"R2 {severity}"), "rule_id": "R2", "zone_id": "z-04",
            **kw}


def health(items: list[dict] | None, *, as_of: int | None, sent_at: int | None = None) -> dict:
    payload: dict = {"system_health": "NOMINAL", "cameras": [], "pipeline_lag_ms": 100,
                     "calibration_state": "CALIBRATED"}
    if items is not None:
        payload["active_site_alerts"] = items
    if as_of is not None:
        payload["active_site_alerts_as_of"] = as_of
    return {"schema_version": "2.1.0", "msg_type": "health", "msg_id": str(uuid.uuid4()),
            "sent_at": sent_at if sent_at is not None else (as_of or T) + 1, "source_id": "tier0-01",
            "payload": payload}


def state(core: SiteAlertCore) -> dict[str, tuple[int, str, bool]]:
    return {e: (a.event_sequence, a.severity, a.acknowledged) for e, a in core.active_alerts().items()}


# ---- 목록에 맞춰 켜고 끔 ----------------------------------------------------------------------------------------


def test_missed_local_alert_is_shown_and_sounds(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    assert core.handle(health([item(eid, 2, severity="HIGH", summary="R1 진입")], as_of=T)).outcome is Outcome.APPLIED
    assert state(core) == {eid: (2, "HIGH", False)}
    assert core.active_alerts()[eid].summary == "R1 진입" and sound.playing.name == "HIGH"


def test_listed_acknowledged_only_displays(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(health([item(eid, 3, acknowledged=True)], as_of=T))
    assert state(core) == {eid: (3, "CRITICAL", True)} and sound.calls == []


def test_missed_alarm_stop_is_applied_from_list(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(health([item(eid, 2, acknowledged=True)], as_of=T + 500))  # alarm_stop 유실, ACK 전이 seq 2
    assert state(core) == {eid: (2, "CRITICAL", True)} and sound.playing is None


def test_missed_resolved_is_cleared_from_list(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    a, b = uuid7(), uuid7()
    core.handle(la(a, 1, "RAISED", sent_at=T))
    core.handle(la(b, 1, "RAISED", severity="HIGH", sent_at=T + 1))
    core.handle(health([item(b, 1, severity="HIGH")], as_of=T + 500))  # a 의 RESOLVED 유실
    assert set(core.active_alerts()) == {b} and sound.playing.name == "HIGH"
    core.handle(health([], as_of=T + 1500))  # 빈 목록 = 활성 현장 경보 없음
    assert core.active_alerts() == {} and sound.playing is None


def test_cleared_event_not_resurrected_by_late_lower_sequence(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(la(eid, 3, "RESOLVED", sent_at=T + 100))
    core.handle(health([item(eid, 2)], as_of=T + 50, sent_at=T + 150))  # 해소 전 목록(어긋난 송신 측)
    assert core.active_alerts() == {}


def test_resumed_after_missed_resolve_alarms_again(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """확인·해소·재개를 모두 놓쳐도 목록의 높은 sequence + acknowledged=false 로 다시 울린다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(stop(eid, 1, sent_at=T + 100))
    assert sound.playing is None
    core.handle(health([item(eid, 5, acknowledged=False)], as_of=T + 5000))  # 해소(3)·재개(4)·갱신(5) 놓침
    assert state(core) == {eid: (5, "CRITICAL", False)} and sound.playing.name == "CRITICAL"
    # 재개 전 상태에 대한 늦은 정지는 다시 울린 경보를 멈추지 않는다
    assert core.handle(stop(eid, 2, sent_at=T + 5100)).outcome is Outcome.STALE
    assert sound.playing.name == "CRITICAL"


def test_missed_escalation_from_list_unacks(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", severity="HIGH", sent_at=T))
    core.handle(stop(eid, 1, sent_at=T + 100))
    core.handle(health([item(eid, 3, severity="CRITICAL")], as_of=T + 1000))  # 상향 local_alert 유실
    assert state(core) == {eid: (3, "CRITICAL", False)} and sound.playing.name == "CRITICAL"
    assert core.active_alerts()[eid].alarm_sequence == 3


def test_health_without_list_never_clears(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """active_site_alerts 가 없는 health(이전 송신 측)로는 경보를 끄지 않는다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    assert core.handle(health(None, as_of=None, sent_at=T + 1000)).outcome is Outcome.IGNORED
    assert eid in core.active_alerts() and sound.playing.name == "CRITICAL"


def test_lower_sequence_in_list_does_not_roll_back(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(la(eid, 4, "UPDATED", severity="CRITICAL", summary="최신", sent_at=T + 100))
    core.handle(health([item(eid, 3, severity="HIGH", summary="옛것")], as_of=T + 200))
    assert state(core) == {eid: (4, "CRITICAL", False)} and core.active_alerts()[eid].summary == "최신"


def test_duplicate_list_is_idempotent(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    msg = health([item(eid, 2)], as_of=T)
    core.handle(msg)
    core.handle(msg)  # 같은 health 를 다시 받음
    core.handle(health([item(eid, 2)], as_of=T + 1000))
    assert state(core) == {eid: (2, "CRITICAL", False)} and sound.calls == [("start", "CRITICAL")]


def test_duplicate_event_id_in_list_uses_highest_sequence(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(health([item(eid, 2, acknowledged=False), item(eid, 4, acknowledged=True)], as_of=T))
    assert state(core) == {eid: (4, "CRITICAL", True)}


def test_non_live_or_caution_item_never_alarms_nor_clears(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """LIVE 아닌·CAUTION 항목은 켜지 않는다. 그 event_id 의 기존 경보도 이 목록으로 끄지 않는다(판단 못 함)."""
    shown, replay, caution = uuid7(), uuid7(), uuid7()
    core.handle(la(shown, 1, "RAISED", sent_at=T))
    core.handle(health([item(shown, 1, source="REPLAY"), item(replay, 1, source="WHAT_IF"),
                        item(caution, 1, severity="CAUTION")], as_of=T + 500))
    assert set(core.active_alerts()) == {shown} and sound.playing.name == "CRITICAL"


def test_unidentifiable_item_blocks_clearing(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(health([{"event_sequence": 1, "source": "LIVE"}], as_of=T + 500))
    assert eid in core.active_alerts()


def test_fall_resync_same_event_does_not_restart(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """낙상(R4) 새 이벤트는 같은 패턴이어도 다시 울리지만(S15P31A101-863), 재접속 목록의 같은 event_id 는 다시 울리지 않는다."""
    entry, fall = uuid7(), uuid7()
    core.handle(la(entry, 1, "RAISED", sent_at=T))
    core.handle(health([item(entry, 1), item(fall, 1, rule_id="R4")], as_of=T + 10))  # 놓친 낙상: 목록으로 처음 봄
    assert sound.calls == [("start", "CRITICAL"), ("stop", None), ("start", "CRITICAL")]
    core.handle(health([item(entry, 1), item(fall, 2, rule_id="R4")], as_of=T + 20))  # 재동기화(같은 event_id)
    core.handle(la(fall, 3, sent_at=T + 30, rule_id="R4"))  # 재전송
    assert sound.calls == [("start", "CRITICAL"), ("stop", None), ("start", "CRITICAL")]


# ---- 상향 전 확인의 늦은 alarm_stop ----------------------------------------------------------------------------


def test_late_alarm_stop_for_pre_escalation_state_keeps_sounding(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """-90 남은 일: HIGH 상태에서 누른 현장 확인이 상향 뒤에 기록돼 온 alarm_stop 은 CRITICAL 소리를 멈추지 않는다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", severity="HIGH", sent_at=T))
    envelope = core.site_ack(eid)
    assert envelope["payload"]["event_sequence"] == 1
    core.handle(la(eid, 2, "UPDATED", severity="CRITICAL", sent_at=T + 100))  # 상향: 확인을 풀고 다시 울림
    assert sound.playing.name == "CRITICAL"
    ack_id = envelope["payload"]["ack"]["ack_id"]
    late = stop(eid, 1, sent_at=T + 200, ack_id=ack_id, channel="SITE_BUTTON", role="SITE", actor="laptop-01")
    result = core.handle(late)
    assert result.outcome is Outcome.STALE
    assert sound.playing.name == "CRITICAL" and not core.active_alerts()[eid].acknowledged
    # Tier 0 목록도 확인 전(상향 전 확인은 세지 않음)이라 같은 상태가 유지된다
    core.handle(health([item(eid, 3, acknowledged=False)], as_of=T + 1000))
    assert sound.playing.name == "CRITICAL"
    # 상향된 경보를 다시 확인하면 멈춘다
    core.handle(stop(eid, 3, sent_at=T + 1100))
    assert sound.playing is None


def test_alarm_stop_after_non_escalating_updates_still_stops(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """대상 sequence 가 낮아도 그 뒤 등급 상향이 없었으면(같은 등급 갱신) 멈춘다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(la(eid, 3, "UPDATED", sent_at=T + 100))
    assert core.handle(stop(eid, 1, sent_at=T + 200)).outcome is Outcome.APPLIED
    assert sound.playing is None


def test_alarm_stop_without_target_stops_current(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", severity="HIGH", sent_at=T))
    core.handle(la(eid, 2, "UPDATED", severity="CRITICAL", sent_at=T + 100))
    assert core.handle(stop(eid, None, sent_at=T + 200)).outcome is Outcome.APPLIED  # 이전 송신 측
    assert sound.playing is None


def test_malformed_alarm_stop_target_is_rejected(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    assert core.handle(stop(eid, 0, sent_at=T + 100)).outcome is Outcome.REJECTED
    assert sound.playing.name == "CRITICAL"


def test_early_alarm_stop_with_older_target_does_not_ack_escalated_first_alert(core: SiteAlertCore,
                                                                              sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(stop(eid, 1, sent_at=T))  # local_alert 보다 먼저 옴(대상 1)
    core.handle(la(eid, 3, "UPDATED", sent_at=T + 100))  # RAISED·상향을 놓치고 처음 받은 것이 3
    assert sound.playing.name == "CRITICAL"


def test_pending_site_ack_kept_for_new_level_when_old_alarm_stop_arrives(core: SiteAlertCore) -> None:
    """상향 뒤 다시 누른 현장 확인은, 다른 채널의 상향 전 확인 alarm_stop 이 와도 재전송 대기에서 빠지지 않는다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", severity="HIGH", sent_at=T))
    core.handle(la(eid, 2, "UPDATED", severity="CRITICAL", sent_at=T + 100))
    core.site_ack(eid)
    assert core.handle(stop(eid, 1, sent_at=T + 200)).outcome is Outcome.STALE
    assert len(core.pending_site_acks()) == 1


# ---- 경합: as_of 전후 ------------------------------------------------------------------------------------------


def test_local_alert_sent_after_as_of_not_cleared(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """목록을 만든 뒤(as_of) 보낸 local_alert 가 먼저 도착해도, 그 목록에 없다는 이유로 끄지 않는다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T + 10))
    core.handle(health([], as_of=T + 5, sent_at=T + 20))
    assert eid in core.active_alerts() and sound.playing.name == "CRITICAL"
    core.handle(health([], as_of=T + 1005))  # 다음 목록에서도 없으면 해소로 본다
    assert core.active_alerts() == {} and sound.playing is None


def test_local_alert_sent_at_same_ms_as_as_of_not_cleared(core: SiteAlertCore) -> None:
    """경계: as_of 와 같은 ms 에 보낸 local_alert 는 목록에 반영됐는지 모르므로 끄지 않는다(리뷰 R4)."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T + 5))
    core.handle(health([], as_of=T + 5, sent_at=T + 6))
    assert eid in core.active_alerts()
    core.handle(health([], as_of=T + 6, sent_at=T + 7))  # 1 ms 뒤 목록에는 반영됐어야 한다
    assert core.active_alerts() == {}


def test_alarm_stop_sent_at_same_ms_as_as_of_does_not_keep_ack(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """경계: 확인 유지는 as_of 보다 뒤에 보낸 것만. 같은 ms 인데 목록이 미확인이면 소리를 다시 켠다(안전한 쪽)."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(stop(eid, 1, sent_at=T + 100))
    core.handle(health([item(eid, 1, acknowledged=False)], as_of=T + 99, sent_at=T + 101))
    assert core.active_alerts()[eid].acknowledged and sound.playing is None  # 1 ms 뒤에 보낸 확인은 유지
    core.handle(health([item(eid, 1, acknowledged=False)], as_of=T + 100, sent_at=T + 102))
    assert not core.active_alerts()[eid].acknowledged and sound.playing.name == "CRITICAL"


def test_early_alarm_stop_at_same_ms_as_as_of_not_applied(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """경계: local_alert 전에 온 alarm_stop 은 as_of 보다 뒤에 보낸 것만 목록의 미확인보다 앞선다."""
    same, later = uuid7(), uuid7()
    core.handle(stop(same, 1, sent_at=T + 100))
    core.handle(stop(later, 1, sent_at=T + 101))
    core.handle(health([item(same, 1), item(later, 1)], as_of=T + 100, sent_at=T + 102))
    assert not core.active_alerts()[same].acknowledged and core.active_alerts()[later].acknowledged
    assert sound.playing.name == "CRITICAL"


def test_local_alert_sent_before_as_of_cleared(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(health([], as_of=T + 5))
    assert core.active_alerts() == {}


def test_alarm_stop_sent_after_as_of_keeps_ack(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(stop(eid, 1, sent_at=T + 110))
    core.handle(health([item(eid, 1, acknowledged=False)], as_of=T + 100, sent_at=T + 120))
    assert core.active_alerts()[eid].acknowledged and sound.playing is None
    core.handle(health([item(eid, 2, acknowledged=True)], as_of=T + 1100))
    assert core.active_alerts()[eid].acknowledged


def test_ack_known_before_as_of_but_listed_unacked_sounds_again(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """as_of 전에 받은 확인인데 목록이 false 면 Tier 0 기준으로 지금 단계의 확인이 없다(놓친 재개 등). 다시 울린다."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.handle(stop(eid, 1, sent_at=T + 50))
    core.handle(health([item(eid, 4, acknowledged=False)], as_of=T + 100))
    assert not core.active_alerts()[eid].acknowledged and sound.playing.name == "CRITICAL"


def test_pending_site_ack_not_undone_by_list(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """현장 확인을 Tier 0 가 아직 기록하지 않은 동안 목록이 false 여도 소리를 다시 켜지 않는다(누른 즉시 정지)."""
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", sent_at=T))
    core.site_ack(eid)
    core.handle(health([item(eid, 1, acknowledged=False)], as_of=T + 1000))
    assert core.active_alerts()[eid].acknowledged and sound.playing is None
    assert len(core.pending_site_acks()) == 1
    core.handle(health([item(eid, 2, acknowledged=True)], as_of=T + 2000))  # alarm_stop 은 유실, 목록으로 기록 확인
    alert = core.active_alerts()[eid]
    assert core.pending_site_acks() == [] and alert.acknowledged and alert.ack is None  # 누가 했는지는 모름


def test_pending_site_ack_dropped_when_list_shows_escalation(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(la(eid, 1, "RAISED", severity="HIGH", sent_at=T))
    core.site_ack(eid)
    core.handle(health([item(eid, 2, severity="CRITICAL", acknowledged=False)], as_of=T + 1000))
    assert sound.playing.name == "CRITICAL" and core.pending_site_acks() == []


# ---- 단절·재접속 ----------------------------------------------------------------------------------------------


def test_reconnect_resync_after_outage(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    """끊긴 동안 새 경보 발생·기존 경보 해소·다른 경보 확인 → 재접속 직후 목록 하나로 맞춘다."""
    gone, acked, new = uuid7(), uuid7(), uuid7()
    core.set_control_connected(True)
    core.handle(la(gone, 1, "RAISED", sent_at=T))
    core.handle(la(acked, 1, "RAISED", severity="HIGH", sent_at=T + 1))
    core.set_control_connected(False)  # 이 사이의 local_alert·alarm_stop·RESOLVED 는 모두 유실
    assert set(core.active_alerts()) == {gone, acked}  # 끊겨도 경보는 그대로(모르면 끄지 않는다)
    core.set_control_connected(True)
    core.handle(health([item(acked, 2, severity="HIGH", acknowledged=True), item(new, 1, severity="CRITICAL")],
                       as_of=T + 30_000))
    assert state(core) == {acked: (2, "HIGH", True), new: (1, "CRITICAL", False)}
    assert sound.playing.name == "CRITICAL"
    assert core.alarm_status() == {"sounding_event_ids": [new], "displayed_event_ids": [acked, new]}


# ---- 무작위: Tier 0 모형 + 유실·단절 ----------------------------------------------------------------------------


_RANK = {"HIGH": 1, "CRITICAL": 2}


@dataclass
class _Event:
    seq: int
    severity: str
    alarm_seq: int
    ack_targets: list[int] = field(default_factory=list)

    @property
    def site_acknowledged(self) -> bool:
        return any(t >= self.alarm_seq for t in self.ack_targets)


class Tier0Model:
    """Tier 0 생명주기 규칙의 작은 모형(ai/src/simai_ai/risk/lifecycle.py, site_alerts.py 와 같은 규칙)."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.t = T
        self.events: dict[str, _Event] = {}
        self.resolved: dict[str, _Event] = {}
        self.seen_ack_ids: set[str] = set()

    def now(self) -> int:
        self.t += self.rng.randint(1, 50)
        return self.t

    def raise_(self) -> list[dict]:
        eid = uuid7(self.t)
        sev = "HIGH" if self.rng.random() < 0.7 else "CRITICAL"  # 상향이 자주 일어나게 HIGH 를 많이
        self.events[eid] = _Event(1, sev, 1)
        return [la(eid, 1, "RAISED", severity=sev, sent_at=self.now())]

    def escalate(self, eid: str) -> list[dict]:
        e = self.events[eid]
        if e.severity == "CRITICAL":
            return []
        e.seq += 1
        e.severity, e.alarm_seq = "CRITICAL", e.seq
        return [la(eid, e.seq, severity="CRITICAL", acknowledged=e.site_acknowledged, sent_at=self.now())]

    def downgrade(self, eid: str) -> list[dict]:
        e = self.events[eid]
        if e.severity == "HIGH":
            return []
        e.seq += 1
        e.severity = "HIGH"  # 하향은 현장 경보를 다시 보내지 않는다
        return []

    def update(self, eid: str) -> list[dict]:
        self.events[eid].seq += 1  # 수치 갱신 등 현장 경보를 보내지 않는 전이
        return []

    def ack(self, eid: str, target: int | None = None, ack_id: str | None = None) -> list[dict]:
        e = self.events[eid]
        target = e.seq if target is None else min(target, e.seq)
        e.ack_targets.append(target)
        e.seq += 1  # ACKNOWLEDGED 전이
        return [stop(eid, target, sent_at=self.now(), ack_id=ack_id)]

    def resolve(self, eid: str) -> list[dict]:
        e = self.events.pop(eid)
        e.seq += 1
        self.resolved[eid] = e
        return [la(eid, e.seq, "RESOLVED", severity=e.severity, acknowledged=e.site_acknowledged, sent_at=self.now())]

    def resume(self, eid: str) -> list[dict]:
        e = self.resolved.pop(eid)
        e.seq += 1
        e.alarm_seq, e.ack_targets = e.seq, []
        e.severity = self.rng.choice(["HIGH", "CRITICAL"])
        self.events[eid] = e
        return [la(eid, e.seq, severity=e.severity, sent_at=self.now())]

    def site_ack(self, envelope: dict) -> list[dict]:
        p = envelope["payload"]
        ack_id = p["ack"]["ack_id"]
        if ack_id in self.seen_ack_ids or p["event_id"] not in self.events:
            return []  # 같은 ack_id 는 한 번만 기록. 해소된 이벤트는 기록하지 않는다
        self.seen_ack_ids.add(ack_id)
        return self.ack(p["event_id"], p.get("event_sequence"), ack_id)

    def health(self) -> dict:
        as_of = self.now()
        items = [item(eid, e.seq, acknowledged=e.site_acknowledged, severity=e.severity)
                 for eid, e in sorted(self.events.items())]
        return health(items, as_of=as_of, sent_at=as_of)


def _assert_matches(core: SiteAlertCore, tier0: Tier0Model, context: str) -> None:
    laptop = core.active_alerts()
    assert set(laptop) == set(tier0.events), context
    pending = {env["payload"]["event_id"] for env in core.pending_site_acks()}
    for eid, e in tier0.events.items():
        a = laptop[eid]
        assert (a.event_sequence, a.severity) == (e.seq, e.severity), context
        if eid in pending and a.ack is not None and a.ack.local:
            assert a.acknowledged, context  # 누른 현장 확인은 Tier 0 기록 전에도 소리를 멈춘다
        else:
            assert a.acknowledged == e.site_acknowledged, context
    sounding = {eid for eid, a in laptop.items() if not a.acknowledged}
    expected = max((laptop[e].severity for e in sounding), key=_RANK.get, default=None)
    playing = core.screen_state().playing
    assert playing == expected, context


@pytest.mark.parametrize("seed", range(200))
def test_random_loss_and_outage_converge_on_every_list(seed: int) -> None:
    rng = random.Random(seed)
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01", clock=FakeClock(T))
    tier0 = Tier0Model(rng)
    connected = True
    loss = rng.choice([0.0, 0.2, 0.5])
    reorder = rng.random() < 0.5  # 메시지 하나를 다음 메시지(health 포함) 뒤로 늦게 넣는다(순서 뒤바뀜)
    log: list[str] = []
    held: list[dict] = []

    def release() -> None:
        while held:
            m = held.pop()
            core.handle(m)
            log.append(f"late {m['msg_type']} {m['payload'].get('event_sequence')}")

    def deliver(messages: list[dict]) -> None:
        for m in messages:
            if connected and rng.random() >= loss:
                if reorder and not held and rng.random() < 0.2:
                    held.append(m)
                    continue
                core.handle(m)
                log.append(f"recv {m['msg_type']} {m['payload'].get('event_sequence')}")
                release()
            else:
                log.append(f"lost {m['msg_type']}")

    inflight: list[dict] = []  # 보냈지만 Tier 0 가 아직 처리하지 않은 site_ack(그 사이 Tier 0 가 상향·재개할 수 있다)

    for _ in range(80):
        if inflight and rng.random() < 0.2:
            envelope = inflight.pop(0)
            deliver(tier0.site_ack(envelope))
            log.append("site_ack 처리")
        op = rng.random()
        active = sorted(tier0.events)
        if op < 0.15 or not active and op < 0.6:
            deliver(tier0.raise_())
        elif active and op < 0.25:
            deliver(tier0.escalate(rng.choice(active)))
        elif active and op < 0.32:
            deliver(tier0.downgrade(rng.choice(active)))
        elif active and op < 0.40:
            deliver(tier0.update(rng.choice(active)))
        elif active and op < 0.48:
            deliver(tier0.ack(rng.choice(active)))
        elif active and op < 0.55:
            deliver(tier0.resolve(rng.choice(active)))
        elif tier0.resolved and op < 0.60:
            deliver(tier0.resume(rng.choice(sorted(tier0.resolved))))
        elif op < 0.70:
            shown = sorted(core.active_alerts())
            if shown:
                envelope = core.site_ack(rng.choice(shown))
                if envelope is not None and connected:
                    inflight.append(envelope)
        elif op < 0.78:
            if connected:
                connected = False
                core.set_control_connected(False)
                held.clear()
                inflight.clear()  # 끊기면 처리되지 않은 site_ack 는 사라진다(재접속 때 노트북이 다시 보낸다)
                log.append("disconnect")
            else:
                connected = True
                core.set_control_connected(True)
                log.append("reconnect")
                h = tier0.health()  # 재접속 직후 health 를 바로 보낸다(유실 없음: 접속 직후 첫 메시지)
                core.handle(h)
                _assert_matches(core, tier0, f"seed={seed} 재접속 직후 {log[-12:]}")
                for envelope in core.pending_site_acks():  # 앱이 끊긴 동안 누른 현장 확인을 다시 보낸다
                    deliver(tier0.site_ack(envelope))
        else:
            h = tier0.health()
            if connected and rng.random() >= loss:
                core.handle(h)
                _assert_matches(core, tier0, f"seed={seed} {log[-12:]}")
                if held:
                    release()  # health 보다 먼저 보낸 메시지가 뒤늦게 도착해도 목록에 맞춘 상태를 되돌리지 않는다
                    _assert_matches(core, tier0, f"seed={seed} 늦은 도착 뒤 {log[-12:]}")
    # 마지막으로 접속해 목록 하나를 받으면 반드시 같아진다
    release()
    if not connected:
        core.set_control_connected(True)
        connected = True
        core.handle(tier0.health())
        for envelope in core.pending_site_acks():
            deliver(tier0.site_ack(envelope))
    core.handle(tier0.health())
    _assert_matches(core, tier0, f"seed={seed} 최종 {log[-12:]}")
