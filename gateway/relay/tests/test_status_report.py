"""중계 → 현장 경보 단말 상태 보고(S15P31A101-91): 실제 중계 세션의 카메라 상태가 단말 수신기에 닿고, 항목이
laptop_heartbeat 계약을 통과하며, 받는 쪽이 없어도 중계가 멈추지 않는다."""

from __future__ import annotations

import socket
import time

from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import SystemClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import SyntheticSource
from simai_site_alert.relay_status import RelayStatusReceiver

from simai_relay.relay import RelaySession
from simai_relay.sink import CountingSink
from simai_relay.status_report import RelayStatusPublisher


def wait_until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.02)


def _session(clock: SystemClock) -> RelaySession:
    cams = [CameraSpec(c, SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=1.0),
                       CaptureTimeModel()) for c in ("cam_A", "cam_B")]
    return RelaySession(cams, CountingSink, clock, reconnect_delays_s=(0.2,))


def test_publisher_reaches_site_alert_receiver(contract_validator) -> None:
    clock = SystemClock()
    session = _session(clock)
    got = []
    rx = RelayStatusReceiver(lambda report, now: got.append(report), port=0)
    rx.start()
    session.start()
    video = ["cam_A"]
    pub = RelayStatusPublisher(lambda: session.heartbeat_cameras(), lambda: list(video), port=rx.port, interval_s=0.2)
    pub.start()
    try:
        wait_until(lambda: got and got[-1].cameras and all("last_frame_sequence" in c for c in got[-1].cameras))
        report = got[-1]
        assert report.camera_count == 2 and report.video_cameras == ("cam_A",)
        assert {c.get("source_kind") for c in report.cameras} == {"SYNTHETIC"} and report.test_input  # S15P31A101-724
        hb = {"schema_version": "1.3.0", "msg_type": "laptop_heartbeat", "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000112",
              "sent_at": 1, "source_id": "laptop-01",
              "payload": {"heartbeat_seq": 0, "site_display": "NORMAL", "cameras": list(report.cameras),
                          "tier0": {"control": "CONNECTED", "last_message_at": 1, "video_cameras": ["cam_A"]}}}
        contract_validator("laptop_heartbeat", hb)
        video.clear()  # Tier 0 영상 연결이 모두 끊김
        wait_until(lambda: got[-1].video_cameras == ())
    finally:
        pub.stop()
        session.stop()
        session.join(timeout=10)
        rx.close()


def test_publisher_without_receiver_keeps_running() -> None:
    """단말이 꺼져 있어도(받는 쪽 없음) 보고 실패로 중계가 멈추지 않는다."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 아무도 듣지 않는 포트
    pub = RelayStatusPublisher(lambda: [], lambda: [], port=port, interval_s=0.05)
    pub.start()
    try:
        time.sleep(0.4)
        assert pub._thread.is_alive()  # noqa: SLF001
    finally:
        pub.stop()


def test_log_report_does_not_reset_heartbeat_window() -> None:
    """로그(_report)는 도착 간격 창을 비우지 않는다. 창은 상태 보고(하트비트)만 비운다."""
    clock = SystemClock()
    session = _session(clock)
    session.start()
    try:
        wait_until(lambda: all("camera_to_gateway_gap_max_ms" in e for e in session.heartbeat_cameras(reset_window=False)))
        session.heartbeat_cameras(reset_window=False)
        assert all("camera_to_gateway_gap_max_ms" in e for e in session.heartbeat_cameras(reset_window=False))
    finally:
        session.stop()
        session.join(timeout=10)


def test_cli_rejects_bad_status_port(tmp_path, capsys) -> None:
    from simai_relay.cli import main

    cfg = tmp_path / "c.toml"
    cfg.write_text('[[cameras]]\ncamera_id = "cam_A"\nkind = "synthetic"\n', encoding="utf-8")
    assert main(["run", "--config", str(cfg), "--seconds", "0.1", "--status-port", "70000",
                 "--env-file", str(tmp_path / "none.env")]) == 2
    assert "--status-port" in capsys.readouterr().err
