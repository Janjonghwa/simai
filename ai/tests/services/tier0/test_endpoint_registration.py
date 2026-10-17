"""Tier 0 엔드포인트 등록(S15P31A101-159): 값 모으기, 공개키 지문, 계약, 다시 보내기, 비밀 값 비노출."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.endpoint_registration import (  # noqa: E402
    Backoff,
    EndpointConfigError,
    EndpointRegistrar,
    HttpTransport,
    RegistrationFailed,
    TransportError,
    build_payload,
    cert_der_from_pem,
    main,
    spki_der_from_cert_der,
    spki_sha256,
)
from simai_ai.providers.contract import schema_errors  # noqa: E402

TOKEN = "tier0-secret-token-7f3a"
T0 = 1760342400000


# ---------------------------------------------------------------------------- 시험용 인증서(DER 를 손으로 짠다)


def tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(raw)]) + raw + content


SPKI = tlv(0x30, tlv(0x30, tlv(0x06, bytes.fromhex("2a8648ce3d0201")) + tlv(0x06, bytes.fromhex("2a8648ce3d030107")))
           + tlv(0x03, b"\x00\x04" + bytes(range(64))))


def fake_cert_der(*, with_version: bool = True, spki: bytes = SPKI) -> bytes:
    name = tlv(0x30, tlv(0x31, tlv(0x30, tlv(0x06, b"\x55\x04\x03") + tlv(0x0C, b"tier0" * 40))))  # 긴 길이 형식
    validity = tlv(0x30, tlv(0x17, b"261017000000Z") + tlv(0x17, b"361017000000Z"))
    tbs = (tlv(0xA0, tlv(0x02, b"\x02")) if with_version else b"") + tlv(0x02, b"\x01\x23") + tlv(0x30, tlv(0x06, b"\x2a"))
    tbs += name + validity + name + spki + tlv(0xA3, tlv(0x30, b""))
    return tlv(0x30, tlv(0x30, tbs) + tlv(0x30, tlv(0x06, b"\x2a")) + tlv(0x03, b"\x00" + b"\x11" * 70))


def pem(der: bytes) -> bytes:
    b64 = base64.encodebytes(der).replace(b"\n", b"\n")
    return b"-----BEGIN CERTIFICATE-----\n" + b64 + b"-----END CERTIFICATE-----\n"


CERT_PEM = pem(fake_cert_der())
EXPECTED_FP = "sha256:" + hashlib.sha256(SPKI).hexdigest()


def runpod_env(**over: str) -> dict[str, str]:
    env = {
        "SIMAI_TIER0_ID": "tier0-01",
        "SIMAI_TIER0_CONTROL_PORT": "70000",
        "SIMAI_TIER0_VIDEO_PORTS": "cam1:70001, cam2:70002,cam3:70003,cam4:70004",
        "RUNPOD_PUBLIC_IP": "203.0.113.24",
        "RUNPOD_TCP_PORT_70000": "44464",
        "RUNPOD_TCP_PORT_70001": "44465",
        "RUNPOD_TCP_PORT_70002": "44466",
        "RUNPOD_TCP_PORT_70003": "44467",
        "RUNPOD_TCP_PORT_70004": "44468",
        "RUNPOD_POD_ID": "abc123xyz",
        "RUNPOD_DC_ID": "AP-JP-1",
        "SIMAI_TIER0_TOKEN": TOKEN,
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def registrar(transport, sleeps: list[float] | None = None, **kw) -> EndpointRegistrar:
    ids = iter(f"0192a6f0-7c1e-7b3a-9f2d-{i:012d}" for i in range(1, 100))
    return EndpointRegistrar(
        transport,
        clock_ms=lambda: T0,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        backoff=Backoff(initial_s=0.5, factor=2.0, max_s=2.0),
        new_msg_id=lambda: next(ids),
        **kw,
    )


# ---------------------------------------------------------------------------- 공개키 지문


def test_spki_extracted_with_and_without_version() -> None:
    assert spki_der_from_cert_der(fake_cert_der()) == SPKI
    assert spki_der_from_cert_der(fake_cert_der(with_version=False)) == SPKI
    assert spki_sha256(fake_cert_der()) == EXPECTED_FP


@pytest.mark.parametrize("bad", [b"", b"\x30\x05\x30\x03", fake_cert_der()[:-3], b"\x02\x01\x00"])
def test_spki_rejects_broken_der(bad: bytes) -> None:
    with pytest.raises(ValueError):
        spki_der_from_cert_der(bad)


def test_pem_with_private_key_is_refused() -> None:
    mixed = b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n" + CERT_PEM
    with pytest.raises(ValueError, match="비밀 키"):
        cert_der_from_pem(mixed)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl 명령이 없다")
def test_spki_matches_openssl_for_generated_cert(tmp_path: Path) -> None:
    """시험 중에 자체 서명 인증서를 만들어(커밋하지 않음) openssl 이 꺼낸 공개키 DER 의 SHA-256 과 비교한다."""
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=tier0-test"],
                   check=True, capture_output=True)
    pub = subprocess.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
                         check=True, capture_output=True).stdout
    pub_der = base64.b64decode(b"".join(pub.split(b"-----")[2].split()))
    assert spki_sha256(cert_der_from_pem(cert.read_bytes())) == "sha256:" + hashlib.sha256(pub_der).hexdigest()


# ---------------------------------------------------------------------------- 값 모으기와 계약


def test_payload_from_runpod_env_matches_contract() -> None:
    reg = registrar(lambda *a: 201)
    msg = reg.build(runpod_env(), CERT_PEM)
    assert not schema_errors(msg, "tier0_endpoint"), schema_errors(msg, "tier0_endpoint")
    p = msg["payload"]
    assert msg["source_id"] == p["gateway_id"] == "tier0-01"  # 스키마 밖 규칙
    assert p["public_ip"] == "203.0.113.24"
    assert p["tls_public_key_sha256"] == EXPECTED_FP
    assert p["registered_at"] == T0 == msg["sent_at"]
    assert p["ports"][0] == {"role": "CONTROL", "requested_port": 70000, "public_port": 44464}
    videos = [x for x in p["ports"] if x["role"] == "VIDEO"]
    assert [v["camera_id"] for v in videos] == ["cam1", "cam2", "cam3", "cam4"]  # VIDEO camera_id 유일
    assert [v["public_port"] for v in videos] == [44465, 44466, 44467, 44468]
    assert p["pod_id"] == "abc123xyz" and p["datacenter"] == "AP-JP-1"
    assert TOKEN not in json.dumps(msg)  # 비밀 값을 싣지 않는다


def test_cameras_may_share_one_video_port() -> None:
    env = runpod_env(SIMAI_TIER0_VIDEO_PORTS="cam1:70001,cam2:70001")
    payload = build_payload(env, cert_pem=CERT_PEM, registered_at_ms=T0)
    videos = [x for x in payload["ports"] if x["role"] == "VIDEO"]
    assert [(v["camera_id"], v["public_port"]) for v in videos] == [("cam1", 44465), ("cam2", 44465)]


def test_optional_runpod_values_are_omitted_when_unknown() -> None:
    env = runpod_env()
    del env["RUNPOD_POD_ID"], env["RUNPOD_DC_ID"]
    payload = build_payload(env, cert_pem=CERT_PEM, registered_at_ms=T0)
    assert "pod_id" not in payload and "datacenter" not in payload


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        ({"RUNPOD_PUBLIC_IP": None}, "RUNPOD_PUBLIC_IP 가 없다"),
        ({"RUNPOD_PUBLIC_IP": "203.0.113.024"}, "IPv4"),
        ({"RUNPOD_PUBLIC_IP": "0.0.0.0"}, "접속할 수 있는"),
        ({"RUNPOD_TCP_PORT_70000": None}, "RUNPOD_TCP_PORT_70000 가 없다"),
        ({"RUNPOD_TCP_PORT_70003": None}, "RUNPOD_TCP_PORT_70003 가 없다"),
        ({"RUNPOD_TCP_PORT_70001": "70001"}, "1~65535"),
        ({"RUNPOD_TCP_PORT_70001": "²"}, "1~65535"),
        ({"SIMAI_TIER0_CONTROL_PORT": None}, "SIMAI_TIER0_CONTROL_PORT 가 없다"),
        ({"SIMAI_TIER0_CONTROL_PORT": "8443"}, "대칭 매핑"),
        ({"SIMAI_TIER0_VIDEO_PORTS": None}, "SIMAI_TIER0_VIDEO_PORTS 가 없다"),
        ({"SIMAI_TIER0_VIDEO_PORTS": "cam1"}, "형식"),
        ({"SIMAI_TIER0_VIDEO_PORTS": "cam1:70001,cam1:70002"}, "두 번"),
        ({"SIMAI_TIER0_VIDEO_PORTS": "cam1:70000"}, "같이 쓴다"),
        ({"SIMAI_TIER0_ID": "Tier0"}, "SIMAI_TIER0_ID"),
        ({"RUNPOD_POD_ID": "pod/1"}, "RUNPOD_POD_ID"),
    ],
)
def test_unknown_or_bad_values_are_not_invented(change: dict, expect: str) -> None:
    sent: list = []
    reg = registrar(lambda path, body, t: sent.append(body) or 201)
    with pytest.raises(EndpointConfigError, match=expect):
        reg.register(runpod_env(**change), CERT_PEM)
    assert sent == []  # 모르는 값으로는 보내지 않는다


def test_missing_cert_is_registration_failure_and_all_problems_are_reported() -> None:
    env = runpod_env(RUNPOD_PUBLIC_IP=None, RUNPOD_TCP_PORT_70002=None)
    with pytest.raises(EndpointConfigError) as info:
        build_payload(env, cert_pem=None, registered_at_ms=T0)
    text = str(info.value)
    assert "RUNPOD_PUBLIC_IP" in text and "RUNPOD_TCP_PORT_70002" in text and "지문을 모른다" in text
    assert len(info.value.problems) == 3


# ---------------------------------------------------------------------------- 다시 보내기


class ScriptedTransport:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def post(self, path: str, body, timeout_s: float) -> int:
        self.bodies.append(body)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_retries_no_response_429_and_5xx_with_backoff_and_same_envelope() -> None:
    t = ScriptedTransport(TransportError("ConnectionRefusedError"), 503, 429, 500, 201)
    sleeps: list[float] = []
    done = registrar(t, sleeps).register(runpod_env(), CERT_PEM)
    assert done.attempts == 5 and done.status == 201
    assert sleeps == [0.5, 1.0, 2.0, 2.0]
    assert len({b["msg_id"] for b in t.bodies}) == 1  # 같은 등록을 다시 보낸다(EC2 는 msg_id 로 중복 제거)


@pytest.mark.parametrize("status", [401, 403, 400, 404, 302])
def test_rejections_are_not_retried(status: int) -> None:
    t = ScriptedTransport(status)
    with pytest.raises(RegistrationFailed) as info:
        registrar(t).register(runpod_env(), CERT_PEM)
    assert info.value.retryable is False and info.value.attempts == 1 and len(t.bodies) == 1


def test_gives_up_after_max_attempts() -> None:
    t = ScriptedTransport(*([503] * 3))
    sleeps: list[float] = []
    with pytest.raises(RegistrationFailed) as info:
        registrar(t, sleeps, max_attempts=3).register(runpod_env(), CERT_PEM)
    assert info.value.retryable is True and info.value.attempts == 3 and len(sleeps) == 2


# ---------------------------------------------------------------------------- HTTP 전송과 비밀 값


class _Recorder(http.server.BaseHTTPRequestHandler):
    status = 201
    location: str | None = None
    seen: list = []

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        type(self).seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
        self.send_response(type(self).status)
        if type(self).location:
            self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def _server(status: int, location: str | None = None):
    handler = type("H", (_Recorder,), {"status": status, "location": location, "seen": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


def test_http_transport_posts_with_token_and_does_not_follow_redirect() -> None:
    other, other_h = _server(201)
    srv, h = _server(307, location=f"http://127.0.0.1:{other.server_address[1]}/api/endpoints/tier0")
    try:
        transport = HttpTransport(f"http://127.0.0.1:{srv.server_address[1]}", TOKEN)
        with pytest.raises(RegistrationFailed):
            registrar(transport).register(runpod_env(), CERT_PEM)
        assert h.seen[0][0] == "/api/endpoints/tier0" and h.seen[0][1] == f"Bearer {TOKEN}"
        assert h.seen[0][2]["msg_type"] == "tier0_endpoint"
        assert other_h.seen == []  # 리다이렉트를 따라가 토큰을 다른 곳에 싣지 않는다
    finally:
        srv.shutdown()
        other.shutdown()


def test_http_transport_success_status() -> None:
    srv, h = _server(201)
    try:
        transport = HttpTransport(f"http://127.0.0.1:{srv.server_address[1]}/", TOKEN)
        assert registrar(transport).register(runpod_env(), CERT_PEM).status == 201
    finally:
        srv.shutdown()


def test_http_transport_rules_and_no_token_in_errors(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError):
        HttpTransport("http://10.0.0.5:8080", TOKEN)  # 평문 http 는 루프백만
    with pytest.raises(ValueError):
        HttpTransport("https://ec2.example", "")
    transport = HttpTransport("http://127.0.0.1:1", TOKEN)
    assert TOKEN not in repr(transport)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RegistrationFailed) as info:
        registrar(transport, max_attempts=2, request_timeout_s=1).register(runpod_env(), CERT_PEM)
    assert TOKEN not in str(info.value)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_main_reports_missing_values_without_secrets(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    env = runpod_env(SIMAI_EC2_BASE_URL="http://127.0.0.1:1", RUNPOD_PUBLIC_IP=None,
                     SIMAI_TIER0_TLS_CERT_FILE=str(tmp_path / "없음.pem"))
    assert main([], env=env) == 2
    err = capsys.readouterr().err
    assert "RUNPOD_PUBLIC_IP" in err and "지문을 모른다" in err and TOKEN not in err
    assert main([], env={"SIMAI_EC2_BASE_URL": "https://ec2.example"}) == 2


def test_main_registers_with_cert_file(tmp_path: Path) -> None:
    cert = tmp_path / "tier0.crt"
    cert.write_bytes(CERT_PEM)
    srv, h = _server(201)
    try:
        env = runpod_env(SIMAI_EC2_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}",
                         SIMAI_TIER0_TLS_CERT_FILE=str(cert))
        assert main(["--max-attempts", "1"], env=env) == 0
        assert h.seen[0][2]["payload"]["tls_public_key_sha256"] == EXPECTED_FP
    finally:
        srv.shutdown()


def test_entrypoint_help_runs_from_ai_dir() -> None:
    result = subprocess.run([sys.executable, "-m", "services.tier0.endpoint_registration", "--help"],
                            cwd=AI_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
