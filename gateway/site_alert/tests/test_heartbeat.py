"""노트북 → EC2 하트비트(HTTPS) 와 laptop_heartbeat 계약: 스키마 통과, HTTPS·인증서 검증, 기기 토큰 노출 없음,
EC2 가 느려도 쌓이지 않고 현장 경보 경로를 막지 않음. EC2 는 로컬 HTTPS 시험 서버다(자체 서명 인증서는 tmp_path 에만)."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time

import pytest
from link_helpers import Harness, healthy_harness
from tls_helpers import HttpsEc2, make_cert, wait_until

from simai_site_alert.app import SiteAlertApp
from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.clock import SystemClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.heartbeat import HEARTBEAT_PATH, Ec2HeartbeatClient, Ec2HeartbeatError, Ec2HeartbeatSender
from simai_site_alert.link_status import CONTROL_HALTED, NORMAL, REMOTE_FAULT, Reason, SiteLinkMonitor
from simai_site_alert.messages import build_laptop_heartbeat
from simai_site_alert.sound import FakeSoundBackend
from simai_site_alert.transport import InMemoryControlChannel

TOKEN = "dev-tok-S3cr3t+/=%41"  # 퍼센트 인코딩 꼴까지 확인하려고 특수 문자를 넣는다


def _token_forms() -> set[str]:
    import urllib.parse

    return {TOKEN, urllib.parse.quote(TOKEN, safe=""), urllib.parse.quote_plus(TOKEN)}


def assert_no_token(*texts: str) -> None:
    for text in texts:
        for form in _token_forms():
            assert form not in text, "기기 토큰이 드러났다"


@pytest.fixture
def cert(tmp_path):
    return make_cert(tmp_path, "ec2", loopback_san=True)


def client_for(ec2: HttpsEc2, cert, **kw) -> Ec2HeartbeatClient:
    return Ec2HeartbeatClient(ec2.base_url, device_token=TOKEN, ssl_context=ssl.create_default_context(cafile=str(cert.cert_file)), **kw)


# ---- 계약 --------------------------------------------------------------------------------------------------------


def test_heartbeats_pass_contract_in_every_state(contract_validator) -> None:
    """정상·관제 중단·원격 장애·시작 직후 모두 laptop_heartbeat 스키마를 통과한다(Tier 0 용은 ec2 포함, EC2 용은 뺌)."""
    h = Harness()
    try:
        seqs = []

        def check(expect_display: str) -> None:
            for include_ec2 in (True, False):
                env = h.app.build_heartbeat(include_ec2=include_ec2)
                contract_validator("laptop_heartbeat", env)
                assert ("ec2" in env["payload"]) is include_ec2
                assert env["payload"]["site_display"] == expect_display
                assert env["source_id"] == "laptop-01"
                seqs.append(env["payload"]["heartbeat_seq"])

        check(CONTROL_HALTED)  # 시작 직후: 아무것도 모른다
        h.connect()
        h.seconds_until(NORMAL)
        eid = uuid7()
        h.channel.deliver(local_alert(eid, 1, "RAISED", now_ms=h.clock.now_ms()))
        check(NORMAL)
        env = h.app.build_heartbeat(include_ec2=True)
        assert env["payload"]["alarm"] == {"sounding_event_ids": [eid], "displayed_event_ids": [eid]}
        assert [c["camera_id"] for c in env["payload"]["cameras"]] == ["cam1", "cam2", "cam3", "cam4"]
        h.ec2_on = False
        h.seconds_until(REMOTE_FAULT)
        check(REMOTE_FAULT)
        h.health_on = False
        h.seconds_until(CONTROL_HALTED)
        check(CONTROL_HALTED)
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # 단조 증가
    finally:
        h.close()


def test_contract_examples_match_builder(contract_validator) -> None:
    from conftest import load_example

    for name in ("laptop_heartbeat", "laptop_heartbeat.tier0_down"):
        ex = load_example(name)
        built = build_laptop_heartbeat(laptop_id=ex["source_id"], now_ms=ex["sent_at"], payload=ex["payload"])
        contract_validator("laptop_heartbeat", built)
        assert built["schema_version"] == ex["schema_version"] and built["msg_type"] == ex["msg_type"]


def test_tier0_heartbeat_goes_over_control_channel_every_second() -> None:
    """노트북 → Tier 0 하트비트: 연결돼 있으면 1초마다 제어 채널로(ec2 포함), 끊겨 있으면 보내지 않는다."""
    h = healthy_harness()
    try:
        before = len([m for m in h.channel.sent if m["msg_type"] == "laptop_heartbeat"])
        h.run(5)
        hbs = [m for m in h.channel.sent if m["msg_type"] == "laptop_heartbeat"][before:]
        assert 4 <= len(hbs) <= 6
        assert all("ec2" in m["payload"] for m in hbs)
    finally:
        h.close()


# ---- HTTPS 송신 --------------------------------------------------------------------------------------------------


def test_post_reaches_ec2_with_token_in_header_only(cert, contract_validator) -> None:
    with HttpsEc2(cert) as ec2:
        client = client_for(ec2, cert)
        env = build_laptop_heartbeat(laptop_id="laptop-01", now_ms=1_760_342_401_000, payload={
            "heartbeat_seq": 0, "tier0": {"control": "DISCONNECTED", "last_message_at": None, "video_cameras": []},
            "site_display": "CONTROL_HALTED"})
        assert client.post(env) == 200
        [req] = ec2.requests
        assert req["path"] == HEARTBEAT_PATH
        assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert req["headers"]["Content-Type"] == "application/json"
        body = json.loads(req["body"])
        contract_validator("laptop_heartbeat", body)
        assert_no_token(req["path"], req["body"].decode("utf-8"))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8443",  # 평문 http 는 루프백도 안 된다
        "ftp://ec2.example",
        "https://user:pw@ec2.example",
        "https://ec2.example/?token=x",
        "https://ec2.example/#frag",
        f"https://ec2.example/{TOKEN}",
        "https://ec2.example/dev-tok-S3cr3t%2B%2F%3D%2541",
        "https:///nohost",
    ],
)
def test_client_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError) as e:
        Ec2HeartbeatClient(url, device_token=TOKEN)
    assert_no_token(str(e.value))


def test_client_rejects_unverified_context_and_empty_token() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        Ec2HeartbeatClient("https://ec2.example", device_token=TOKEN, ssl_context=ctx)
    with pytest.raises(ValueError):
        Ec2HeartbeatClient("https://ec2.example", device_token=" ")


def test_untrusted_certificate_fails_and_sends_nothing(cert) -> None:
    """인증서를 검증한다: 노트북이 믿지 않는 자체 서명 인증서 서버에는 토큰을 실은 요청이 닿지 않는다."""
    with HttpsEc2(cert) as ec2:
        client = Ec2HeartbeatClient(ec2.base_url, device_token=TOKEN)  # 기본 신뢰 저장소: 시험 인증서를 모른다
        with pytest.raises(Ec2HeartbeatError) as e:
            client.post({"msg_type": "laptop_heartbeat"})
        assert ec2.requests == []
        assert_no_token(str(e.value), repr(e.value))
        assert e.value.__cause__ is None and e.value.__suppress_context__  # 원인 예외(받은 값이 섞일 수 있음)를 잇지 않는다


def test_hostname_mismatch_fails(tmp_path) -> None:
    other = make_cert(tmp_path, "nosan")  # 127.0.0.1 이름이 없는 인증서
    with HttpsEc2(other) as ec2:
        client = Ec2HeartbeatClient(ec2.base_url, device_token=TOKEN,
                                    ssl_context=ssl.create_default_context(cafile=str(other.cert_file)))
        with pytest.raises(Ec2HeartbeatError):
            client.post({"msg_type": "laptop_heartbeat"})
        assert ec2.requests == []


def test_redirect_is_not_followed(cert) -> None:
    """리다이렉트를 따라가면 토큰이 다른 곳으로 실린다. 따라가지 않고 실패(2xx 아님)로 본다."""
    with HttpsEc2(cert) as target, HttpsEc2(cert, mode="redirect:PLACEHOLDER") as ec2:
        ec2.mode = f"redirect:{target.base_url}/steal"
        client = client_for(ec2, cert)
        assert client.post({"msg_type": "laptop_heartbeat"}) == 302
        assert target.requests == []


def test_token_never_in_logs_errors_or_repr(cert, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    with HttpsEc2(cert, mode="status:401") as ec2:
        client = client_for(ec2, cert)
        texts.append(repr(client))
        results: list[bool] = []
        sender = Ec2HeartbeatSender(client, lambda: {"msg_type": "laptop_heartbeat"},
                                    lambda ok, mono, ms: results.append(ok))
        assert sender.send_once() is False  # 401: 토큰 거부
        ec2.mode = "status:500"
        assert sender.send_once() is False
        ec2.mode = "ok"
        assert sender.send_once() is True
        port = ec2.port
    # 서버가 사라진 뒤(연결 거부)와 신뢰하지 않는 인증서
    dead = Ec2HeartbeatClient(f"https://127.0.0.1:{port}", device_token=TOKEN, timeout_s=0.5)
    texts.append(repr(dead))
    for c in (dead,):
        try:
            c.post({"msg_type": "laptop_heartbeat"})
        except Ec2HeartbeatError as e:
            texts += [str(e), repr(e)]
    sender = Ec2HeartbeatSender(dead, lambda: {"msg_type": "laptop_heartbeat"}, lambda *a: None)
    assert sender.send_once() is False
    texts += [r.getMessage() for r in caplog.records]
    assert any("기기 토큰을 거부" in t for t in texts)
    assert_no_token(*texts)


def test_settings_repr_hides_token(tmp_path) -> None:
    from simai_site_alert.config import load_settings

    env = tmp_path / ".env"
    env.write_text(f"SIMAI_DEVICE_TOKEN={TOKEN}\nSIMAI_EC2_BASE_URL=https://ec2.example\n", encoding="utf-8")
    assert_no_token(repr(load_settings(env)), str(load_settings(env)))


# ---- EC2 가 느리거나 막혔을 때 ----------------------------------------------------------------------------------------


def test_slow_ec2_does_not_queue_and_does_not_block_site_alarm(cert) -> None:
    """EC2 가 응답마다 4초 걸려도: 하트비트는 쌓이지 않고(한 번에 하나), 3초 넘게 2xx 가 없으니 원격 장애로 바뀌며(5초 안),
    그동안 받은 local_alert 는 바로 울린다. 송신이 막혀 있어도 현장 경보 경로(수신 → 코어 → 소리)를 기다리게 하지 않는다."""
    clock = SystemClock()
    sound = FakeSoundBackend()
    core = SiteAlertCore(sound, laptop_id="laptop-01", clock=clock)
    channel = InMemoryControlChannel(monotonic=clock.monotonic)
    monitor = SiteLinkMonitor()
    with HttpsEc2(cert) as ec2:
        app = SiteAlertApp(core, channel, tick_s=0.25, monitor=monitor, ec2=client_for(ec2, cert, timeout_s=10),
                           clock=clock)
        app.start(test_alert_s=None)
        try:
            wait_until(lambda: monitor.evaluate(clock.monotonic()).ec2_link == "CONNECTED", 5)
            ec2.mode = "delay:4"
            slow_from = clock.monotonic()
            n_before = len(ec2.requests)
            wait_until(lambda: core.screen_state().site.ec2_reasons != (), 6)
            assert clock.monotonic() - slow_from <= 5.0
            # 현장 경보는 송신이 막혀 있는 동안에도 바로 울린다
            eid = uuid7()
            t0 = time.perf_counter()
            channel.connected = True
            channel.deliver(local_alert(eid, 1, "RAISED"))
            assert time.perf_counter() - t0 < 0.2
            assert sound.playing.name == "CRITICAL"
            time.sleep(4.5)
            # 4초짜리 응답 동안 새 요청을 쌓지 않는다: 8~9초 동안 요청은 3개 이하
            assert len(ec2.requests) - n_before <= 3
        finally:
            app.close()


def test_hung_ec2_thread_does_not_stop_status_updates(cert) -> None:
    """EC2 가 아예 답하지 않아(송신 스레드가 막혀) 결과를 못 알려도, 원격 장애 판정은 시간으로 하므로 5초 안에 바뀐다."""
    gate = threading.Event()

    class HangingClient:
        def post(self, envelope):  # noqa: ARG002
            gate.wait(10)
            return 200

    clock = SystemClock()
    core = SiteAlertCore(FakeSoundBackend(), laptop_id="laptop-01", clock=clock)
    monitor = SiteLinkMonitor()
    channel = InMemoryControlChannel(monotonic=clock.monotonic)
    app = SiteAlertApp(core, channel, tick_s=0.25, monitor=monitor, clock=clock)
    sender = Ec2HeartbeatSender(HangingClient(), lambda: app.build_heartbeat(include_ec2=False),
                                lambda ok, mono, ms: monitor.on_ec2_result(ok, mono, ms),
                                monotonic=clock.monotonic, now_ms=clock.now_ms)
    monitor.on_ec2_result(True, clock.monotonic(), clock.now_ms())  # 직전까지 정상
    app.start(test_alert_s=None)
    sender.start()
    try:
        start = clock.monotonic()
        wait_until(lambda: core.screen_state().site.ec2_reasons == (Reason.EC2_UNREACHABLE,), 5)
        assert clock.monotonic() - start <= 3.6
        assert core.screen_state().site.ec2_link == "DISCONNECTED"
    finally:
        gate.set()
        sender.close()
        app.close()
