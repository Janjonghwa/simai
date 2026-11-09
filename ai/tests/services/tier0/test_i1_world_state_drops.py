"""health 1.5.0 `ec2_world_state_drops`: Tier 0 → EC2 world_state 송신 버림 누적 (S15P31A101-170).

가짜 연결로 버림 세 경로(밀려 버림, 끊긴 동안 버림, 끊길 때 칸에 남음·보내다 끊김)를 세고, 재연결로 0 이 되지 않는지,
health payload 에 합칠 때 지어내지 않는지 본다. 보고 전 자기 반례 시험(공통 규칙) 반례 E·F·G 와 리뷰 R1 수정의
반례 H·H2·I·J(막힌 send 중 재연결, 감시가 끊지 못한 send 의 대기 상한, 연속 재연결, 늦게 성공한 send)를 여기 둔다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.i1_publisher import (  # noqa: E402
    WORLD_STATE_DROPS_FIELD,
    I1Publisher,
    WorldStateDrops,
    build_i1_link,
    with_world_state_drops,
    world_state_drop_fields,
)
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import FixedClock  # noqa: E402

_spec = importlib.util.spec_from_file_location("_i1_units", Path(__file__).with_name("test_i1_units.py"))
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)

T0 = u.T0


def _world(env, i=0):
    return env.make("world_state", u.world_payload(T0 + i * 100))


def test_counterexample_e_every_drop_path_is_counted_and_survives_reconnect() -> None:
    """반례 E: 첫 health 를 못 만들어 관문이 닫힌 연결에서 world_state 가 밀리고(superseded), 끊길 때 칸에 남은 것과
    끊긴 동안 넣은 것은 disconnected 로 센다. 재연결해도 누적은 0 이 되지 않는다."""
    first, second = u.FakeConn(), u.FakeConn()
    client = u.make_client(u.FakeConnector([first, second]), first_message_type="health")
    env = u.envelope()
    assert client.put_latest(_world(env, 0)) is False  # 연결 전: 끊긴 동안 버림
    assert client.drop_counts("world_state") == (0, 1)
    client.start()
    try:
        assert u.wait_until(lambda: client.connected and client.generation == 1)
        for i in range(1, 4):  # health 가 없어 관문이 닫혀 있다: 세 개 중 둘은 밀리고 하나가 칸에 남는다
            client.put_latest(_world(env, i))
        assert client.drop_counts("world_state") == (2, 1)
        client.drop_connection()
        assert u.wait_until(lambda: client.generation == 2)
        assert client.stats.lost_on_disconnect["world_state"] == 1  # 끊길 때 칸에 남아 있던 것
        assert client.drop_counts("world_state") == (2, 2)  # 재연결로 0 이 되지 않는다
        assert first.sent == []  # 관문이 닫힌 동안 world_state 는 나가지 않았다
    finally:
        client.close()
    assert client.drop_counts("world_state") == (2, 2)
    with pytest.raises(ValueError):
        client.drop_counts("observation_summary")


def test_world_state_lost_while_sending_is_counted_once() -> None:
    """보내다 연결이 끊긴 world_state 는 disconnected 로 한 번만 센다(칸은 이미 비어 끊김 때 다시 세지 않는다)."""
    first, second = u.FakeConn(), u.FakeConn()
    first.fail_sends = True
    client = u.make_client(u.FakeConnector([first, second]))
    env = u.envelope()
    client.start()
    try:
        assert u.wait_until(lambda: client.connected)
        client.put_latest(_world(env))
        assert u.wait_until(lambda: client.generation == 2)
        assert u.wait_until(lambda: client.stats.disconnects >= 1)
    finally:
        client.close()
    assert client.stats.lost_on_disconnect["world_state"] == 1
    assert client.drop_counts("world_state") == (0, 1)
    assert client.stats.send_failures >= 1


def test_counterexample_f_publisher_counts_from_its_start_and_excludes_coalescing() -> None:
    """반례 F: 스케줄러를 만들기 전 누적은 넣지 않고(since_ms 기준), 10 Hz 내려 뽑기(world_coalesced)는 송신 버림이 아니다."""
    mono, client = u.FakeMono(), u.FakeClient()
    client.drops["world_state"] = (5, 7)  # 이 스케줄러 전에 다른 송신 쪽이 센 값
    pub = I1Publisher(client, u.envelope(FixedClock(T0 + 42)), health_payload=u.health_payload, monotonic=mono)
    assert pub.world_state_drops() == WorldStateDrops(superseded=0, disconnected=0, since_ms=T0 + 42)
    for step in range(1, 11):  # 100 Hz 로 넣어 10 Hz 로 내려 뽑힌다
        mono.t = step * 0.01
        pub.offer_world_state(u.world_payload(T0 + step))
        pub.tick()
    assert pub.world_coalesced > 0
    assert pub.world_state_drops() == WorldStateDrops(0, 0, T0 + 42)
    client.drops["world_state"] = (6, 10)
    assert pub.world_state_drops() == WorldStateDrops(1, 3, T0 + 42)


def test_drops_value_object_and_field_helpers() -> None:
    drops = WorldStateDrops(3, 41, T0)
    assert world_state_drop_fields(drops) == {WORLD_STATE_DROPS_FIELD: {"superseded": 3, "disconnected": 41, "since_ms": T0}}
    assert world_state_drop_fields(None) == {}
    for bad in ((-1, 0, T0), (0, -1, T0), (0, 0, -1), (True, 0, T0), (0.5, 0, T0)):
        with pytest.raises(ValueError):
            WorldStateDrops(*bad)


def test_counterexample_g_helper_never_invents_health_or_counts() -> None:
    """반례 G: health 를 모르면(None) 버림 수만으로 health 를 만들지 않고, 버림 수를 모르면 0 을 싣지 않는다.
    이미 같은 필드가 있으면 어느 값을 쓸지 고르지 않고 거절한다. 원본 payload 는 바꾸지 않는다."""
    drops = WorldStateDrops(3, 41, T0)
    assert with_world_state_drops(None, drops) is None
    base = u.health_payload()
    plain = with_world_state_drops(base, None)
    assert plain == base and WORLD_STATE_DROPS_FIELD not in plain
    merged = with_world_state_drops(base, drops)
    assert WORLD_STATE_DROPS_FIELD not in base  # 원본 그대로
    assert merged[WORLD_STATE_DROPS_FIELD] == drops.to_payload()
    msg = u.envelope().make("health", merged)
    assert msg["schema_version"] == "2.3.0" and schema_errors(msg, "health") == []
    with pytest.raises(ValueError, match=WORLD_STATE_DROPS_FIELD):
        with_world_state_drops(merged, drops)


def test_first_health_after_reconnect_carries_offline_drops() -> None:
    """조립 예(-171 이 할 배선): health_payload 에 with_world_state_drops 를 씌우면, 끊긴 동안 버린 world_state 가 연결
    첫 health 에 실리고 다음 연결에서도 줄지 않는다."""
    conns = [u.FakeConn(), u.FakeConn()]
    holder: dict = {}

    def health():
        return with_world_state_drops(u.health_payload(), holder["link"].publisher.world_state_drops())

    mono = u.FakeMono()
    link = build_i1_link(u.FakeConnector(conns), u.envelope(), health_payload=health, client_kwargs=u.FAST,
                         publisher_kwargs={"monotonic": mono})
    holder["link"] = link
    link.publisher.offer_world_state(u.world_payload(T0))
    link.publisher.tick()  # 연결 전: 끊긴 동안 버림
    assert link.publisher.world_state_drops() == WorldStateDrops(0, 1, T0)
    link.client.start()
    try:
        assert u.wait_until(lambda: len(conns[0].sent) >= 1)
        first_health = conns[0].sent[0]
        assert first_health["msg_type"] == "health"
        assert first_health["payload"][WORLD_STATE_DROPS_FIELD] == {"superseded": 0, "disconnected": 1, "since_ms": T0}
        assert schema_errors(first_health, "health") == []
        link.client.drop_connection()
        assert u.wait_until(lambda: len(conns[1].sent) >= 1)
        again = conns[1].sent[0]["payload"][WORLD_STATE_DROPS_FIELD]
        assert again["disconnected"] >= 1 and again["since_ms"] == T0  # 재연결로 0 이 되지 않는다
    finally:
        link.close()


# ---------------------------------------------------------------- 리뷰 R1: 이전 연결의 늦은 send 결과와 첫 health


class DelayedSend(u.FakeConn):
    """world_state send 를 release 까지 붙들고, 풀리면 fail 이 참일 때 실패(끊김), 거짓일 때 성공(끊긴 뒤에도)한다."""

    def __init__(self, fail: bool = True) -> None:
        super().__init__()
        self.fail = fail
        self.enter = threading.Event()
        self.release = threading.Event()

    def send(self, text: str) -> None:
        if json.loads(text)["msg_type"] == "world_state":
            self.enter.set()
            self.release.wait(10)
            if self.fail:
                raise ConnectionError("이전 send 가 늦게 실패")
            self.sent.append(json.loads(text))
            return
        super().send(text)


def _link_with_drops_health(conns, **client_kwargs):
    holder: dict = {}

    def health():
        return with_world_state_drops(u.health_payload(), holder["link"].publisher.world_state_drops())

    kwargs = {**u.FAST, **client_kwargs}
    link = build_i1_link(u.FakeConnector(conns), u.envelope(), health_payload=health, client_kwargs=kwargs)
    holder["link"] = link
    return link


def _first_health_drops(conn) -> dict:
    first = conn.sent[0]
    assert first["msg_type"] == "health" and schema_errors(first, "health") == []
    return first["payload"][WORLD_STATE_DROPS_FIELD]


def _put_world(link, i=0) -> None:
    link.client.put_latest(u.envelope().make("world_state", u.world_payload(T0 + i * 100)))


def test_review_r1_late_send_failure_is_in_first_health_of_next_connection() -> None:
    """리뷰 R1 재현: 이전 연결의 world_state send 가 끊긴 뒤 늦게 실패해도, 새 연결의 첫 health 는 그 성패가 정해진
    뒤에 만들어 버림 1 을 싣는다. 실패는 한 번만 센다."""
    a, b = DelayedSend(), u.FakeConn()
    link = _link_with_drops_health([a, b], send_timeout_s=5.0)
    link.client.start()
    try:
        assert u.wait_until(lambda: len(a.sent) >= 1)  # a 의 첫 health
        _put_world(link)
        assert a.enter.wait(2)
        a.close()  # 수신 쪽이 먼저 끊김을 본다
        time.sleep(0.3)
        assert b.sent == [] and link.client.generation == 1  # 이전 send 의 성패를 기다린다
        a.release.set()
        assert u.wait_until(lambda: len(b.sent) >= 1)
        assert _first_health_drops(b) == {"superseded": 0, "disconnected": 1, "since_ms": T0}
        assert link.client.drop_counts("world_state") == (0, 1)  # 늦은 실패를 두 번 세지 않는다
        assert link.client.stats.inflight_wait_timeouts == 0
    finally:
        a.release.set()
        assert link.close()


def test_counterexample_h_stuck_send_timed_out_is_counted_without_waiting() -> None:
    """반례 H: 막힌 send 중 재연결. 송신 감시가 그 연결을 시간 초과로 끊으면 실패가 확정된 것이라 새 연결은 여유 시간을
    기다리지 않고 바로 버림으로 세어 첫 health 에 싣는다. 막힌 send 가 뒤늦게 실패로 돌아와도 다시 세지 않는다."""
    a, b = DelayedSend(), u.FakeConn()
    link = _link_with_drops_health([a, b], send_timeout_s=0.3)
    link.client.start()
    try:
        assert u.wait_until(lambda: len(a.sent) >= 1)
        _put_world(link)
        assert a.enter.wait(2)
        t0 = time.monotonic()
        a.close()
        assert u.wait_until(lambda: link.client.generation == 2, timeout=3.0)
        assert time.monotonic() - t0 < 0.3 + 0.5  # 감시가 끊는 시각(0.3 s)까지만, 여유(0.5 s)는 기다리지 않는다
        assert link.client.stats.send_timeouts == 1 and link.client.stats.inflight_wait_timeouts == 0
        assert link.client.drop_counts("world_state") == (0, 1)
        a.release.set()  # 송신 스레드는 막힌 send 가 풀려야 b 로 보낸다(-45 동작 그대로)
        assert u.wait_until(lambda: len(b.sent) >= 1)
        assert _first_health_drops(b)["disconnected"] == 1
        assert u.wait_until(lambda: link.client.stats.send_failures >= 1)
        assert link.client.drop_counts("world_state") == (0, 1)  # 늦은 실패를 다시 세지 않는다
    finally:
        a.release.set()
        assert link.close(timeout_s=2.0)


def test_counterexample_h2_unresolved_send_wait_is_bounded() -> None:
    """반례 H2: 송신 감시가 없는(또는 끊지 못한) 막힌 send 중 재연결해도 새 연결은 send_timeout_s + 여유 안에 열린다.
    성패를 모르는 메시지는 미리 버림으로 세지 않고, 실패로 돌아온 뒤 한 번만 센다. close 는 제한 시간 안에 끝난다."""
    a, b = DelayedSend(), u.FakeConn()
    link = _link_with_drops_health([a, b], send_timeout_s=0.3)
    link.client._run_watchdog = lambda: None  # 송신 감시가 끊지 못하는 경우
    link.client.start()
    try:
        assert u.wait_until(lambda: len(a.sent) >= 1)
        _put_world(link)
        assert a.enter.wait(2)
        a.close()
        assert u.wait_until(lambda: link.client.generation == 2, timeout=3.0)  # 0.3 s + 여유 0.5 s 뒤 연다
        assert link.client.stats.inflight_wait_timeouts == 1
        assert link.client.drop_counts("world_state") == (0, 0)
        a.release.set()
        assert u.wait_until(lambda: len(b.sent) >= 1)
        assert _first_health_drops(b)["disconnected"] == 0  # 성패를 모를 때 만든 health: 지어내지 않았다
        assert u.wait_until(lambda: link.client.drop_counts("world_state") == (0, 1))
    finally:
        a.release.set()
        assert link.close(timeout_s=2.0)
    assert link.client.drop_counts("world_state") == (0, 1)


def test_counterexample_i_consecutive_reconnects_accumulate_in_each_first_health() -> None:
    """반례 I: 연속 재연결(a → b → c)마다 이전 연결의 늦은 실패가 다음 연결 첫 health 에 누적으로 실린다(1, 2)."""
    a, b, c = DelayedSend(), DelayedSend(), u.FakeConn()
    link = _link_with_drops_health([a, b, c], send_timeout_s=5.0)
    link.client.start()
    try:
        for i, (old, new, expected) in enumerate(((a, b, 1), (b, c, 2))):
            assert u.wait_until(lambda old=old: len(old.sent) >= 1)
            _put_world(link, i)
            assert old.enter.wait(2)
            old.close()
            time.sleep(0.1)
            old.release.set()
            assert u.wait_until(lambda new=new: len(new.sent) >= 1)
            assert _first_health_drops(new)["disconnected"] == expected
        assert link.client.drop_counts("world_state") == (0, 2)
    finally:
        a.release.set()
        b.release.set()
        assert link.close()


def test_counterexample_j_late_successful_send_is_not_counted() -> None:
    """반례 J: 끊긴 뒤 늦게 성공한 send 는 버림이 아니다. 끊는 순간 미리 버림으로 세지 않는다."""
    a, b = DelayedSend(fail=False), u.FakeConn()
    link = _link_with_drops_health([a, b], send_timeout_s=5.0)
    link.client.start()
    try:
        assert u.wait_until(lambda: len(a.sent) >= 1)
        _put_world(link)
        assert a.enter.wait(2)
        a.close()
        time.sleep(0.1)
        a.release.set()
        assert u.wait_until(lambda: len(b.sent) >= 1)
        assert _first_health_drops(b)["disconnected"] == 0
        assert link.client.drop_counts("world_state") == (0, 0)
        assert link.client.stats.sent.get("world_state") == 1
    finally:
        a.release.set()
        assert link.close()
