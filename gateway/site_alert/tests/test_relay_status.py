"""영상 중계 → 단말 상태 보고(loopback UDP): 형식, 늦게 온 보고, 형식 오류는 통째로 버림, 실제 UDP 수신."""

from __future__ import annotations

import json
import socket

import pytest
from tls_helpers import wait_until

from simai_site_alert.relay_status import KIND, RelayStatusReceiver, build_report, new_boot_id, parse_report

ENTRY = {"camera_id": "cam1", "rtsp": "CONNECTED", "stream_epoch": 3, "last_frame_sequence": 99,
         "camera_to_gateway_gap_max_ms": 80.0}


def test_roundtrip_and_entries_pass_contract(contract_validator) -> None:
    data = build_report(boot="b1", seq=7, camera_count=2, video_cameras=["cam2", "cam1"],
                        cameras=[ENTRY, {"camera_id": "cam2", "rtsp": "DISCONNECTED"}])
    boot, seq, report = parse_report(data)
    assert (boot, seq) == ("b1", 7)
    assert report.video_cameras == ("cam1", "cam2") and report.camera_count == 2
    hb = {"schema_version": "1.2.0", "msg_type": "laptop_heartbeat", "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000112",
          "sent_at": 1, "source_id": "laptop-01",
          "payload": {"heartbeat_seq": 0, "site_display": "NORMAL", "cameras": list(report.cameras),
                      "tier0": {"control": "CONNECTED", "last_message_at": 1, "video_cameras": list(report.video_cameras)}}}
    contract_validator("laptop_heartbeat", hb)


def _doc(**over):
    doc = {"kind": KIND, "boot": "b", "seq": 1, "camera_count": 1, "video_cameras": ["cam1"], "cameras": [ENTRY]}
    doc.update(over)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"\xff\xfe",
        _doc(kind="other"),
        _doc(seq=-1),
        _doc(seq=True),
        _doc(boot=""),
        _doc(camera_count=1000),
        _doc(video_cameras=["cam 1"]),
        _doc(video_cameras=["cam1", "cam1"]),
        _doc(video_cameras="cam1"),
        _doc(cameras=[{**ENTRY, "rtsp": "UP"}]),
        _doc(cameras=[{**ENTRY, "stream_epoch": -1}]),
        _doc(cameras=[{**ENTRY, "camera_to_gateway_gap_max_ms": float("nan")}]),
        _doc(cameras=[ENTRY, ENTRY]),
        _doc(cameras=[{"rtsp": "CONNECTED"}]),
    ],
)
def test_malformed_reports_are_dropped_whole(data: bytes) -> None:
    assert parse_report(data) is None


def test_unknown_entry_fields_are_not_forwarded() -> None:
    _, _, report = parse_report(_doc(cameras=[{**ENTRY, "rtsp_url": "rtsp://user:pw@cam"}]))
    assert "rtsp_url" not in report.cameras[0]


def test_receiver_over_udp_drops_late_reports_and_accepts_new_boot() -> None:
    got = []
    rx = RelayStatusReceiver(lambda report, now: got.append(report), port=0)
    rx.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        boot = new_boot_id()

        def send(seq: int, video: list[str], b: str = boot) -> None:
            tx.sendto(build_report(boot=b, seq=seq, camera_count=1, video_cameras=video, cameras=[]),
                      ("127.0.0.1", rx.port))

        send(5, ["cam1"])
        wait_until(lambda: len(got) == 1)
        send(4, [])  # 같은 중계의 늦게 온 보고: 버린다
        send(5, [])  # 중복
        tx.sendto(b"garbage", ("127.0.0.1", rx.port))
        send(6, [])
        wait_until(lambda: len(got) == 2)
        assert got[-1].video_cameras == ()
        send(0, ["cam1"], b=new_boot_id())  # 중계를 다시 띄움(새 boot, seq 0)
        wait_until(lambda: len(got) == 3)
        assert got[-1].video_cameras == ("cam1",)
        wait_until(lambda: rx.dropped == 3)
    finally:
        tx.close()
        rx.close()


def test_receiver_port_in_use_raises() -> None:
    first = RelayStatusReceiver(lambda *a: None, port=0)
    try:
        with pytest.raises(OSError):
            RelayStatusReceiver(lambda *a: None, port=first.port)
    finally:
        first.close()
