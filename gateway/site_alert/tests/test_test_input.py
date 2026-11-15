"""시험 입력(녹화 파일·합성 영상)일 때 실제 경보 소리 끄기 (S15P31A101-724, AGENTS 규칙 5).

- 중계 보고의 cameras[].source_kind 를 그대로 받아 하트비트 cameras[] 에 싣는다(계약 laptop_heartbeat 1.3.0).
- 하나라도 FILE·SYNTHETIC 이면 단말은 경보를 화면에만 표시하고 소리를 내지 않는다. 시험 입력 중 표시된 경보는 해소까지 조용하다.
- RTSP 이거나 source_kind 가 없으면(이전 중계) 지금처럼 울린다.
"""

from __future__ import annotations

import json

from conftest import LAPTOP_ID

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.clock import FakeClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.display import TEST_INPUT_BANNER, render_lines
from simai_site_alert.link_status import RelayReport, SiteLinkMonitor
from simai_site_alert.relay_status import KIND, build_report, parse_report
from simai_site_alert.sound import FakeSoundBackend


def _entry(cam: str, kind: str | None) -> dict:
    entry = {"camera_id": cam, "rtsp": "CONNECTED", "stream_epoch": 1, "last_frame_sequence": 10}
    if kind is not None:
        entry["source_kind"] = kind
    return entry


def _report(*kinds: str | None) -> RelayReport:
    cams = tuple(_entry(f"cam_{i}", k) for i, k in enumerate(kinds))
    return RelayReport(tuple(c["camera_id"] for c in cams), len(cams), cams)


# ---- 중계 보고 형식 ------------------------------------------------------------------------------------------------


def test_source_kind_passes_through_relay_report() -> None:
    data = build_report(boot="b", seq=1, camera_count=2, video_cameras=["cam_0", "cam_1"],
                        cameras=[_entry("cam_0", "FILE"), _entry("cam_1", "RTSP")])
    _, _, report = parse_report(data)
    assert [c.get("source_kind") for c in report.cameras] == ["FILE", "RTSP"]
    assert report.test_input


def test_unknown_source_kind_drops_report() -> None:
    doc = {"kind": KIND, "boot": "b", "seq": 1, "camera_count": 1, "video_cameras": ["cam_0"],
           "cameras": [_entry("cam_0", "REPLAY")]}
    assert parse_report(json.dumps(doc).encode()) is None


def test_test_input_only_for_file_or_synthetic() -> None:
    assert _report("FILE").test_input
    assert _report("RTSP", "SYNTHETIC").test_input
    assert not _report("RTSP", "RTSP").test_input
    assert not _report(None, None).test_input  # 이전 중계: RTSP 로 본다
    assert not _report().test_input


def test_heartbeat_carries_source_kind_and_fits_contract(contract_validator) -> None:
    clock = FakeClock()
    monitor = SiteLinkMonitor()
    app = SiteAlertApp(SiteAlertCore(FakeSoundBackend(), laptop_id=LAPTOP_ID, clock=clock), None,
                       monitor=monitor, clock=clock)
    app.on_relay_report(_report("FILE", "FILE"), clock.monotonic())
    hb = app.build_heartbeat(include_ec2=True)
    assert hb is not None and hb["schema_version"] == "1.3.0"
    assert [c["source_kind"] for c in hb["payload"]["cameras"]] == ["FILE", "FILE"]
    contract_validator("laptop_heartbeat", hb)


# ---- 소리 끄기 ----------------------------------------------------------------------------------------------------


def test_file_input_shows_alert_without_sound(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    app = SiteAlertApp(core, None)
    app.on_relay_report(_report("FILE"), 0.0)
    assert core.test_input
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL"))
    assert [c for c in sound.calls if c[0] == "start"] == []
    assert sound.playing is None
    assert core.alarm_status() == {"sounding_event_ids": [], "displayed_event_ids": [eid]}
    lines = render_lines(core.screen_state())
    assert any(TEST_INPUT_BANNER in line for line in lines)
    assert any("시험 입력이라 소리 없음" in line for line in lines)


def test_switching_to_file_stops_sounding_alert(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="HIGH"))
    assert sound.playing is not None and sound.playing.name == "HIGH"
    core.set_test_input(True)
    assert sound.playing is None
    assert core.alarm_status()["sounding_event_ids"] == []
    core.handle(local_alert(eid, 2, "UPDATED", severity="CRITICAL"))  # 등급 상향도 울리지 않는다
    assert sound.playing is None


def test_alert_from_test_input_stays_silent_after_rtsp_returns(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    replayed = uuid7()
    core.set_test_input(True)
    core.handle(local_alert(replayed, 1, "RAISED", severity="CRITICAL"))
    core.set_test_input(False)
    assert sound.playing is None, "재생 영상에서 난 경보는 실제 카메라로 돌아와도 해소까지 울리지 않는다"
    assert not any(TEST_INPUT_BANNER in line for line in render_lines(core.screen_state()))

    live = uuid7()
    core.handle(local_alert(live, 1, "RAISED", severity="HIGH"))
    assert sound.playing is not None and sound.playing.name == "HIGH"
    assert core.alarm_status()["sounding_event_ids"] == [live]

    core.handle(local_alert(replayed, 2, "RESOLVED", severity="CRITICAL"))
    core.handle(local_alert(replayed, 3, "UPDATED", severity="CRITICAL"))  # 해소 뒤 재개는 실제 입력의 경보로 새로 판단한다
    assert sound.playing is not None and sound.playing.name == "CRITICAL"


def test_rtsp_input_sounds_as_before(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    app = SiteAlertApp(core, None)
    app.on_relay_report(_report("RTSP", "RTSP"), 0.0)
    assert not core.test_input
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED", severity="CRITICAL"))
    assert sound.calls == [("start", "CRITICAL")]
    assert core.alarm_status()["sounding_event_ids"] == [eid]


def test_startup_test_alert_still_sounds_during_test_input(core: SiteAlertCore, sound: FakeSoundBackend) -> None:
    core.set_test_input(True)
    assert core.start_test_alert()
    assert sound.playing is not None and sound.playing.name == "TEST"
