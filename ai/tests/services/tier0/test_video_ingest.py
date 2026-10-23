"""Tier 0 I0 영상 수신(S15P31A101-83): 레코드 형식(노트북과 같은 기준 바이트), 헤더 계약 검증, 기기 토큰, 손실 집계,
카메라별 연결 독립. 로컬 TLS(자체 서명, tmp_path 에서 만들고 커밋하지 않음)로 돈다."""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import logging
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0 import video_ingest as vi  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402

REPO = AI_DIR.parent
TOKEN = "laptop-device-token-S3CRET"

# gateway/relay/tests/test_tls_sink.py 와 같은 바이트 (frame_header 예시 + 압축 프레임 8바이트, SKIP_NOTICE)
GOLDEN_FRAME = bytes.fromhex(
    "04000000eb000000087b2263616d6572615f6964223a2263616d5f41222c2273747265616d5f65706f6368223a31322c226672616d655f73"
    "657175656e6365223a31303332342c22736f757263655f707473223a393837363534332c227074735f74696d6562617365223a22312f3930"
    "303030222c22657374696d617465645f636170747572655f74696d655f6d73223a313736303334323430303130302c2274696d657374616d"
    "705f736f75726365223a22524543454956455f455354494d41544544222c2274696d657374616d705f756e6365727461696e74795f6d7322"
    "3a31382c226b65796672616d65223a747275657d0000000165888400")
GOLDEN_SKIP = bytes.fromhex(
    "0500000054000000007b2263616d6572615f6964223a2263616d5f41222c2273747265616d5f65706f6368223a31322c2266697273745f73"
    "657175656e6365223a31303330302c226c6173745f73657175656e6365223a31303332337d")


def _example(name: str) -> dict:
    return json.loads((REPO / "contracts" / "examples" / f"{name}.json").read_text(encoding="utf-8"))


def _codec(cam: str = "cam_A", epoch: int = 0) -> dict:
    return {**_example("codec_info"), "camera_id": cam, "stream_epoch": epoch}


def _header(cam: str = "cam_A", epoch: int = 0, seq: int = 0, key: bool = False) -> dict:
    return {**_example("frame_header"), "camera_id": cam, "stream_epoch": epoch, "frame_sequence": seq,
            "keyframe": key}


# ---------------------------------------------------------------------------- 형식·계약


def test_golden_bytes_decode_to_contract_records():
    kind, meta, data = vi.read_record(_Buf(GOLDEN_FRAME))
    assert kind == vi.FRAME and data == bytes.fromhex("0000000165888400")
    assert meta == _example("frame_header")
    vi.validate_frame_header(meta)
    kind, meta, _ = vi.read_record(_Buf(GOLDEN_SKIP))
    assert kind == vi.SKIP_NOTICE
    vi.validate_skip_notice(meta)
    assert vi.encode(vi.FRAME, _example("frame_header"), bytes.fromhex("0000000165888400")) == GOLDEN_FRAME


class _Buf:
    def __init__(self, data: bytes) -> None:
        self._b = io.BytesIO(data)

    def recv(self, n: int) -> bytes:
        return self._b.read(n)


def test_read_record_limits_and_bad_json():
    assert vi.read_record(_Buf(b"")) is None
    with pytest.raises(vi.WireError):
        vi.read_record(_Buf(vi.PREFIX.pack(vi.FRAME, 10, vi.MAX_DATA + 1)))  # 본문을 읽기 전에 거절
    with pytest.raises(vi.WireError):
        vi.read_record(_Buf(vi.PREFIX.pack(9, 0, 0)))
    with pytest.raises(vi.WireError):
        vi.read_record(_Buf(vi.PREFIX.pack(vi.FRAME, 3, 0) + b"[1]"))
    with pytest.raises(vi.WireError):
        vi.read_record(_Buf(vi.PREFIX.pack(vi.FRAME, 3, 0) + b"NaN"))
    with pytest.raises(vi.WireError):
        vi.read_record(_Buf(vi.PREFIX.pack(vi.FRAME, 10, 0) + b"{}"))  # 중간에 끊김


MUTATIONS = [
    ("frame_header", lambda h: h.pop("timestamp_uncertainty_ms")),
    ("frame_header", lambda h: h.pop("source_pts")),
    ("frame_header", lambda h: h.update(extra=1)),
    ("frame_header", lambda h: h.update(camera_id="cam A")),
    ("frame_header", lambda h: h.update(stream_epoch=-1)),
    ("frame_header", lambda h: h.update(frame_sequence="3")),
    ("frame_header", lambda h: h.update(timestamp_source="GUESS")),
    ("frame_header", lambda h: h.update(timestamp_uncertainty_ms=-1)),
    ("frame_header", lambda h: h.update(pts_timebase="90000")),
    ("frame_header", lambda h: h.update(keyframe=1)),
    ("frame_header", lambda h: h.update(received_at_ms=-5)),
    ("codec_info", lambda c: c.update(codec="H265")),
    ("codec_info", lambda c: c.update(sps="!!")),
    ("codec_info", lambda c: c.update(width=0)),
    ("codec_info", lambda c: c.update(nominal_fps=0)),
    ("codec_info", lambda c: c.pop("pps")),
    ("codec_info", lambda c: c.update(bitrate=1)),
]


@pytest.mark.parametrize("name,mutate", MUTATIONS)
def test_validators_agree_with_contract_schema(name, mutate):
    validate = vi.validate_frame_header if name == "frame_header" else vi.validate_codec_info
    good = _example(name)
    assert schema_errors(good, name) == []
    validate(good)
    bad = copy.deepcopy(good)
    mutate(bad)
    assert schema_errors(bad, name) != []
    with pytest.raises(vi.HeaderError):
        validate(bad)


def test_received_example_and_null_pts_are_valid():
    vi.validate_frame_header(_example("frame_header.received"))
    vi.validate_frame_header({**_example("frame_header"), "source_pts": None})


# ---------------------------------------------------------------------------- 토큰


def test_token_verifier_uses_hashes_only():
    v = vi.TokenVerifier(vi.parse_token_hashes(f" {vi.token_sha256('old')} ,{vi.token_sha256(TOKEN)}"))
    assert v.verify(TOKEN) and v.verify("old")
    assert not v.verify("x") and not v.verify(None) and not v.verify("")
    assert TOKEN not in repr(v)
    with pytest.raises(ValueError):
        vi.parse_token_hashes("sha256:abc")
    with pytest.raises(ValueError):
        vi.TokenVerifier(frozenset())


# ---------------------------------------------------------------------------- 스트림 상태·손실 (소켓 없이)


def test_stream_counts_loss_and_laptop_skipped_separately():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    assert not s.on_frame(_header(seq=0, key=True))  # codec_info 전
    assert s.on_codec_info(_codec())
    assert not s.on_frame(_header(seq=0, key=False))  # 키프레임 전
    assert s.on_frame(_header(seq=1, key=True)) and s.ready
    assert s.on_frame(_header(seq=2))
    assert s.on_frame(_header(seq=5))  # 3,4 손실
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 6, "last_sequence": 9})
    assert s.on_frame(_header(seq=12, key=True))  # 6~9 노트북 skipped, 10·11 손실
    assert not s.on_frame(_header(seq=12))  # 지난 번호
    st = s.stats
    assert (st.gateway_to_runpod_loss, st.laptop_skipped) == (4, 4)
    assert (st.before_codec_info, st.before_keyframe, st.out_of_order) == (1, 1, 1)
    assert st.frames_forwarded == 4


def test_stream_continues_numbering_across_reconnect_and_resets_on_new_epoch():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec(epoch=3))
    s.on_frame(_header(epoch=3, seq=0, key=True))
    s.connection_ended()
    s.connection_started()  # 노트북 재접속: 같은 epoch codec_info → 키프레임
    assert not s.on_frame(_header(epoch=3, seq=1))  # 재접속 뒤 codec_info 전
    assert s.on_codec_info(_codec(epoch=3))
    assert s.on_frame(_header(epoch=3, seq=40, key=True))
    # 1~39: codec_info 전에 와서 버린 1 은 번호를 받지 않았으므로(규칙 위반, before_codec_info) 빈칸에도 든다
    assert s.stats.gateway_to_runpod_loss == 39 and s.stats.before_codec_info == 1
    assert s.on_codec_info(_codec(epoch=4))  # 새 epoch: 번호를 새로 센다
    assert s.on_frame(_header(epoch=4, seq=100, key=True))  # 첫 번호 앞은 모른다
    assert s.stats.gateway_to_runpod_loss == 39
    assert not s.on_codec_info(_codec(epoch=2)) and not s.on_frame(_header(epoch=2, seq=0, key=True))
    assert s.stats.stale_epoch == 2


def test_stream_ignores_skip_notice_for_other_epoch_or_past_range():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec())
    s.on_frame(_header(seq=0, key=True))
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 7, "first_sequence": 1, "last_sequence": 3})
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 0, "last_sequence": 0})  # 지난 범위
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 0, "last_sequence": 2})  # 일부만 남음
    s.on_frame(_header(seq=4, key=True))
    assert s.stats.ignored_skip_notices == 2
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss) == (2, 1)  # 1·2 skipped, 3 손실


# ---------------------------------------------------------------------------- 실제 TLS


def _make_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tier0-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_file, key_file = directory / "tier0.crt", directory / "tier0.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


class Recorder:
    def __init__(self, slow: dict[str, float] | None = None) -> None:
        self.codecs: list[dict] = []
        self.frames: list[tuple[dict, bytes]] = []
        self.disconnected: list[str] = []
        self.slow = slow or {}
        self.lock = threading.Lock()

    def on_codec_info(self, codec_info):
        with self.lock:
            self.codecs.append(dict(codec_info))

    def on_frame(self, header, data):
        time.sleep(self.slow.get(header["camera_id"], 0))
        with self.lock:
            self.frames.append((dict(header), data))

    def on_disconnected(self, camera_id):
        with self.lock:
            self.disconnected.append(camera_id)

    def seqs(self, cam: str) -> list[int]:
        with self.lock:
            return [h["frame_sequence"] for h, _ in self.frames if h["camera_id"] == cam]


def wait_until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


@pytest.fixture
def server_factory(tmp_path):
    cert, key = _make_cert(tmp_path)
    servers = []

    def make(consumer=None, cams=("cam_A", "cam_B", "cam_C", "cam_D")):
        server = vi.VideoIngestServer([vi.ListenSpec(0, frozenset(cams), "127.0.0.1")],
                                      vi.server_context(str(cert), str(key)),
                                      vi.TokenVerifier(frozenset({vi.token_sha256(TOKEN)})), consumer,
                                      hello_timeout_s=2.0)
        server.start()
        servers.append(server)
        return server

    yield make
    for s in servers:
        s.stop()


def _client(port: int, cam: str, token: str = TOKEN) -> tuple[ssl.SSLSocket, dict]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 시험 클라이언트(서버 쪽 시험). 노트북 쪽 공개키 고정은 gateway/relay 시험이 본다
    tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname=None)
    tls.sendall(vi.encode(vi.HELLO, {"protocol": vi.PROTOCOL, "camera_id": cam, "laptop_id": "laptop-01",
                                     "device_token": token}))
    reply = vi.read_record(tls, max_data=0)
    assert reply is not None and reply[0] == vi.HELLO_ACK
    return tls, reply[1]


def _send(tls, cam, seqs, *, epoch=0, gop=10, codec=True):
    if codec:
        tls.sendall(vi.encode(vi.CODEC_INFO, _codec(cam, epoch)))
    for s in seqs:
        tls.sendall(vi.encode(vi.FRAME, _header(cam, epoch, s, s % gop == 0), b"\x00\x00\x00\x01" + bytes([s % 256])))


def test_four_cameras_with_token_deliver_contract_frames(server_factory):
    rec = Recorder()
    server = server_factory(rec)
    port = server.ports[0]
    clients = {}
    for cam in ("cam_A", "cam_B", "cam_C", "cam_D"):
        tls, ack = _client(port, cam)
        assert ack == {"ok": True}
        clients[cam] = tls
    for cam, tls in clients.items():
        _send(tls, cam, range(30))
    wait_until(lambda: all(len(rec.seqs(c)) == 30 for c in clients))
    assert sorted(server.connected_cameras()) == sorted(clients)
    for snap in server.snapshots():
        assert snap["gateway_to_runpod_loss"] == 0 and snap["ready"] and snap["connected"]
    for tls in clients.values():
        tls.close()
    wait_until(lambda: len(rec.disconnected) == 4)


def test_wrong_token_is_rejected_before_any_record_and_not_logged(server_factory, caplog):
    caplog.set_level(logging.DEBUG)
    rec = Recorder()
    server = server_factory(rec)
    tls, ack = _client(server.ports[0], "cam_A", token="wrong-S3CRET")
    assert ack == {"ok": False, "reason": "unauthorized"}
    with pytest.raises((OSError, ssl.SSLError)):
        for _ in range(50):  # 서버가 닫았다: 결국 송신이 실패한다
            _send(tls, "cam_A", range(10))
            time.sleep(0.02)
    assert rec.frames == [] and rec.codecs == [] and server.stats.auth_rejections == 1
    tls2, ack2 = _client(server.ports[0], "cam_Z")  # 토큰은 맞지만 이 포트에 없는 카메라
    assert ack2 == {"ok": False, "reason": "unknown_camera"}
    tls2.close()
    assert "S3CRET" not in caplog.text


def test_record_camera_mismatch_closes_connection(server_factory):
    rec = Recorder()
    server = server_factory(rec)
    tls, _ = _client(server.ports[0], "cam_A")
    tls.sendall(vi.encode(vi.CODEC_INFO, _codec("cam_B")))
    wait_until(lambda: server.streams["cam_A"].stats.protocol_errors == 1)
    assert rec.codecs == []
    tls.close()


def test_invalid_header_closes_connection(server_factory):
    rec = Recorder()
    server = server_factory(rec)
    tls, _ = _client(server.ports[0], "cam_A")
    tls.sendall(vi.encode(vi.CODEC_INFO, _codec("cam_A")))
    bad = _header("cam_A", 0, 0, True)
    bad.pop("timestamp_uncertainty_ms")
    tls.sendall(vi.encode(vi.FRAME, bad, b"x"))
    wait_until(lambda: server.streams["cam_A"].stats.protocol_errors == 1)
    assert rec.frames == []
    tls.close()


def test_slow_consumer_on_one_camera_does_not_block_others(server_factory):
    rec = Recorder(slow={"cam_A": 0.2})
    server = server_factory(rec)
    a, _ = _client(server.ports[0], "cam_A")
    b, _ = _client(server.ports[0], "cam_B")
    _send(a, "cam_A", range(20))
    t0 = time.monotonic()
    _send(b, "cam_B", range(50))
    wait_until(lambda: len(rec.seqs("cam_B")) == 50, 3)
    assert time.monotonic() - t0 < 2.0  # cam_A 가 다 넘어가려면 4초
    assert len(rec.seqs("cam_A")) < 20
    a.close()
    b.close()


def test_reconnect_supersedes_old_connection_and_counts_loss(server_factory):
    rec = Recorder()
    server = server_factory(rec)
    old, _ = _client(server.ports[0], "cam_A")
    _send(old, "cam_A", range(15), epoch=5)
    wait_until(lambda: len(rec.seqs("cam_A")) == 15)
    new, ack = _client(server.ports[0], "cam_A")  # 노트북 재접속(이전 연결이 조용히 죽은 경우)
    assert ack == {"ok": True}
    _send(new, "cam_A", [20, 21, 22], epoch=5, gop=10)  # 같은 epoch: codec_info → 키프레임 20
    tls_skip = vi.encode(vi.SKIP_NOTICE, {"camera_id": "cam_A", "stream_epoch": 5, "first_sequence": 23,
                                         "last_sequence": 29})
    new.sendall(tls_skip)
    new.sendall(vi.encode(vi.FRAME, _header("cam_A", 5, 30, True), b"x"))
    wait_until(lambda: rec.seqs("cam_A")[-1:] == [30])
    st = server.streams["cam_A"].stats
    assert st.superseded == 1 and st.connections == 2
    assert st.gateway_to_runpod_loss == 5 and st.laptop_skipped == 7  # 15~19 손실, 23~29 노트북 skipped
    assert rec.seqs("cam_A") == [*range(15), 20, 21, 22, 30]
    assert [c["stream_epoch"] for c in rec.codecs] == [5, 5]
    new.close()
    old.close()


# ---------------------------------------------------------------------------- 종료 직후 듣기 포트 (S15P31A101-251)
# 수정 전 Linux 는 accept 스레드가 poll(시간 제한 0.5s) 안에 있으면 close() 뒤에도 그동안 LISTEN 이 남았다. 아래 시험은
# stop() 직후(join 전, 기다림 없이) 포트를 본다. Windows 는 close 만으로 닫혀 수정 전에도 통과한다.

def _server(tmp_path, ports, **kw) -> vi.VideoIngestServer:
    cert, key = _make_cert(tmp_path)
    cams = [f"cam_{c}" for c in "ABCD"]
    return vi.VideoIngestServer([vi.ListenSpec(p, frozenset({cams[i]}), "127.0.0.1") for i, p in enumerate(ports)],
                                vi.server_context(str(cert), str(key)),
                                vi.TokenVerifier(frozenset({vi.token_sha256(TOKEN)})), **kw)


def _refused(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_stop_closes_every_listening_port_before_join(tmp_path):
    """반례(순서): accept 스레드가 기다리는 중에 stop 하면 join 을 기다리지 않아도 모든 포트가 바로 닫혀 있다. 두 번 불러도 같다."""
    server = _server(tmp_path, [0, 0, 0])
    server.start()
    assert len(server.alive_threads()) == 3
    server.stop()
    assert [p for p in server.ports if not _refused(p)] == []
    server.stop()  # 이미 닫힌 소켓의 shutdown·close 도 조용히 넘어간다
    assert server.join(3.0) == []


def test_stop_with_live_and_half_open_connections_closes_listener_and_threads_end(tmp_path):
    """반례(동시성): 인증된 연결과 HELLO 를 안 보낸 TCP 연결이 붙어 있어도 stop 직후 듣기 포트는 닫히고 모든 스레드가 끝난다."""
    server = _server(tmp_path, [0], hello_timeout_s=2.0)
    server.start()
    port = server.ports[0]
    tls, ack = _client(port, "cam_A")
    assert ack == {"ok": True}
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)  # TLS 핸드셰이크 중에 멈춘 연결
    wait_until(lambda: len(server.alive_threads()) == 3)
    server.stop()
    assert _refused(port)
    assert server.join(5.0) == []
    tls.close()
    raw.close()


def test_restart_on_same_port_right_after_stop_accepts_again(tmp_path):
    """반례(재시작): stop 직후 같은 포트에 새 서버를 띄울 수 있고 노트북이 다시 붙는다(남은 LISTEN 이 있으면 Linux bind 가 실패한다)."""
    first = _server(tmp_path, [0])
    first.start()
    port = first.ports[0]
    old, _ = _client(port, "cam_A")
    first.stop()
    second = _server(tmp_path, [port])
    try:
        second.start()
        assert second.ports == [port]
        tls, ack = _client(port, "cam_A")
        assert ack == {"ok": True}
        tls.close()
    finally:
        second.stop()
        old.close()
    assert first.join(3.0) == [] and second.join(3.0) == []


def test_listen_specs_from_env_uses_runpod_ports_and_refuses_unknown():
    env = {"SIMAI_TIER0_VIDEO_PORTS": "cam1:70001,cam2:70001,cam3:70003", "RUNPOD_TCP_PORT_70001": "40001",
           "RUNPOD_TCP_PORT_70003": "40003"}
    specs = vi.listen_specs_from_env(env)
    assert [(s.port, sorted(s.cameras)) for s in specs] == [(40001, ["cam1", "cam2"]), (40003, ["cam3"])]
    with pytest.raises(ValueError, match="RUNPOD_TCP_PORT_70003"):
        vi.listen_specs_from_env({**env, "RUNPOD_TCP_PORT_70003": ""})
    with pytest.raises(ValueError):
        vi.build_server({**env, "SIMAI_TIER0_TLS_CERT_FILE": "", "SIMAI_TIER0_TLS_KEY_FILE": ""})


# ---------------------------------------------------------------------------- 리뷰 반영: 중계 재시작, 재접속 경계의 skipped


def test_relay_restart_with_start_ms_epoch_resumes_and_lower_epoch_is_still_refused(caplog):
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec(epoch=1760342400000))  # 중계 첫 epoch = 시작 UTC ms
    s.on_frame(_header(epoch=1760342400000, seq=0, key=True))
    s.on_codec_info(_codec(epoch=1760342400002))  # RTSP 재접속 두 번
    s.on_frame(_header(epoch=1760342400002, seq=0, key=True))
    s.connection_ended()
    s.connection_started()  # 중계 프로세스 재시작: 새 첫 epoch 는 새 시작 UTC ms
    assert s.on_codec_info(_codec(epoch=1760342460000))
    assert s.on_frame(_header(epoch=1760342460000, seq=0, key=True)) and s.ready
    # 같은 연결 안에서 epoch 역행·번호 중복은 여전히 버린다
    caplog.set_level(logging.WARNING)
    assert not s.on_codec_info(_codec(epoch=0))
    assert not s.on_frame(_header(epoch=1760342400002, seq=1, key=True))
    assert not s.on_frame(_header(epoch=1760342460000, seq=0, key=True))
    assert s.stats.stale_epoch == 2 and s.stats.out_of_order == 1
    assert "stream_epoch" in caplog.text


def test_skip_notice_survives_reconnect_before_keyframe():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec())
    s.on_frame(_header(seq=0, key=True))
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 1, "last_sequence": 8})
    s.connection_ended()  # 끝내는 키프레임 전에 끊김
    s.connection_started()
    s.on_codec_info(_codec())
    # 노트북은 정산 전 범위를 다시 보낸다(겹쳐도 한 번만 센다)
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 1, "last_sequence": 8})
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 6, "last_sequence": 10})
    assert s.on_frame(_header(seq=13, key=True))
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss) == (10, 2)  # 1~10 skipped, 11·12 손실
    s.on_frame(_header(seq=15))  # 정산한 범위는 다시 쓰지 않는다
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss) == (10, 3)
    s.on_skip_notice({"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 16, "last_sequence": 20})
    assert s.on_codec_info(_codec(epoch=1))  # 새 epoch: 알린 범위를 버린다
    s.on_frame(_header(epoch=1, seq=0, key=True))
    s.on_frame(_header(epoch=1, seq=18))
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss) == (10, 20)


def test_skip_notice_over_tls_reconnect_counts_skipped_not_loss(server_factory):
    server = server_factory(Recorder())
    one, _ = _client(server.ports[0], "cam_A")
    _send(one, "cam_A", [0])
    one.sendall(vi.encode(vi.SKIP_NOTICE, {"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 1,
                                          "last_sequence": 8}))
    wait_until(lambda: server.streams["cam_A"]._skip_spans == [[1, 8]])
    one.close()
    wait_until(lambda: "cam_A" not in server.connected_cameras())
    two, _ = _client(server.ports[0], "cam_A")
    _send(two, "cam_A", [9], gop=9)
    wait_until(lambda: server.streams["cam_A"].last_sequence == 9)
    st = server.streams["cam_A"].stats
    assert (st.laptop_skipped, st.gateway_to_runpod_loss, st.connections) == (8, 0, 2)
    two.close()


def test_lower_epoch_gets_stream_reject_and_duplicates_stay_refused(server_factory):
    rec = Recorder()
    server = server_factory(rec)
    port = server.ports[0]
    one, _ = _client(port, "cam_A")
    _send(one, "cam_A", [0], epoch=10)
    wait_until(lambda: rec.seqs("cam_A") == [0])
    one.close()
    wait_until(lambda: "cam_A" not in server.connected_cameras())
    two, _ = _client(port, "cam_A")  # 줄어든 epoch: 새 TLS 연결이어도 받지 않고 이유를 알린다
    two.sendall(vi.encode(vi.CODEC_INFO, _codec("cam_A", 3)))
    reply = vi.read_record(two, max_data=0)
    assert reply is not None and reply[0] == vi.STREAM_REJECT
    assert reply[1] == {"reason": "stale_epoch", "stream_epoch": 3, "current_epoch": 10}
    two.close()
    wait_until(lambda: "cam_A" not in server.connected_cameras())
    st = server.streams["cam_A"].stats
    assert st.stream_rejects == 1 and st.stale_epoch == 1 and [c["stream_epoch"] for c in rec.codecs] == [10]
    three, _ = _client(port, "cam_A")  # 같은 스트림의 전송 재접속: 번호 비교는 그대로
    _send(three, "cam_A", [0, 5], epoch=10, gop=5)
    wait_until(lambda: rec.seqs("cam_A") == [0, 5])
    assert st.out_of_order == 1 and st.gateway_to_runpod_loss == 4
    three.close()


# ---------------------------------------------------------------------------- 리뷰 반영: 버림 구간 집합·메모리 상한·구분 불가


def _notice(first: int, last: int, epoch: int = 0, **extra) -> dict:
    return {"camera_id": "cam_A", "stream_epoch": epoch, "first_sequence": first, "last_sequence": last, **extra}


def test_repeated_skip_notices_do_not_grow_receiver_memory():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec())
    s.on_frame(_header(seq=0, key=True))
    for _ in range(10_000):  # 재접속마다 같은 구간 집합을 다시 받음
        s.on_skip_notice(_notice(1, 3))
        s.on_skip_notice(_notice(5, 6))
        s.on_skip_notice(_notice(4, 4))  # 맞닿으면 합친다
    assert s._skip_spans == [[1, 6]]
    s.on_frame(_header(seq=8, key=True))
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss) == (6, 1) and s._skip_spans == []


def test_more_than_64_separate_skip_runs_all_count_as_skipped():
    s = vi.CameraStream("cam_A")
    s.connection_started()
    s.on_codec_info(_codec())
    s.on_frame(_header(seq=0, key=True))
    for k in range(200):  # 서로 떨어진 버림 구간 200개(각 2프레임), 사이 번호는 손실
        s.on_skip_notice(_notice(1 + 3 * k, 2 + 3 * k))
    s.on_frame(_header(seq=600, key=True))
    assert (s.stats.laptop_skipped, s.stats.gateway_to_runpod_loss, s.stats.unattributed_gap) == (400, 199, 0)


def test_span_cap_marks_unattributed_instead_of_inventing_loss():
    s = vi.CameraStream("cam_A")
    s.max_skip_spans = 4
    s.connection_started()
    s.on_codec_info(_codec())
    s.on_frame(_header(seq=0, key=True))
    for k in range(6):  # 구간 6개: 오래된 2개(1~2, 4~5)를 잘라 낸다
        s.on_skip_notice(_notice(1 + 3 * k, 2 + 3 * k))
    assert len(s._skip_spans) == 4 and s.stats.skip_spans_truncated == 2
    s.on_frame(_header(seq=20, key=True))
    st = s.stats
    # 잘라 낸 구간 끝(5)까지 1~5 는 손실인지 버림인지 모른다(구분 불가 5). 7~17 은 구간 4개(8) 버림, 6·9·12·15·18·19 손실
    assert (st.unattributed_gap, st.laptop_skipped, st.gateway_to_runpod_loss) == (5, 8, 6)
    # 노트북이 잘라 냈다고 알리면(unknown_before) 그 앞 빈칸도 손실로 세지 않는다
    s.on_skip_notice(_notice(30, 31, unknown_before=28))
    s.on_frame(_header(seq=33, key=True))
    assert (st.unattributed_gap, st.laptop_skipped, st.gateway_to_runpod_loss) == (5 + 7, 10, 6 + 3)  # 21~27 구분 불가
    vi.validate_skip_notice(_notice(30, 31, unknown_before=28))
    s.on_codec_info(_codec(epoch=1))  # 새 epoch 에서 비운다
    assert s._skip_spans == [] and s._unknown_upto is None
