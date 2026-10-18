"""Tier 0 쪽 노트북 연결 판정 (명세 3.2 이중 감시, 6.5, S15P31A101-171): 하트비트 3회 연속 누락이면 끊김."""

import pytest

from simai_ai.tracking.gateway_link import GatewayLinkMonitor


def test_connected_only_after_first_heartbeat():
    m = GatewayLinkMonitor()
    assert not m.ok(0.0)  # 연결 전
    gen = m.control_connected(0.0)
    assert not m.ok(10.0)  # 연결됐지만 하트비트를 받기 전: 모르면 끊김
    assert m.heartbeat(20.0, gen)
    assert m.ok(20.0)
    assert m.status(20.0).link_state == "CONNECTED"


def test_three_missed_heartbeats_boundary():
    m = GatewayLinkMonitor(period_ms=1000, missed_limit=3)
    gen = m.control_connected(0.0)
    m.heartbeat(1000.0, gen)
    assert m.ok(4000.0)  # 정확히 3초: 세 번째 하트비트가 올 차례까지는 기다린다
    assert not m.ok(4001.0)  # 넘으면 끊김
    m.heartbeat(4100.0, gen)
    assert m.ok(4100.0)  # 다시 오면 연결됨


def test_disconnect_drops_heartbeats_and_stale_generation_is_ignored():
    m = GatewayLinkMonitor()
    old = m.control_connected(0.0)
    m.heartbeat(10.0, old)
    new = m.control_connected(20.0)  # 재접속: 이전 연결의 하트비트는 버린다
    assert not m.ok(30.0)
    assert not m.heartbeat(40.0, old)  # 이전 연결 세대의 늦은 하트비트는 세지 않는다
    assert not m.ok(40.0)
    m.control_disconnected(old)  # 이전 연결의 늦은 끊김 통지는 무시
    assert m.heartbeat(50.0, new) and m.ok(50.0)
    m.control_disconnected(new)
    assert not m.ok(60.0)
    assert not m.heartbeat(70.0, new)


def test_monotonic_clock_must_not_go_back():
    m = GatewayLinkMonitor()
    m.control_connected(100.0)
    with pytest.raises(ValueError):
        m.ok(50.0)
