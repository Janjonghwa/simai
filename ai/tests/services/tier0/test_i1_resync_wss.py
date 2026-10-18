"""EC2 단절·복구 재동기화 E2E (S15P31A101-81, 명세 3.2·3.10, 15.1 G3, 부록 D-G3-05·12·15).

로컬 wss 서버(EC2 흉내, 자체 서명 TLS)로 본다: EC2 가 끊긴 동안 R1 이 발생하고 판정·현장 경보(노트북 목록)는
계속된다. 현장 확인 버튼(site_ack)도 저널에 남는다. EC2 가 돌아오면 새 연결에서 health → outbox 재전송(acked_offset
이후, 오름차순) → event_state_snapshot 순서로 받고, 두 연결을 합쳐 저널 전이 누락·중복이 없다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
for p in (AI_DIR, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from resync_site import CONFIG, GATEWAY, INSIDE, OUTSIDE, Site  # noqa: E402
from test_i1_tls import TOKEN, Ec2Server, example, health_payload, make_cert, wait_until  # noqa: E402

from services.tier0.endpoint_registration import Backoff  # noqa: E402
from services.tier0.event_snapshot import EventStateSnapshot  # noqa: E402
from services.tier0.i1_client import Envelope, WebSocketConnector  # noqa: E402
from services.tier0.i1_publisher import build_i1_link  # noqa: E402
from services.tier0.site_alert_sync import SiteAlertResync  # noqa: E402
from simai_ai.eventlog import ACKED_NAME, AckedOffsetStore, EventJournal, Outbox  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import FixedClock, SystemClock  # noqa: E402


class DownableEc2(Ec2Server):
    """down 이면 업그레이드 요청을 503 으로 거절한다(EC2 장애). 연결은 Tier 0 가 백오프로 다시 시도한다.

    rejected 는 서버가 거절 응답을 만든 수다. 시험은 클라이언트 쪽 실패 수가 아니라 이 값(서버 쪽 관문)으로 "단절 중
    재접속 시도가 실제로 거절됐다"를 확인한다.
    """

    down = False
    rejected = 0

    def _check(self, connection, request):
        if self.down:
            with self._lock:
                self.requests += 1
                self.rejected += 1
            return connection.respond(503, "ec2 down\n")
        return super()._check(connection, request)


@pytest.fixture
def ec2(tmp_path):
    cert, key = make_cert(tmp_path)
    server = DownableEc2(cert, key)
    yield server, cert
    server.stop()


def kinds(messages: list[dict]) -> list[str]:
    return [m["msg_type"] for m in messages]


def test_ec2_outage_keeps_judging_and_site_alerts_then_resyncs_without_gap(ec2, tmp_path) -> None:
    server, cert = ec2
    clock = FixedClock(1760342400000)  # 판정·저널 시계(주입)
    wire_clock = SystemClock()  # 봉투 sent_at
    journal = EventJournal.open(tmp_path / "journal", clock=clock)
    store = AckedOffsetStore(tmp_path / ACKED_NAME, gateway_id=GATEWAY)
    site = Site(journal, clock)
    laptop: list[dict] = []  # 노트북 제어 채널로 간 활성 현장 경보 목록(EC2 와 별개 경로)
    resync = SiteAlertResync(active_states=site.tracker.active, send=laptop.append, clock=clock, interval_ms=1000)
    resync.control_connected()
    snap = EventStateSnapshot(journal, gateway_id=GATEWAY, clock=wire_clock, config_version=lambda: CONFIG,
                              start_offset=0, current_state=site.tracker.get, risk_observed_at=site.risk_observed_at)
    link = build_i1_link(
        # 제한 시간 근거: Windows asyncio 시험 서버가 503 거절 응답을 가끔 끝내지 않아 클라이언트가 핸드셰이크를
        # open_timeout_s 까지 기다린다(리뷰 실측: 제품 기본 10s 로 두면 3회 중 2회가 5s 대기를 넘김). 재접속 한 번을
        # 1s 로 묶으면 아래 5s·8s 대기 안에 거절이 여러 번 끝난다. 거절 여부는 서버 관문(rejected)으로 본다.
        # 기본 10s 를 포함한 지연 시나리오는 이 시험의 범위가 아니다(MR 남은 일).
        WebSocketConnector(server.url, TOKEN, ca_file=str(cert), open_timeout_s=1.0),
        Envelope(GATEWAY, wire_clock),
        health_payload=health_payload,
        health_fields=[resync.fields],
        make_outbox=lambda t: snap.bind(Outbox(journal, store, gateway_id=GATEWAY, transport=t, clock=wire_clock,
                                               batch_size=4)),
        client_kwargs={"backoff": Backoff(initial_s=0.05, factor=2.0, max_s=0.2), "stable_after_s": 0.5},
    )
    snap.attach(link.client, link.publisher.envelope)  # -171 조립: snapshot_payload 는 주지 않는다
    try:
        # 1) 연결 전 R1 발생(w-a). 첫 연결: health → 전이 0 → 스냅샷. EC2 가 offset 0 까지 확인한다.
        site.step({"w-a": INSIDE})
        a = site.event_of("w-a")
        link.start()
        assert wait_until(lambda: "event_state_snapshot" in kinds(server.messages(0)))
        server.send({**example("journal_ack"), "payload": {"gateway_id": GATEWAY, "acked_offset": 0}})
        assert wait_until(lambda: link.outbox.acked_offset == 0)

        # 2) EC2 장애: 끊고 재접속을 거절한다
        server.down = True
        server.drop(0)
        assert wait_until(lambda: not link.client.connected)
        rejected = server.rejected

        # 3) 끊긴 동안: R1 새 발생(w-b, CRITICAL → 현장 경보), w-a 현장 확인, w-c 는 발생 후 해소, w-a 관측 상실
        site.step({"w-a": INSIDE, "w-b": INSIDE, "w-c": INSIDE})
        b, c = site.event_of("w-b"), site.event_of("w-c")
        assert {s.event_id for s in site.local_alerts} >= {a, b, c}  # 판정 직후 현장 경보(EC2 를 거치지 않음)
        site.site_ack(a)
        for _ in range(5):
            site.step({"w-a": INSIDE, "w-b": INSIDE, "w-c": OUTSIDE})
        site.step({"w-b": INSIDE})  # w-a 가 시야에서 사라짐: 자동 해소 없이 UNVERIFIABLE
        clock.advance(1000)
        resync.tick()
        alerts = {x["event_id"]: x for x in laptop[-1]["active_site_alerts"]}
        assert set(alerts) == {a, b}  # 현장 경보는 EC2 와 관계없이 유지·해소된다
        assert alerts[a]["acknowledged"] is True and alerts[b]["acknowledged"] is False
        assert wait_until(lambda: server.rejected >= rejected + 2)  # 서버 관문: 재접속을 실제로 거절했다
        assert not link.client.connected and len(server.connections) == 1
        last = journal.last_offset
        assert last == 5  # b·c 발생, a 확인, c 해소, a 관측 상실

        # 4) EC2 복구: 새 연결에서 health → 재전송(1..last) → 스냅샷
        server.down = False
        assert wait_until(lambda: len(server.connections) == 2, timeout=8)
        assert wait_until(lambda: "event_state_snapshot" in kinds(server.messages(1)), timeout=8)
        time.sleep(0.3)
    finally:
        link.close()
        journal.close()

    first, second = server.messages(0), server.messages(1)
    for m in first + second:
        assert schema_errors(m, m["msg_type"]) == [], (m["msg_type"], schema_errors(m, m["msg_type"]))
    k2 = kinds(second)
    assert k2[0] == "health" and k2.count("event_state_snapshot") == 1
    snap_at = k2.index("event_state_snapshot")
    resent = [m["payload"] for m in second[:snap_at] if m["msg_type"] == "risk_event_transition"]
    assert [p["journal_offset"] for p in resent] == list(range(1, last + 1))  # acked_offset 이후, 오름차순
    assert not any(p["resent"] for p in resent)  # 끊긴 동안 생긴 기록은 이번이 첫 송신이다(outbox resent 규칙)
    assert not [m for m in second[snap_at:] if m["msg_type"] == "risk_event_transition"]
    # 두 연결을 합쳐 저널 전이 누락·중복 0 (EC2 는 (event_id, event_sequence)로 걸러 낸다)
    delivered = [m["payload"] for m in first + second if m["msg_type"] == "risk_event_transition"]
    assert sorted({p["journal_offset"] for p in delivered}) == list(range(last + 1))
    keys = [(p["event_id"], p["event_sequence"]) for p in delivered]
    assert len(set(keys)) == last + 1 and len(keys) == last + 1
    # 확인 기록(site_ack)이 EC2 에 반영된다(D-G3-15)
    assert any(p["transition"] == "ACKNOWLEDGED" and p["ack"]["channel"] == "SITE_BUTTON" for p in resent)

    payload = second[snap_at]["payload"]
    assert payload["journal_offset"] == last
    events = {e["event_id"]: e for e in payload["active_events"]}
    assert set(events) == {a, b}  # 끊긴 동안 해소된 w-c 는 없다(감사 기록으로만)
    final = {}
    for p in delivered:
        final[p["event_id"]] = p["event_sequence"]
    for event_id, e in events.items():
        assert e["event_sequence"] == final[event_id] == site.tracker.get(event_id).event_sequence
    assert events[a]["acknowledged"] is True and events[a]["verification"] == "UNVERIFIABLE"
    # 위험 근거 시각: 관측을 잃기 전 마지막으로 구역 안을 VERIFIED 로 본 촬영 시각(주입 risk_observed_at)
    assert events[a]["last_valid_observation_at"] == site.risk_seen[a][1]
    assert events[b]["acknowledged"] is False and events[b]["verification"] == "VERIFIED"
    assert events[b]["last_valid_observation_at"] == site.risk_seen[b][1]
    assert site.risk_seen[a][1] < site.risk_seen[b][1]
    # 첫 연결의 스냅샷은 그때 상태(w-a 만, offset 0)
    [first_snap] = [m["payload"] for m in first if m["msg_type"] == "event_state_snapshot"]
    assert first_snap["journal_offset"] == 0 and [e["event_id"] for e in first_snap["active_events"]] == [a]
