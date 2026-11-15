"""녹화 재생 시험 알림 스위치 (S15P31A101-736, AGENTS 규칙 5 의 시험용 예외).

- .env SIMAI_REPLAY_TEST_ALERTS 는 기본 꺼짐이다. 끄면 -724 그대로 시험 입력(녹화 파일·합성 영상) 중 경보 소리를 끈다.
- 켜면 시험 입력이어도 소리를 라이브처럼 내고, 하트비트 sounding_event_ids 에도 싣고, 화면에 「재생 시험 · 실제 알림 켜짐」 띠를 띄운다.
- 실제 카메라(RTSP) 입력이면 스위치와 무관하게 지금처럼 울리고 띠가 없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import LAPTOP_ID

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.clock import FakeClock
from simai_site_alert.config import load_settings
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.display import REPLAY_TEST_BANNER, TEST_INPUT_BANNER, render_lines
from simai_site_alert.link_status import RelayReport
from simai_site_alert.sound import FakeSoundBackend


def _report(kind: str) -> RelayReport:
    cams = tuple({"camera_id": f"cam_{i}", "rtsp": "CONNECTED", "stream_epoch": 1, "last_frame_sequence": 10,
                  "source_kind": kind} for i in range(4))
    return RelayReport(tuple(c["camera_id"] for c in cams), len(cams), cams)


@pytest.fixture
def switched_on(sound: FakeSoundBackend, clock: FakeClock) -> SiteAlertCore:
    return SiteAlertCore(sound, laptop_id=LAPTOP_ID, clock=clock, replay_test_alerts=True)


def test_switch_defaults_off_and_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIMAI_REPLAY_TEST_ALERTS", raising=False)
    env = tmp_path / ".env"
    env.write_text("SIMAI_LAPTOP_ID=laptop-01\n", encoding="utf-8")
    assert load_settings(env).replay_test_alerts is False
    env.write_text("SIMAI_REPLAY_TEST_ALERTS=true\n", encoding="utf-8")
    assert load_settings(env).replay_test_alerts is True
    monkeypatch.setenv("SIMAI_REPLAY_TEST_ALERTS", "false")  # 환경 변수가 .env 보다 먼저
    assert load_settings(env).replay_test_alerts is False
    monkeypatch.setenv("SIMAI_REPLAY_TEST_ALERTS", "ture")
    with pytest.raises(ValueError):
        load_settings(env)


def test_switch_off_keeps_test_input_silent(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    SiteAlertApp(core, None).on_relay_report(_report("FILE"), 0.0)
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL"))
    assert sound.playing is None
    lines = render_lines(core.screen_state())
    assert any(TEST_INPUT_BANNER in line for line in lines)
    assert not any(REPLAY_TEST_BANNER in line for line in lines)


def test_switch_on_sounds_replay_alerts_like_live_and_shows_banner(switched_on: SiteAlertCore,
                                                                   sound: FakeSoundBackend) -> None:
    SiteAlertApp(switched_on, None).on_relay_report(_report("FILE"), 0.0)
    assert switched_on.test_input
    eid = uuid7()
    switched_on.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL"))
    assert sound.calls == [("start", "CRITICAL")]
    assert switched_on.alarm_status() == {"sounding_event_ids": [eid], "displayed_event_ids": [eid]}
    lines = render_lines(switched_on.screen_state())
    assert any(REPLAY_TEST_BANNER in line for line in lines)
    assert not any(TEST_INPUT_BANNER in line for line in lines)
    assert not any("시험 입력이라 소리 없음" in line for line in lines)


def test_switch_on_with_rtsp_is_plain_live(switched_on: SiteAlertCore, sound: FakeSoundBackend) -> None:
    SiteAlertApp(switched_on, None).on_relay_report(_report("RTSP"), 0.0)
    eid = uuid7()
    switched_on.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    assert sound.calls == [("start", "HIGH")]
    lines = render_lines(switched_on.screen_state())
    assert not any(REPLAY_TEST_BANNER in line or TEST_INPUT_BANNER in line for line in lines)
