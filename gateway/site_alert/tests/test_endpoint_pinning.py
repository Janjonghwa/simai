"""엔드포인트 조회·공개키 고정(S15P31A101-159).

- 응답 해석: 계약 예시, 스키마 밖 규칙(VIDEO camera_id 유일, source_id 일치), 틀린 값 거부
- EC2 조회: 기기 토큰, 404·401·5xx, 리다이렉트 안 따름, 평문 http 는 루프백만
- 공개키 지문: cryptography 기준값과 같음, 고정 목록 해석
- 제어 채널(로컬 TLS 서버, 시험 중 만든 자체 서명 인증서): Pod 포트·IP 가 바뀌면 수동 설정 없이 다시 붙음,
  지문 불일치는 접속 안 함(토큰을 보내기 전), 조회 실패 재시도, 비밀 값이 로그·오류에 없음
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

import pytest
from conftest import load_example
from tls_helpers import FakeEc2, RawTlsServer, TlsTier0, endpoint_doc, make_cert, wait_until

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.cli import build_control_channel
from simai_site_alert.config import Settings
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.endpoint import (
    Ec2EndpointLookup,
    EndpointAuthError,
    EndpointNotRegistered,
    EndpointResolver,
    EndpointUnavailable,
    InvalidEndpoint,
    parse_endpoint,
)
from simai_site_alert.pinning import (
    PinMismatchError,
    PinnedPeerVerifier,
    parse_pins,
    pinned_client_context,
    spki_sha256,
)
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import WebSocketControlChannel

TOKEN = "dev-token-9c1e-secret"
OTHER_PIN = "sha256:" + "0" * 64


@pytest.fixture
def certs(tmp_path: Path):
    return make_cert(tmp_path / "a", "tier0"), make_cert(tmp_path / "b", "impostor")


def channel_for(ec2: FakeEc2, pins: set[str], **kw) -> WebSocketControlChannel:
    lookup = Ec2EndpointLookup(ec2.base_url, device_token=TOKEN, timeout_s=2)
    resolver = EndpointResolver(lookup.fetch, frozenset(pins))
    kw.setdefault("reconnect_min_s", 0.05)
    kw.setdefault("reconnect_max_s", 0.2)
    return WebSocketControlChannel(resolver=resolver, device_token=TOKEN, **kw)


def assert_no_token_logged(caplog: pytest.LogCaptureFixture) -> None:
    """노트북 쪽 로그(websockets 클라이언트 DEBUG 헤더 포함)에 기기 토큰이 없다. 시험용 Tier 0 서버(websockets.server)가
    받은 헤더를 찍는 것은 노트북 로그가 아니라 뺀다."""
    client = [r for r in caplog.records if not r.name.startswith("websockets.server")]
    assert any(r.name.startswith("simai_site_alert") for r in client)
    assert all(TOKEN not in r.getMessage() for r in client), [r.getMessage() for r in client if TOKEN in r.getMessage()]


def run_app(channel: WebSocketControlChannel) -> tuple[SiteAlertApp, SiteAlertCore, FakeSoundBackend]:
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01")
    app = SiteAlertApp(core, channel, tick_s=60)
    app.start(test_alert_s=None)
    return app, core, sound


# ---------------------------------------------------------------------------- 응답 해석


def test_contract_example_parses(contract_validator) -> None:
    doc = load_example("tier0_endpoint")
    contract_validator("tier0_endpoint", doc)
    ep = parse_endpoint(doc)
    assert ep.control_url() == "wss://203.0.113.24:44464/control"
    assert ep.video_ports == {"cam1": 44465, "cam2": 44466, "cam3": 44467, "cam4": 44468}
    assert ep.tls_public_key_sha256 == doc["payload"]["tls_public_key_sha256"]


def test_helper_endpoint_doc_matches_contract(contract_validator) -> None:
    contract_validator("tier0_endpoint", endpoint_doc("127.0.0.1", 44464, "sha256:" + "ab" * 32))


def _mutate(path: str, value):
    doc = load_example("tier0_endpoint")
    target = doc
    keys = path.split(".")
    for k in keys[:-1]:
        target = target[int(k)] if isinstance(target, list) else target[k]
    last = keys[-1]
    if value is KeyError:
        del target[last]
    elif isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value
    return doc


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("msg_type", "health"),
        ("source_id", "tier0-02"),  # source_id 와 gateway_id 가 같아야 한다
        ("payload.public_ip", "203.0.113.024"),
        ("payload.public_ip", "0.0.0.0"),
        ("payload.public_ip", "tier0.example"),
        ("payload.tls_public_key_sha256", "sha256:ABC"),
        ("payload.tls_public_key_sha256", KeyError),
        ("payload.ports.0.public_port", 70000),
        ("payload.ports.0.public_port", True),
        ("payload.ports.0.role", "SSH"),
        ("payload.ports.0", {"role": "VIDEO", "camera_id": "cam2", "requested_port": 70009, "public_port": 1}),
        ("payload.registered_at", KeyError),
    ],
)
def test_invalid_endpoint_is_rejected(path: str, value) -> None:
    with pytest.raises(InvalidEndpoint):
        parse_endpoint(_mutate(path, value))


def test_endpoint_without_control_or_video_is_rejected() -> None:
    doc = load_example("tier0_endpoint")
    doc["payload"]["ports"] = [p for p in doc["payload"]["ports"] if p["role"] == "VIDEO"]
    with pytest.raises(InvalidEndpoint, match="CONTROL"):
        parse_endpoint(doc)
    doc = load_example("tier0_endpoint")
    doc["payload"]["ports"] = [p for p in doc["payload"]["ports"] if p["role"] == "CONTROL"]
    with pytest.raises(InvalidEndpoint, match="VIDEO"):
        parse_endpoint(doc)


# ---------------------------------------------------------------------------- EC2 조회


def test_lookup_sends_device_token_and_maps_errors() -> None:
    pin = "sha256:" + "ab" * 32
    with FakeEc2() as ec2:
        lookup = Ec2EndpointLookup(ec2.base_url + "/", device_token=TOKEN, timeout_s=2)
        with pytest.raises(EndpointNotRegistered):
            lookup.fetch()  # 아직 등록 없음(404)
        ec2.current = endpoint_doc("127.0.0.1", 44464, pin)
        ec2.fail_next = [401, 503]
        with pytest.raises(EndpointAuthError):
            lookup.fetch()
        with pytest.raises(EndpointUnavailable, match="503"):
            lookup.fetch()
        assert lookup.fetch().control_port == 44464
        assert ec2.requests[-1] == ("/api/endpoints/tier0", f"Bearer {TOKEN}")
        assert TOKEN not in repr(lookup)


def test_lookup_does_not_follow_redirect() -> None:
    with FakeEc2(endpoint_doc("127.0.0.1", 1, OTHER_PIN)) as other, FakeEc2() as ec2:
        ec2.fail_next = [302]
        lookup = Ec2EndpointLookup(ec2.base_url, device_token=TOKEN, timeout_s=2)
        with pytest.raises(EndpointUnavailable, match="302"):
            lookup.fetch()
        assert other.requests == []


@pytest.mark.parametrize("url", ["http://10.0.0.5:8080", "ftp://ec2.example", "ec2.example"])
def test_lookup_requires_https_except_loopback(url: str) -> None:
    with pytest.raises(ValueError):
        Ec2EndpointLookup(url, device_token=TOKEN)


def test_lookup_requires_token() -> None:
    with pytest.raises(ValueError):
        Ec2EndpointLookup("https://ec2.example", device_token="")


def test_lookup_unreachable_error_has_no_token() -> None:
    lookup = Ec2EndpointLookup("http://127.0.0.1:1", device_token=TOKEN, timeout_s=1)
    with pytest.raises(EndpointUnavailable) as info:
        lookup.fetch()
    assert TOKEN not in str(info.value)


# ---------------------------------------------------------------------------- 공개키 지문


def test_spki_fingerprint_matches_cryptography(tmp_path: Path) -> None:
    for i, rsa in enumerate((False, True)):
        cert = make_cert(tmp_path / str(i), rsa=rsa)
        assert spki_sha256(cert.der) == cert.fingerprint


def test_parse_pins() -> None:
    a, b = "sha256:" + "ab" * 32, "sha256:" + "cd" * 32
    assert parse_pins(f" {a} , {b.upper().replace('SHA256', 'sha256')} ,") == {a, b}
    assert parse_pins("") == frozenset() and parse_pins(None) == frozenset()
    with pytest.raises(ValueError):
        parse_pins("ab" * 32)  # 접두사 없음


def test_resolver_requires_pins_and_rejects_unpinned_fingerprint() -> None:
    doc = endpoint_doc("127.0.0.1", 44464, "sha256:" + "ab" * 32)
    with pytest.raises(ValueError):
        EndpointResolver(lambda: parse_endpoint(doc), frozenset())  # 고정값 없이는 접속하지 않는다
    resolver = EndpointResolver(lambda: parse_endpoint(doc), frozenset({OTHER_PIN}))
    with pytest.raises(PinMismatchError):
        resolver.resolve()  # EC2 가 알려 준 지문이 고정 목록에 없다
    with pytest.raises(PinMismatchError):
        PinnedPeerVerifier("sha256:" + "ab" * 32, frozenset({OTHER_PIN}))


def test_pinned_context_never_used_without_verifier() -> None:
    ctx = pinned_client_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError):
        WebSocketControlChannel(device_token=TOKEN)  # 주소도 조회도 없다
    with pytest.raises(ValueError):
        WebSocketControlChannel("wss://tier0.example:1/control", device_token=TOKEN,
                                resolver=EndpointResolver(lambda: None, frozenset({OTHER_PIN})))


def test_cli_builds_pinned_channel_only_with_all_settings() -> None:
    base = dict(laptop_id="laptop-01", ec2_base_url="https://ec2.example", device_token=TOKEN,
                pinned_spki="sha256:" + "ab" * 32, control_path="/control", ca_file=None)
    assert isinstance(build_control_channel(Settings(**base)), WebSocketControlChannel)
    for missing in ("ec2_base_url", "device_token", "pinned_spki"):
        with pytest.raises(ValueError) as info:
            build_control_channel(Settings(**{**base, missing: None}))
        assert TOKEN not in str(info.value)
    with pytest.raises(ValueError):
        build_control_channel(Settings(**{**base, "pinned_spki": "not-a-pin"}))


# ---------------------------------------------------------------------------- 제어 채널 (로컬 TLS)


def test_reconnects_without_manual_config_when_pod_port_and_ip_change(certs, caplog) -> None:
    """Pod 재설정 흉내: 옛 Tier 0 가 내려가고 새 IP·포트로 올라와 EC2 등록이 바뀌면, 노트북은 설정을 고치지 않고 다시 붙는다."""
    caplog.set_level(logging.DEBUG)
    cert, _ = certs
    eid = uuid7()
    with FakeEc2() as ec2:
        with TlsTier0(cert, "127.0.0.1", hello=[local_alert(eid, 1, "RAISED")]) as old:
            ec2.current = endpoint_doc("127.0.0.1", old.port, cert.fingerprint)
            channel = channel_for(ec2, {cert.fingerprint}, refresh_s=60)
            app, core, sound = run_app(channel)
            try:
                wait_until(lambda: core.screen_state().control_connected)
                wait_until(lambda: eid in core.active_alerts())
                assert old.auth == [f"Bearer {TOKEN}"] and old.paths == ["/control"]
                app.site_ack(eid)
                wait_until(lambda: len(old.received) == 1)
                with TlsTier0(cert, "127.0.0.2") as new:  # 새 Pod: IP·포트가 바뀐다
                    ec2.current = endpoint_doc("127.0.0.2", new.port, cert.fingerprint, registered_at=1760340099000)
                    old.__exit__(None, None, None)  # 옛 Pod 가 내려간다 → 끊김 → 다시 조회
                    wait_until(lambda: len(new.auth) == 1)
                    wait_until(lambda: core.screen_state().control_connected)
                    assert channel.current_target.url == f"wss://127.0.0.2:{new.port}/control"
                    assert new.auth == [f"Bearer {TOKEN}"]
                    # Tier 0 기록(alarm_stop)을 받지 못한 site_ack 는 새 연결에서 같은 봉투로 다시 보낸다
                    wait_until(lambda: len(new.received) == 1)
                    assert new.received[0] == old.received[0]
            finally:
                app.close()
    assert_no_token_logged(caplog)
    # websockets 클라이언트 DEBUG 헤더 로그는 남되 토큰 줄은 가려진다(-v 실행)
    assert any(r.getMessage() == "> Authorization: ***" for r in caplog.records)


def test_switches_when_endpoint_changes_while_connected(certs) -> None:
    """연결 중 재조회에서 주소·포트가 바뀌면(옛 서버가 아직 살아 있어도) 새 엔드포인트로 옮긴다."""
    cert, _ = certs
    with FakeEc2() as ec2, TlsTier0(cert) as old, TlsTier0(cert) as new:
        ec2.current = endpoint_doc("127.0.0.1", old.port, cert.fingerprint)
        channel = channel_for(ec2, {cert.fingerprint}, refresh_s=0.1)
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: len(old.auth) == 1)
            ec2.current = endpoint_doc("127.0.0.1", new.port, cert.fingerprint, registered_at=1760340099000)
            wait_until(lambda: len(new.auth) == 1)
            wait_until(lambda: core.screen_state().control_connected)
            assert len(old.auth) == 1  # 옛 주소로 다시 붙지 않는다
        finally:
            app.close()


def test_fingerprint_mismatch_from_ec2_never_connects(certs, caplog) -> None:
    """EC2 가 알려 준 지문이 노트북 고정값과 다르면 서버에 접속하지 않는다(조회 값으로 신뢰할 키를 바꾸지 않는다)."""
    caplog.set_level(logging.DEBUG)
    cert, impostor = certs
    with FakeEc2() as ec2, TlsTier0(impostor) as server:
        ec2.current = endpoint_doc("127.0.0.1", server.port, impostor.fingerprint)
        channel = channel_for(ec2, {cert.fingerprint})
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: len(ec2.requests) >= 3)  # 다시 조회는 하지만
            assert server.auth == [] and not core.screen_state().control_connected  # 접속은 안 한다
        finally:
            app.close()
    assert any("PinMismatchError" in r.getMessage() for r in caplog.records)
    assert_no_token_logged(caplog)


def test_impostor_server_key_is_refused_before_token_is_sent(certs, caplog) -> None:
    """EC2 는 고정된 지문을 알려 줬지만 그 주소의 서버가 다른 키를 내밀면(사칭) 기기 토큰을 보내기 전에 끊는다."""
    caplog.set_level(logging.DEBUG)
    cert, impostor = certs
    with FakeEc2() as ec2, RawTlsServer(impostor) as server:
        ec2.current = endpoint_doc("127.0.0.1", server.port, cert.fingerprint)
        channel = channel_for(ec2, {cert.fingerprint})
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: server.handshakes >= 2)  # TLS 핸드셰이크까지는 가고 다시 시도하지만
            assert bytes(server.received) == b""  # HTTP 요청(Authorization 헤더)은 한 바이트도 나가지 않는다
            assert not core.screen_state().control_connected
        finally:
            app.close()
    assert TOKEN.encode() not in bytes(server.received)
    assert any("공개키 지문이 고정값과 다르다" in r.getMessage() for r in caplog.records)


def test_lookup_failures_are_retried_until_registered(certs, caplog) -> None:
    """EC2 장애(5xx)·미등록(404)·인증 오류 뒤에도 다시 조회해, 등록이 생기면 붙는다."""
    caplog.set_level(logging.DEBUG)
    cert, _ = certs
    with FakeEc2() as ec2, TlsTier0(cert) as server:
        ec2.fail_next = [503, 500, 401]
        channel = channel_for(ec2, {cert.fingerprint})
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: len(ec2.requests) >= 5)  # 503, 500, 401, 404(미등록), ...
            assert server.auth == []
            ec2.current = endpoint_doc("127.0.0.1", server.port, cert.fingerprint)
            wait_until(lambda: core.screen_state().control_connected)
            assert server.auth == [f"Bearer {TOKEN}"]
        finally:
            app.close()
    assert_no_token_logged(caplog)


def test_key_rotation_with_two_pins(certs) -> None:
    """키 교체 기간: 옛 키·새 키를 함께 고정하면 EC2 가 새 키로 바꿔도 붙는다(고정 목록 밖의 키는 여전히 거부)."""
    old_cert, new_cert = certs
    with FakeEc2() as ec2, TlsTier0(new_cert) as server:
        ec2.current = endpoint_doc("127.0.0.1", server.port, new_cert.fingerprint)
        channel = channel_for(ec2, {old_cert.fingerprint, new_cert.fingerprint})
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: core.screen_state().control_connected)
            assert channel.current_target.spki_sha256 == new_cert.fingerprint
        finally:
            app.close()


@pytest.mark.parametrize("stable_after_s", [10.0, 0.0])
def test_backoff_resets_only_after_connection_was_stable(certs, stable_after_s: float) -> None:
    """서버가 받자마자 끊으면 재접속 간격이 늘어난다(접속 성공만으로 0.5초로 되돌리지 않는다).
    연결이 stable_after_s 이상 유지됐을 때만 처음 간격으로 돌아간다(0 이면 매번 되돌림 = 비교 기준)."""
    cert, _ = certs
    with FakeEc2() as ec2, TlsTier0(cert, close_immediately=True) as server:
        ec2.current = endpoint_doc("127.0.0.1", server.port, cert.fingerprint)
        channel = channel_for(ec2, {cert.fingerprint}, reconnect_min_s=0.05, reconnect_max_s=0.4,
                              stable_after_s=stable_after_s)
        app, _, _ = run_app(channel)
        try:
            wait_until(lambda: len(server.connected_at) >= 6)
        finally:
            app.close()
    gaps = [b - a for a, b in zip(server.connected_at, server.connected_at[1:])]
    if stable_after_s > 0:
        assert min(gaps[3:5]) >= 0.3, gaps  # 0.05 → 0.1 → 0.2 → 0.4 → 0.4 ...
    else:
        assert max(gaps[3:5]) < 0.3, gaps


def test_unpinned_fingerprint_on_refresh_keeps_connection_and_warns(certs, caplog) -> None:
    """연결 중 재조회에서 EC2 가 고정 밖 지문을 주면 WARNING 으로 남기고 지금(검증된) 연결은 유지한다."""
    caplog.set_level(logging.DEBUG)
    cert, impostor = certs
    with FakeEc2() as ec2, TlsTier0(cert) as server:
        ec2.current = endpoint_doc("127.0.0.1", server.port, cert.fingerprint)
        channel = channel_for(ec2, {cert.fingerprint}, refresh_s=0.05)
        app, core, _ = run_app(channel)
        try:
            wait_until(lambda: core.screen_state().control_connected)
            ec2.current = endpoint_doc("127.0.0.1", server.port, impostor.fingerprint, registered_at=1760340099000)
            wait_until(lambda: any(r.levelno == logging.WARNING and "연결 중 재조회" in r.getMessage()
                                   and "고정값과 다르다" in r.getMessage() for r in caplog.records))
            assert core.screen_state().control_connected and len(server.auth) == 1
        finally:
            app.close()
    assert_no_token_logged(caplog)
