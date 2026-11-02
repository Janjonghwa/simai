"""CCTV 원격 시청 업로드(S15P31A101-400): 기기 토큰, 수요 있을 때만, codec 먼저·키프레임부터, 다시 붙기, 꺼짐 기본값.

가짜 EC2 는 루프백 websockets 서버다(인증 헤더를 보고 demand 를 보낸다). 입력은 test_preview 와 같은 합성 스트림 I0 기록이다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time

import pytest
from test_preview import _cfg, _frames, _records
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

from simai_relay import cli
from simai_relay.preview import PROTOCOL, parse_frame_message
from simai_relay.remote import (
    RemoteUploader,
    make_remote_hub,
    parse_control,
    remote_url,
)

TOKEN = "test-only-device-token"


def _wait(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


def _demand(active: bool) -> str:
    return json.dumps({"kind": "demand", "active": active, "sent_at_ms": int(time.time() * 1000)})


class FakeEc2:
    """/ws/devices/cctv 만 받는 가짜 EC2. 토큰이 틀리면 401. 연결마다 받은 메시지를 큐에 둔다."""

    def __init__(self, initial_demand: bool = False) -> None:
        self.initial_demand = initial_demand
        self.connections: queue.Queue = queue.Queue()
        self.auth_headers: list[str | None] = []
        self.rejected = 0
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._check, compression=None,
                             max_size=None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.socket.getsockname()[1]}"

    def _check(self, connection, request):
        self.auth_headers.append(request.headers.get("Authorization"))
        if request.path != "/ws/devices/cctv" or request.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.rejected += 1
            return Response(401, "Unauthorized", Headers(), b"")
        return None

    def _handle(self, ws) -> None:
        conn = {"ws": ws, "messages": queue.Queue()}
        ws.send(_demand(self.initial_demand))
        self.connections.put(conn)
        with contextlib.suppress(Exception):
            for m in ws:
                conn["messages"].put(m)

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(3)


@pytest.fixture
def ec2():
    server = FakeEc2()
    yield server
    server.close()


def _drain(q: queue.Queue, timeout: float = 0.3) -> list:
    out = []
    try:
        while True:
            out.append(q.get(timeout=timeout))
    except queue.Empty:
        return out


# ---------------------------------------------------------------------------- 주소·제어 메시지


def test_remote_url_from_ec2_base_url():
    assert remote_url("https://simai.live", TOKEN) == "wss://simai.live/ws/devices/cctv"
    assert remote_url("https://simai.live/", TOKEN) == "wss://simai.live/ws/devices/cctv"
    assert remote_url("http://127.0.0.1:8080", TOKEN) == "ws://127.0.0.1:8080/ws/devices/cctv"
    for bad in ("http://simai.live", "ftp://simai.live", "https://user:pw@simai.live", "https://simai.live/?a=1",
                f"https://simai.live/{TOKEN}"):
        with pytest.raises(ValueError) as e:
            remote_url(bad, TOKEN)
        assert TOKEN not in str(e.value)


def test_parse_control_follows_contract(contract_validator, contract_example):
    example = contract_example("cctv_remote_control")
    contract_validator("cctv_remote_control", example)
    assert parse_control(json.dumps(example)) is True
    doc = json.loads(_demand(False))
    contract_validator("cctv_remote_control", doc)
    assert parse_control(json.dumps(doc)) is False
    for bad in ("not json", "[]", '{"kind":"other","active":true}', '{"kind":"demand","active":"yes"}'):
        assert parse_control(bad) is None


def test_repr_hides_token(ec2):
    up = RemoteUploader(make_remote_hub(["cam1"]), ec2.base_url, TOKEN)
    assert TOKEN not in repr(up)


# ---------------------------------------------------------------------------- 수요·형식


def test_uploads_only_while_demand_active(ec2, contract_validator):
    records = _records(keyframe_interval_s=1.0)  # 키프레임 0, 25, 50
    frames = _frames(records)
    hub = make_remote_hub(["cam1", "cam2"])
    up = RemoteUploader(hub, ec2.base_url, TOKEN)
    up.start()
    try:
        conn = ec2.connections.get(timeout=5)
        assert ec2.auth_headers[-1] == f"Bearer {TOKEN}"  # 기기 토큰은 헤더에만
        _wait(lambda: up.stats.connected)
        hub.publish_codec(records[0])
        for f in frames[:10]:  # 수요 없음: 소비자가 없어 아무것도 올리지 않는다
            hub.publish_frame(f)
        assert _drain(conn["messages"]) == []
        assert hub.consumers == 0

        conn["ws"].send(_demand(True))
        _wait(lambda: hub.consumers == 1)
        for f in frames[10:40]:
            hub.publish_frame(f)
        got = _drain(conn["messages"], 1.0)
        hello, codec = json.loads(got[0]), json.loads(got[1])
        contract_validator("cctv_preview", hello)
        contract_validator("cctv_preview", codec)
        assert hello == {"kind": "hello", "protocol": PROTOCOL, "cameras": ["cam1", "cam2"]}
        assert codec["codec_info"] == records[0].codec_info
        headers = []
        for m in got[2:]:
            header, _ = parse_frame_message(m)
            contract_validator("frame_header", header)
            headers.append(header)
        assert headers[0]["keyframe"] and headers[0]["frame_sequence"] == 25  # 다음 키프레임부터
        assert [h["frame_sequence"] for h in headers] == list(range(25, 40))

        conn["ws"].send(_demand(False))
        _wait(lambda: hub.consumers == 0)
        for f in frames[40:]:
            hub.publish_frame(f)
        assert _drain(conn["messages"]) == []  # 멈춤, 연결은 그대로
        assert up.stats.connected and up.stats.connects == 1
        assert up.stats.demand is False and up.stats.demand_changes == 2
    finally:
        up.stop()


def test_reconnects_after_disconnect_and_resumes_with_hello(ec2):
    ec2.initial_demand = True
    records = _records(keyframe_interval_s=0.4)
    hub = make_remote_hub(["cam1"])
    sleeps: list[float] = []

    def short_sleep(s: float) -> bool:
        sleeps.append(s)
        time.sleep(0.05)
        return False

    up = RemoteUploader(hub, ec2.base_url, TOKEN, sleep=short_sleep)
    up.start()
    try:
        first = ec2.connections.get(timeout=5)
        _wait(lambda: hub.consumers == 1)
        hub.publish_codec(records[0])
        first["ws"].close()  # EC2 가 끊는다
        second = ec2.connections.get(timeout=5)
        _wait(lambda: hub.consumers == 1 and up.stats.connects == 2)
        for f in _frames(records)[:15]:
            hub.publish_frame(f)
        got = _drain(second["messages"], 1.0)
        assert json.loads(got[0])["kind"] == "hello" and json.loads(got[1])["kind"] == "codec"
        assert parse_frame_message(got[2])[0]["keyframe"]
        assert sleeps and sleeps[0] == 1.0  # 붙었다 끊기면 백오프는 처음(1 s)부터
    finally:
        up.stop()


def test_backoff_doubles_to_30s_when_rejected():
    server = FakeEc2()
    sleeps: list[float] = []
    done = threading.Event()

    def record(s: float) -> bool:
        sleeps.append(s)
        if len(sleeps) >= 7:
            done.set()
            return True  # 멈춤
        return False

    try:
        up = RemoteUploader(make_remote_hub(["cam1"]), server.base_url, "wrong-token", sleep=record)
        up.start()
        assert done.wait(10)
        up.stop()
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert server.rejected >= 7 and up.stats.connects == 0
        assert up.stats.last_error is not None and "wrong-token" not in json.dumps(up.snapshot())
    finally:
        server.close()


def test_slow_upload_does_not_block_publish(ec2):
    """EC2 가 읽지 않아도 수신 스레드(publish)는 막히지 않고, 넘치면 다음 키프레임까지 건너뛴다."""
    ec2.initial_demand = True
    records = _records(seconds=4.0, keyframe_interval_s=0.4)
    hub = make_remote_hub(["cam1"])
    up = RemoteUploader(hub, ec2.base_url, TOKEN)
    # 보내기를 멈춰 둔다(업로드 회선이 막힌 것처럼)
    gate = threading.Event()
    original = up._send_loop

    def stuck(ws, consumer):
        gate.wait(5)
        original(ws, consumer)

    up._send_loop = stuck  # type: ignore[method-assign]
    up.start()
    try:
        ec2.connections.get(timeout=5)
        _wait(lambda: hub.consumers == 1)
        hub.publish_codec(records[0])
        t0 = time.monotonic()
        big = [f for f in _frames(records)]
        for _ in range(3):
            for f in big:
                hub.publish_frame(f)
        assert time.monotonic() - t0 < 1.0
        assert hub.camera_snapshot("cam1")["dropped_overflow"] > 0
    finally:
        gate.set()
        up.stop()


# ---------------------------------------------------------------------------- CLI


def test_remote_is_off_by_default(tmp_path, monkeypatch):
    started: list = []
    monkeypatch.setattr(cli.RemoteUploader, "start", lambda self: started.append(self))
    assert cli.main(["run", "--config", str(_cfg(tmp_path)), "--seconds", "0.3", "--status-port", "0",
                     "--env-file", str(tmp_path / "none.env")]) == 0
    assert started == []


def test_cli_remote_needs_ec2_and_token(tmp_path, monkeypatch, capsys):
    for k in ("SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["run", "--config", str(_cfg(tmp_path)), "--seconds", "0.1", "--status-port", "0",
                     "--remote-cctv", "--env-file", str(tmp_path / "none.env")]) == 2
    err = capsys.readouterr().err
    assert "SIMAI_EC2_BASE_URL" in err and "SIMAI_DEVICE_TOKEN" in err


def test_cli_remote_streams_when_demanded(tmp_path, monkeypatch, ec2):
    ec2.initial_demand = True
    env = tmp_path / "t.env"
    env.write_text(f"SIMAI_EC2_BASE_URL={ec2.base_url}\nSIMAI_DEVICE_TOKEN={TOKEN}\n", encoding="utf-8")
    for k in ("SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    handlers = logging.getLogger().handlers[:]
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(cli.main([
        "run", "--config", str(_cfg(tmp_path)), "--seconds", "3", "--status-port", "0", "--report-interval", "1",
        "--remote-cctv", "--env-file", str(env)])), daemon=True)
    t.start()
    conn = ec2.connections.get(timeout=5)
    kinds = []
    while len(kinds) < 3:
        m = conn["messages"].get(timeout=5)
        kinds.append(json.loads(m)["kind"] if isinstance(m, str) else parse_frame_message(m)[0])
    assert kinds[0] == "hello" and kinds[1] == "codec" and kinds[2]["keyframe"]
    t.join(10)
    logging.getLogger().handlers[:] = handlers  # cli 가 바꾼 stderr 처리기를 되돌린다(가짜 EC2 정리 로그가 닫힌 출력에 쓰지 않게)
    assert result == [0]
