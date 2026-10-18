"""화면 문구(헤드리스)와 tkinter 창 기동."""

from __future__ import annotations

import io

import pytest

from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.display import TextDisplay, render_lines


def test_idle_and_connection_lines(core: SiteAlertCore) -> None:
    assert render_lines(core.screen_state()) == ["Tier 0 제어 채널: 연결 전", "활성 현장 경보 없음"]
    core.set_control_connected(False)
    assert "끊김" in render_lines(core.screen_state())[0]


def test_alert_line_contents(core: SiteAlertCore) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH", rule_id="R1", zone_id="z-02", summary="R1 위험 구역 진입 1.4s"))
    line = render_lines(core.screen_state())[-1]
    assert line == "[HIGH] | R1 · z-02 | R1 위험 구역 진입 1.4s | 확인 전 · 소리 반복 중"
    core.site_ack(eid)
    lines = render_lines(core.screen_state())
    assert "현장 확인 1건 Tier 0 기록 대기" in lines
    assert lines[-1].endswith("확인됨(현장 확인 버튼, Tier 0 기록 대기)")


def test_text_display_prints_on_change(core: SiteAlertCore) -> None:
    out = io.StringIO()
    display = TextDisplay(out)
    core.add_listener(display.update)
    core.set_control_connected(True)
    core.set_control_connected(True)  # 바뀐 것 없음
    assert out.getvalue().count("----") == 2  # 등록 직후 1번 + 연결 1번


def test_tk_display_renders(core: SiteAlertCore) -> None:
    """tkinter 창을 실제로 만들어 경보·소리 오류·시험 상태를 그려 본다. 화면이 없는 환경이면 건너뛴다."""
    tkinter = pytest.importorskip("tkinter")
    from simai_site_alert.display import TkDisplay

    acked: list[str] = []
    try:
        display = TkDisplay(on_site_ack=acked.append, on_test_alert=lambda: None)
    except tkinter.TclError as e:
        pytest.skip(f"화면 없음: {e}")
    try:
        core.add_listener(display.update)
        eid = uuid7()
        core.handle(local_alert(eid, 1, "RAISED"))
        core._sound.fail_check = True  # noqa: SLF001 - 시험
        core.start_test_alert()
        display._poll()  # noqa: SLF001 - 대기열을 바로 비운다
        display.root.update()
        assert display._fault.winfo_ismapped()  # noqa: SLF001
        buttons = [w for row in display._alerts.winfo_children() for w in row.winfo_children()  # noqa: SLF001
                   if isinstance(w, tkinter.Button)]
        assert len(buttons) == 1
        buttons[0].invoke()
        assert acked == [eid]
        # 장애별 현장 표시 띠 (S15P31A101-91). Tk 를 한 프로세스에서 여러 번 띄우면 환경에 따라 실패해 같은 창에서 본다
        for state, bg in (("CONTROL_HALTED", "#4a0000"), ("REMOTE_FAULT", "#ffb000"), ("NORMAL", "#1e5e2e")):
            core.set_site_status(_site(state))
            display._poll()  # noqa: SLF001
            display.root.update()
            assert display._site.winfo_ismapped()  # noqa: SLF001
            assert display._site.cget("bg") == bg  # noqa: SLF001
            assert display._site.cget("text").startswith(("관제 중단", "원격 관제 장애", "관제 정상"))  # noqa: SLF001
    finally:
        display.close()


def _site(display: str, **kw):
    from simai_site_alert.link_status import SiteStatus

    base = dict(site_display=display, tier0_reasons=(), ec2_reasons=(), control_link="CONNECTED", ec2_link="CONNECTED",
                video_cameras=("cam1", "cam2", "cam3", "cam4"), camera_count=4)
    base.update(kw)
    return SiteStatus(**base)


def test_site_status_lines_replace_control_line(core: SiteAlertCore) -> None:
    """장애별 현장 표시(명세 3.10 표): 관제 중단·원격 관제 장애·정상, 원인과 연결 상세를 함께 쓴다."""
    from simai_site_alert.link_status import Reason

    core.set_site_status(_site("NORMAL"))
    assert render_lines(core.screen_state())[:2] == ["관제 정상", "Tier 0: 연결됨 · 영상: 4/4대 · EC2: 연결됨"]
    core.set_site_status(_site("REMOTE_FAULT", ec2_reasons=(Reason.EC2_UNREACHABLE,), ec2_link="DISCONNECTED"))
    lines = render_lines(core.screen_state())
    assert lines[0] == ("원격 관제 장애 - 현장 경보는 동작 중입니다. 관제 화면·반장 PWA 전달이 늦거나 안 됩니다"
                        " (EC2 응답 없음)")
    assert lines[1].endswith("EC2: 끊김")
    core.set_site_status(_site("CONTROL_HALTED", tier0_reasons=(Reason.HEALTH_MISSING, Reason.VIDEO_UNKNOWN),
                               control_link="DISCONNECTED", video_cameras=(), camera_count=None, recovering=True))
    lines = render_lines(core.screen_state())
    assert lines[0] == "관제 중단 - 위험 감시가 멈췄습니다 (Tier 0 응답 없음, 영상 중계 상태 모름) · 복구 확인 중"
    assert lines[1] == "Tier 0: 끊김 · 영상: 상태 모름 · EC2: 연결됨"
    assert not any("제어 채널" in line for line in lines)

