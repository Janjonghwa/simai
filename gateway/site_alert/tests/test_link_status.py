"""장애 상태 기계(link_status)와 장애별 현장 표시: 단절 시나리오마다 5초 안에 표시가 바뀌는지(G3), 반례(동시 단절,
깜빡이는 연결, 틀린 시계, 늦은 메시지, 모르는 상태를 정상으로 보지 않음).

가짜 시계로 0.5초씩 진행한다(조립 app 의 tick 주기와 같다). 실제 시간 종단 시험은 test_heartbeat_e2e.py.
"""

from __future__ import annotations

import pytest
from link_helpers import CAMS, Harness, health, healthy_harness, relay

from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.display import render_lines
from simai_site_alert.link_status import (
    CONTROL_HALTED,
    NORMAL,
    REMOTE_FAULT,
    Reason,
    SiteLinkMonitor,
)

G3_SITE_S = 5.0


@pytest.fixture
def h():
    harness = healthy_harness()
    yield harness
    harness.close()


# ---- 시작: 모르면 정상이 아니다 ---------------------------------------------------------------------------------


def test_startup_is_halted_until_everything_is_confirmed() -> None:
    h = Harness()
    try:
        h.app.publish_status()
        assert h.display == CONTROL_HALTED
        assert Reason.CONTROL_NOT_CONNECTED in h.site.tier0_reasons and Reason.VIDEO_UNKNOWN in h.site.tier0_reasons
        hb = h.app.build_heartbeat(include_ec2=True)["payload"]
        assert hb["site_display"] == CONTROL_HALTED
        assert hb["tier0"] == {"control": "DISCONNECTED", "last_message_at": None, "video_cameras": []}
        assert hb["ec2"] == {"state": "DISCONNECTED", "last_response_at": None}
        assert "cameras" not in hb  # 중계 보고가 없으면 비운다
        h.connect()
        # Tier 0·영상이 정상이어도 EC2 응답을 받기 전에는 정상이 아니다(원격 장애로 본다)
        h.ec2_on = False
        h.seconds_until(REMOTE_FAULT)
        assert h.display == REMOTE_FAULT
        h.ec2_on = True
        assert h.seconds_until(NORMAL) >= h.monitor.recover_s
    finally:
        h.close()


def test_health_without_system_health_is_not_normal() -> None:
    """system_health 가 없거나 모르는 값이면 판정 불가로 본다(모르면 정상으로 보지 않음)."""
    h = Harness()
    try:
        h.system_health = None
        h.connect()
        h.run(6)
        assert set(d for _, d in h.history) == {CONTROL_HALTED}
        assert Reason.TIER0_UNSAFE in h.site.tier0_reasons
        h.system_health = "SOMETHING_NEW"
        h.run(3)
        assert h.display == CONTROL_HALTED
    finally:
        h.close()


def test_health_with_null_t_latest_global_is_stalled() -> None:
    """처리된 유효 프레임이 없다(t_latest_global null): 처리 진행을 확인할 수 없으니 관제 중단."""
    h = Harness()
    try:
        h.connect()
        for _ in range(8):
            h.clock.advance(0.5)
            h.channel.deliver(health(None))
            h.app.on_relay_report(relay(), h.now)
            h.app._on_ec2_result(True, h.now, h.clock.now_ms())  # noqa: SLF001
            h.app._monitor_tick()  # noqa: SLF001
            assert h.display == CONTROL_HALTED
        assert Reason.PROCESSING_STALLED in h.site.tier0_reasons
        assert h.site.control_link == "CONNECTED"  # health 는 받고 있다(계약 tier0.control 정의)
    finally:
        h.close()


# ---- 단절 시나리오별 표시 (명세 3.10 표, 6.5, G3 현장 5초) -----------------------------------------------------------


def test_tier0_silent_with_open_socket_halts_within_5s_and_resets_channel(h: Harness) -> None:
    """반쯤 열린 연결: 소켓은 열린 채 health 가 끊긴다 → 3회 누락으로 관제 중단, 제어 채널을 끊고 다시 붙는다(D-G3-17)."""
    h.health_on = False
    waited = h.seconds_until(CONTROL_HALTED)
    assert h.now - h.last_health_at <= G3_SITE_S
    assert waited <= 3.5
    assert Reason.HEALTH_MISSING in h.site.tier0_reasons or Reason.CONTROL_NOT_CONNECTED in h.site.tier0_reasons
    h.run(1)
    assert h.channel.resets == 1  # 연결당 한 번만
    assert h.display == CONTROL_HALTED


def test_processing_stall_halts_within_5s_even_though_health_arrives(h: Harness) -> None:
    """health 는 오는데 t_latest_global 이 그대로: 소켓이 아니라 처리 진행으로 판정한다(명세 3.2)."""
    h.progress_on = False
    last_progress = h.now
    h.seconds_until(CONTROL_HALTED)
    assert h.now - last_progress <= G3_SITE_S
    assert h.site.tier0_reasons == (Reason.PROCESSING_STALLED,)
    hb = h.app.build_heartbeat(include_ec2=False)["payload"]
    assert hb["tier0"]["control"] == "CONNECTED" and hb["site_display"] == CONTROL_HALTED
    assert h.channel.resets == 0  # health 는 오므로 연결을 끊지 않는다


def test_tier0_reports_unsafe_unknown_halts_immediately(h: Harness) -> None:
    h.system_health = "UNSAFE_UNKNOWN"
    assert h.seconds_until(CONTROL_HALTED) <= 1.0
    assert h.site.tier0_reasons == (Reason.TIER0_UNSAFE,)


def test_degraded_tier0_is_still_normal(h: Harness) -> None:
    h.system_health = "DEGRADED"
    assert set(h.run(5)) == {NORMAL}


def test_control_disconnect_halts_immediately(h: Harness) -> None:
    h.channel.set_connected(False)
    assert h.display == CONTROL_HALTED  # 다음 tick 을 기다리지 않는다
    assert h.site.tier0_reasons[0] == Reason.CONTROL_NOT_CONNECTED


def test_video_relay_down_halts_immediately(h: Harness) -> None:
    h.video = ()
    assert h.seconds_until(CONTROL_HALTED) <= 1.0
    assert h.site.tier0_reasons == (Reason.VIDEO_DOWN,)
    assert "영상 중계 끊김" in render_lines(h.core.screen_state())[0]


def test_video_relay_silent_halts_within_5s(h: Harness) -> None:
    h.relay_on = False
    h.seconds_until(CONTROL_HALTED)
    assert h.now - h.last_relay_at <= G3_SITE_S
    assert h.site.tier0_reasons == (Reason.VIDEO_UNKNOWN,)
    hb = h.app.build_heartbeat(include_ec2=True)["payload"]
    assert hb["tier0"]["video_cameras"] == [] and "cameras" not in hb  # 모르면 비운다


def test_partial_video_is_not_halt(h: Harness) -> None:
    """카메라 일부만 끊김은 '유효 관측 중단'이 아니다(구역 감시 저하는 Tier 0 R7 이 다룬다)."""
    h.video = CAMS[:3]
    assert set(h.run(4)) == {NORMAL}
    assert "영상: 3/4대" in render_lines(h.core.screen_state())[1]


def test_ec2_only_down_is_remote_fault_and_site_alarm_keeps_working(h: Harness) -> None:
    """EC2 만 끊김: '원격 관제 장애', 관제 중단으로 잘못 표시하지 않고 현장 경보는 계속 울린다(D-G3-12)."""
    h.ec2_on = False
    h.seconds_until(REMOTE_FAULT)
    assert h.now - h.last_ec2_ok_at <= G3_SITE_S
    eid = uuid7()
    h.channel.deliver(local_alert(eid, 1, "RAISED", now_ms=h.clock.now_ms()))
    assert h.sound.playing.name == "CRITICAL" and eid in h.core.active_alerts()
    h.run(10)
    assert CONTROL_HALTED not in {d for _, d in h.history[-20:]}
    assert h.display == REMOTE_FAULT and h.sound.playing.name == "CRITICAL"
    lines = render_lines(h.core.screen_state())
    assert lines[0].startswith("원격 관제 장애 - 현장 경보는 동작 중") and "EC2: 끊김" in lines[1]
    hb = h.app.build_heartbeat(include_ec2=True)["payload"]
    assert hb["tier0"]["control"] == "CONNECTED" and hb["site_display"] == REMOTE_FAULT
    assert hb["ec2"]["state"] == "DISCONNECTED" and hb["alarm"]["sounding_event_ids"] == [eid]


def test_single_ec2_failure_is_not_a_fault(h: Harness) -> None:
    """실패 한두 번(3회 누락 전)으로는 원격 장애로 바꾸지 않는다."""
    h.ec2_on = False
    h.run(2)  # 실패 한 번
    assert h.display == NORMAL
    h.ec2_on = True
    assert set(h.run(5)) == {NORMAL}


# ---- 반례: 동시 단절, 깜빡임, 늦은 메시지, 틀린 시계 ----------------------------------------------------------------


def test_simultaneous_tier0_and_ec2_loss_shows_control_halted(h: Harness) -> None:
    h.health_on = False
    h.ec2_on = False
    h.seconds_until(CONTROL_HALTED)
    h.run(5)
    assert h.display == CONTROL_HALTED  # 관제 중단이 먼저
    assert h.site.ec2_reasons == (Reason.EC2_UNREACHABLE,)
    assert "EC2: 끊김" in render_lines(h.core.screen_state())[1]
    # Tier 0 만 돌아오면 원격 장애로 내려간다(정상으로 건너뛰지 않음)
    h.health_on = True
    h.seconds_until(REMOTE_FAULT)
    assert h.display == REMOTE_FAULT


def test_flapping_control_channel_never_shows_normal(h: Harness) -> None:
    """제어 채널이 0.5~1초마다 끊겼다 붙고 붙을 때마다 health 를 받아도 '정상'이 깜빡이지 않는다."""
    seen = []
    last_flap = h.now
    for i in range(30):
        if i % 2 == 0:
            h.channel.set_connected(False)
            seen.append(h.display)
            h.channel.set_connected(True)
            h.send_health()  # 재접속 직후 health
            last_flap = h.now
        seen.append(h.step())
    assert NORMAL not in seen[1:]
    # 안정되면 마지막 끊김부터 recover_s 뒤에 정상
    h.seconds_until(NORMAL)
    assert h.now - last_flap >= h.monitor.recover_s


def test_flapping_video_between_ticks_is_not_missed() -> None:
    """평가(0.5초) 사이에 '영상 0대' 보고가 지나가도 정상 복귀를 처음부터 다시 기다린다."""
    h = healthy_harness()
    try:
        seen = []
        for _ in range(12):
            h.app.on_relay_report(relay(()), h.now)  # 영상 끊김 보고
            h.app.on_relay_report(relay(), h.now)  # 바로 다시 연결 보고(평가 전에 덮어씀)
            seen.append(h.step())
        assert NORMAL not in seen[1:]
    finally:
        h.close()


def test_flapping_unsafe_between_ticks_is_not_missed(h: Harness) -> None:
    seen = []
    for _ in range(12):
        h.clock.advance(0.1)
        h.t_latest += 100
        h.channel.deliver(health(h.t_latest, system_health="UNSAFE_UNKNOWN"))
        h.t_latest += 100
        h.channel.deliver(health(h.t_latest, system_health="NOMINAL"))
        seen.append(h.step())
    assert NORMAL not in seen


def test_message_after_disconnect_does_not_revive(h: Harness) -> None:
    """끊긴 뒤 늦게 처리된 health 로 정상을 만들지 않는다. 재접속 뒤 새 health 를 받아야 한다."""
    h.auto_reconnect = False
    h.channel.set_connected(False)
    h.monitor.on_tier0_message(h.now, h.clock.now_ms(), None)
    h.health_on = False
    h.run(5)
    assert h.display == CONTROL_HALTED
    h.channel.set_connected(True)
    h.run(1.5)
    assert h.display == CONTROL_HALTED  # health 없이 연결만으로는 정상이 아니다
    h.health_on = True
    h.seconds_until(NORMAL)


def test_quick_reconnect_needs_fresh_health(h: Harness) -> None:
    """끊겼다 곧바로 다시 붙어도, 끊기기 전에 받은 health 로 정상을 만들지 않는다(재접속 뒤 새 health 가 와야 한다).
    끊긴 동안 늦게 처리된 메시지도 세지 않는다."""
    h.health_on = False
    h.channel.set_connected(False)
    h.monitor.on_tier0_message(h.now, h.clock.now_ms(), None)  # 끊긴 뒤 늦게 처리된 메시지
    h.channel.set_connected(True)
    seen = h.run(2.5)  # 끊기기 전 health 는 아직 3초가 안 됐다
    assert set(seen) == {CONTROL_HALTED}
    assert Reason.HEALTH_MISSING in h.site.tier0_reasons
    h.health_on = True
    h.seconds_until(NORMAL)


def test_wall_clock_jump_does_not_change_decisions(h: Harness) -> None:
    """노트북 벽시계가 1시간 뒤·앞으로 뛰어도 판단은 단조 시계로 한다. last_message_at 만 벽시계를 따른다."""
    for offset in (-3_600_000, +3_600_000, 0):
        h.clock.wall_offset_ms = offset
        assert set(h.run(4)) == {NORMAL}
        hb = h.app.build_heartbeat(include_ec2=True)["payload"]
        assert abs(hb["tier0"]["last_message_at"] - h.clock.now_ms()) <= 1500


def test_t_latest_global_step_back_is_not_a_stall(h: Harness) -> None:
    """노트북 시계를 되돌려 촬영 시각(t_latest_global)이 1시간 줄어도, 그 뒤 다시 늘면 진행으로 본다(정체로 오판하지 않음)."""
    h.t_latest -= 3_600_000
    assert set(h.run(6)) == {NORMAL}


def test_t_latest_global_step_back_without_progress_still_stalls(h: Harness) -> None:
    """줄어든 값 자체는 진행이 아니다: 줄어든 뒤 그대로면 정체로 본다."""
    h.t_latest -= 3_600_000
    h.progress_on = False
    h.seconds_until(CONTROL_HALTED, limit_s=4.0)


def test_recovery_requires_stable_condition(h: Harness) -> None:
    h.health_on = False
    h.seconds_until(CONTROL_HALTED)
    h.health_on = True
    t = h.seconds_until(NORMAL)
    assert t >= h.monitor.recover_s
    assert "복구 확인 중" in " ".join(render_lines(h.core.screen_state())) or h.display == NORMAL


def test_ai_delay_is_never_produced(h: Harness) -> None:
    """분석 워커 상태는 노트북에 오지 않는다. AI_DELAY 를 지어내지 않는다."""
    assert "AI_DELAY" not in {d for _, d in h.history}


def test_monitor_rejects_bad_timings() -> None:
    with pytest.raises(ValueError):
        SiteLinkMonitor(timeout_s=0)
