"""CCTV 로컬 미리보기(S15P31A101-310): 루프백 WebSocket, codec 먼저·키프레임부터, 느린 소비자 격리, 꺼짐 기본값.

입력은 합성 스트림(PyAV 메모리 인코딩, 사람 이미지 없음)을 수신 루프에 돌려 얻은 실제 I0 기록이다. 카메라는 없다.
"""

from __future__ import annotations

import base64
import inspect
import json
import socket
import threading
import time

import pytest
from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import FakeClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import SyntheticSource
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from simai_relay import preview
from simai_relay.preview import (
    PREVIEW_HOST,
    PROTOCOL,
    PreviewHub,
    PreviewServer,
    PreviewSink,
    codec_string,
    parse_frame_message,
)
from simai_relay.relay import CameraRelay
from simai_relay.sink import I0CodecInfo, I0Frame, MemorySink

ORIGIN = "http://localhost:5173"


def _records(camera_id: str = "cam1", seconds: float = 2.5, keyframe_interval_s: float = 1.0):
    """합성 스트림 한 대를 수신 루프에 돌려 얻은 I0 기록(codec_info → 키프레임부터)."""
    clock = FakeClock()
    sink = MemorySink(camera_id)
    synth = SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=keyframe_interval_s)
    relay = CameraRelay(CameraSpec(camera_id, synth, CaptureTimeModel()), sink, clock, threading.Event(),
                        clock.monotonic() + seconds)
    relay.run()
    assert relay.error is None
    return sink.records


def _frames(records) -> list[I0Frame]:
    return [r for r in records if isinstance(r, I0Frame)]


def _publish(hub: PreviewHub, records) -> None:
    for r in records:
        if isinstance(r, I0CodecInfo):
            hub.publish_codec(r)
        else:
            hub.publish_frame(r)


def _drain(consumer) -> list:
    out = []
    while (m := consumer.get(0)) is not None:
        out.append(m)
    return out


@pytest.fixture
def server():
    hub = PreviewHub(["cam1", "cam2"])
    srv = PreviewServer(hub, 0, origins=[ORIGIN])
    srv.start()
    yield srv
    srv.stop()


def _url(srv: PreviewServer) -> str:
    return f"ws://127.0.0.1:{srv.port}"


def _wait(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


# ---------------------------------------------------------------------------- 메시지·계약


def test_messages_follow_contract(contract_validator):
    records = _records()
    hub = PreviewHub(["cam1"], max_queue_frames=1000)
    consumer = hub.register()
    _publish(hub, records)
    messages = _drain(consumer)

    hello = json.loads(messages[0])
    contract_validator("cctv_preview", hello)
    assert hello == {"kind": "hello", "protocol": PROTOCOL, "cameras": ["cam1"]}
    codec = json.loads(messages[1])
    contract_validator("cctv_preview", codec)
    assert codec["codec_info"] == records[0].codec_info  # I0 codec_info 그대로
    sps = base64.b64decode(codec["codec_info"]["sps"])
    assert codec["codec_string"] == f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"

    frames = [parse_frame_message(m) for m in messages[2:]]
    assert frames and isinstance(messages[2], bytes)
    for (header, data), record in zip(frames, _frames(records), strict=True):
        contract_validator("frame_header", header)
        assert header == record.header and data == record.data  # 헤더·접근 단위 그대로(재인코딩 없음)
    assert frames[0][0]["keyframe"]


def test_codec_string_from_sps():
    assert codec_string(bytes([0x67, 0x64, 0x00, 0x2A, 0xAC])) == "avc1.64002A"
    with pytest.raises(ValueError):
        codec_string(b"\x67\x64")


def test_late_consumer_gets_last_codec_then_waits_for_keyframe():
    records = _records(keyframe_interval_s=1.0)  # 키프레임 0, 25, 50
    frames = _frames(records)
    hub = PreviewHub(["cam1", "cam2"], max_queue_frames=1000)
    hub.publish_codec(records[0])
    for f in frames[:5]:  # 소비자 없음: 아무것도 하지 않는다
        hub.publish_frame(f)
    assert hub.camera_snapshot("cam1") == {"sent": 0, "dropped_overflow": 0, "waiting_keyframe": 0}

    consumer = hub.register()
    for f in frames[5:]:
        hub.publish_frame(f)
    messages = _drain(consumer)
    assert json.loads(messages[0])["cameras"] == ["cam1", "cam2"]
    assert json.loads(messages[1])["codec_info"]["camera_id"] == "cam1"  # 영상이 없는 cam2 의 codec 은 없다
    headers = [parse_frame_message(m)[0] for m in messages[2:]]
    assert headers[0]["keyframe"] and headers[0]["frame_sequence"] == 25  # 다음 키프레임부터
    assert [h["frame_sequence"] for h in headers] == list(range(25, frames[-1].header["frame_sequence"] + 1))
    assert hub.camera_snapshot("cam1")["waiting_keyframe"] == 20


def test_new_epoch_codec_restarts_from_keyframe():
    first, second = _records(), _records()
    second = [I0CodecInfo({**second[0].codec_info, "stream_epoch": 7})] + [
        I0Frame({**f.header, "stream_epoch": 7}, f.data) for f in _frames(second)]
    hub = PreviewHub(["cam1"], max_queue_frames=1000)
    consumer = hub.register()
    _publish(hub, first[:10])
    hub.publish_codec(second[0])
    _publish(hub, second[2:])  # 새 epoch 의 키프레임(순번 0)을 놓쳤다 → 다음 키프레임까지 넣지 않는다
    after = _drain(consumer)
    i = next(k for k, m in enumerate(after) if isinstance(m, str) and json.loads(m).get("kind") == "codec"
             and json.loads(m)["codec_info"]["stream_epoch"] == 7)
    following = [parse_frame_message(m)[0] for m in after[i + 1:]]
    assert following[0]["stream_epoch"] == 7 and following[0]["keyframe"] and following[0]["frame_sequence"] == 25


# ---------------------------------------------------------------------------- 느린 소비자


def test_slow_consumer_skips_to_keyframe_without_affecting_others():
    records = _records(seconds=4.5, keyframe_interval_s=1.0)
    hub = PreviewHub(["cam1"], max_queue_frames=10)
    slow = hub.register()
    fast = hub.register()
    got: list = []
    hub.publish_codec(records[0])
    t0 = time.perf_counter()
    for f in _frames(records):
        hub.publish_frame(f)
        got.extend(_drain(fast))  # 빠른 소비자는 바로바로 가져간다
    elapsed = time.perf_counter() - t0

    fast_seq = [parse_frame_message(m)[0]["frame_sequence"] for m in got if isinstance(m, bytes)]
    assert fast_seq == [f.header["frame_sequence"] for f in _frames(records)]  # 하나도 안 버림

    slow_frames = [parse_frame_message(m)[0] for m in _drain(slow) if isinstance(m, bytes)]
    assert [h["frame_sequence"] for h in slow_frames] == list(range(10))  # 상한까지만, 나머지는 건너뜀
    assert hub.camera_snapshot("cam1")["dropped_overflow"] > 0
    assert elapsed < 1.0  # 수신 스레드 쪽 넣기는 기다리지 않는다

    # 느린 소비자가 비운 뒤에는 다음 키프레임부터 다시 받는다(중간 P 프레임으로 시작하지 않는다)
    more = _frames(_records(seconds=2.5, keyframe_interval_s=1.0))
    for f in more[3:]:
        hub.publish_frame(f)
    resumed = [parse_frame_message(m)[0] for m in _drain(slow) if isinstance(m, bytes)]
    assert resumed[0]["keyframe"] and resumed[0]["frame_sequence"] == 25


def test_preview_failure_does_not_break_tier0_sink():
    records = _records(seconds=0.5)
    inner = MemorySink("cam1")
    hub = PreviewHub(["cam1"])
    hub.register()

    def broken(record):
        raise RuntimeError("boom")

    hub.publish_frame = broken  # type: ignore[method-assign]
    sink = PreviewSink(inner, hub)
    for r in records:
        sink.send_codec_info(r) if isinstance(r, I0CodecInfo) else sink.send_frame(r)
    assert inner.records == records  # 원래 전송은 그대로
    assert hub.stats.errors == len(_frames(records))


# ---------------------------------------------------------------------------- WebSocket 서버


def test_server_listens_on_loopback_only(server):
    assert server.address[0] == "127.0.0.1" == PREVIEW_HOST
    assert "host" not in inspect.signature(PreviewServer).parameters  # 바꾸는 인자를 두지 않는다


def test_websocket_sends_hello_codec_then_keyframe(server, contract_validator):
    records = _records()
    with connect(_url(server), origin=ORIGIN, max_size=None, compression=None) as ws:
        hello = json.loads(ws.recv(timeout=5))
        contract_validator("cctv_preview", hello)
        assert hello["cameras"] == ["cam1", "cam2"]
        _wait(lambda: server.hub.consumers == 1)
        _publish(server.hub, records[:12])
        codec = json.loads(ws.recv(timeout=5))
        assert codec["kind"] == "codec"
        header, data = parse_frame_message(ws.recv(timeout=5))
        assert header["keyframe"] and data == records[1].data
    _wait(lambda: server.hub.consumers == 0)


def test_slow_websocket_client_does_not_block_publish_or_others(server):
    records = _records(seconds=4.5, keyframe_interval_s=1.0)
    big = [I0Frame(f.header, f.data + bytes(200_000)) for f in _frames(records)]  # 큰 프레임으로 소켓 버퍼를 채운다
    with connect(_url(server), origin=ORIGIN, max_size=None, compression=None) as slow, \
            connect(_url(server), origin=ORIGIN, max_size=None, compression=None) as fast:
        slow.recv(timeout=5)
        fast.recv(timeout=5)
        _wait(lambda: server.hub.consumers == 2)
        server.hub.publish_codec(records[0])
        received: list = []
        done = threading.Event()

        def read_fast() -> None:
            try:
                while len(received) < 1 + len(big):
                    received.append(fast.recv(timeout=10))
            finally:
                done.set()

        reader = threading.Thread(target=read_fast, daemon=True)
        reader.start()
        t0 = time.perf_counter()
        for f in big:
            server.hub.publish_frame(f)
            time.sleep(0.002)  # 빠른 소비자가 따라올 틈(실제 25fps 보다 훨씬 빠르다)
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0  # 느린 소비자(읽지 않음) 때문에 넣기가 막히지 않는다
        assert done.wait(15)
        frames = [parse_frame_message(m)[0]["frame_sequence"] for m in received if isinstance(m, bytes)]
        assert frames == [f.header["frame_sequence"] for f in big]
        assert server.hub.camera_snapshot("cam1")["dropped_overflow"] > 0  # 느린 쪽만 버렸다


def test_foreign_origin_is_rejected(server):
    with pytest.raises(InvalidStatus) as e:
        connect(_url(server), origin="https://evil.example", open_timeout=5)
    assert e.value.response.status_code == 403
    with pytest.raises(InvalidStatus):
        connect(_url(server), open_timeout=5)  # Origin 없음(브라우저가 아닌 웹 페이지 흉내도 막는다)
    assert server.hub.stats.consumers_total == 0


def test_consumer_limit(monkeypatch):
    hub = PreviewHub(["cam1"], max_consumers=1)
    srv = PreviewServer(hub, 0, origins=[ORIGIN])
    srv.start()
    try:
        with connect(_url(srv), origin=ORIGIN) as first:
            first.recv(timeout=5)
            with connect(_url(srv), origin=ORIGIN) as second:
                with pytest.raises(Exception):
                    second.recv(timeout=5)
            assert hub.stats.consumers_rejected == 1
    finally:
        srv.stop()


# ---------------------------------------------------------------------------- CLI


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[cameras]]\ncamera_id = "cam1"\nkind = "synthetic"\nwidth = 64\nheight = 48\n'
                   'keyframe_interval_s = 0.4\n', encoding="utf-8")
    return cfg


def test_preview_is_off_by_default(tmp_path, monkeypatch):
    from simai_relay import cli

    started: list = []
    monkeypatch.setattr(cli.PreviewServer, "start", lambda self: started.append(self))
    assert cli.main(["run", "--config", str(_cfg(tmp_path)), "--seconds", "0.3", "--status-port", "0",
                     "--env-file", str(tmp_path / "none.env")]) == 0
    assert started == []


def test_cli_preview_streams_without_send(tmp_path):
    from simai_relay import cli

    port = _free_port()
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(cli.main([
        "run", "--config", str(_cfg(tmp_path)), "--seconds", "3", "--status-port", "0", "--report-interval", "1",
        "--preview-port", str(port), "--env-file", str(tmp_path / "none.env")])), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while True:
        try:
            ws = connect(f"ws://127.0.0.1:{port}", origin="https://simai.live", max_size=None, open_timeout=2)
            break
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.05)
    with ws:
        kinds = []
        while len(kinds) < 3:
            m = ws.recv(timeout=5)
            kinds.append(json.loads(m)["kind"] if isinstance(m, str) else parse_frame_message(m)[0])
        assert kinds[0] == "hello" and kinds[1] == "codec" and kinds[2]["keyframe"]
    t.join(10)
    assert result == [0]


def test_cli_rejects_bad_preview_port(tmp_path, capsys):
    from simai_relay.cli import main

    assert main(["run", "--config", str(_cfg(tmp_path)), "--seconds", "0.1", "--preview-port", "70000",
                 "--env-file", str(tmp_path / "none.env")]) == 2
    assert "--preview-port" in capsys.readouterr().err


def test_default_port_matches_front_default():
    assert preview.DEFAULT_PREVIEW_PORT == 47032  # front VITE_CCTV_PREVIEW_URL 기본값과 같게
