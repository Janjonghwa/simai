"""Config Sync: 판정 tick 경계 원자적 전환, config_ack, 상태 전이, 덮어쓰기 없음 (명세 3.2, 6.2, 부록 D-G3-06)."""

import random
import threading
import time

import pytest

from simai_ai.config import ConfigSync, ConfigSyncState, RejectReason
from simai_ai.providers.contract import schema_violations
from simai_ai.risk import (
    EventIdFactory,
    LifecycleTracker,
    Origin,
    WorldState,
)

S = ConfigSyncState


def make_sync(kit, **kwargs):
    return ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01", **kwargs)


def assert_ack_valid(ack):
    assert schema_violations(dict(ack), "config_ack") == []


def connected_sync_with(kit, version="cfg-12"):
    sync = make_sync(kit)
    sync.on_connected()
    assert sync.receive(kit.publish(version)).accepted
    kit.clock.advance(10)
    sync.begin_tick()
    sync.take_acks()
    return sync


def assert_consistent(active):
    """한 tick 의 묶음은 구역·보정·작업면·config_version 이 모두 한 묶음에서 온다(반쪽 적용 없음)."""
    v = active.config_version
    assert active.bundle.zones_version == f"zones-{v}"
    assert {z.zone_version for z in active.zones} == {f"zone-{v}"}
    assert active.calibration_version == f"cal-{v}"
    assert active.bundle.geometry_version == f"geom-{v}"
    assert active.bundle.notice.config_version == v


# --- 기본 흐름 ----------------------------------------------------------------------


def test_starts_unverified_without_bundle(kit):
    sync = make_sync(kit)
    assert sync.state is S.UNVERIFIED
    assert sync.begin_tick() is None
    assert sync.health_fields() == {"config_sync": "UNVERIFIED"}


def test_applied_only_at_tick_boundary_with_ack(kit):
    sync = make_sync(kit)
    sync.on_connected()
    result = sync.receive(kit.publish("cfg-13"))
    assert result.accepted and result.ack is None  # APPLIED 는 전환 때 보낸다
    assert sync.state is S.PENDING
    assert sync.active is None and sync.take_acks() == []

    kit.clock.advance(37)
    active = sync.begin_tick()
    assert active.config_version == "cfg-13"
    assert active.applied_at == kit.clock.now_ms()
    assert sync.state is S.CURRENT
    [ack] = sync.take_acks()
    assert_ack_valid(ack)
    assert ack["payload"] == {
        "config_version": "cfg-13",
        "bundle_hash": active.bundle.bundle_hash,
        "result": "APPLIED",
        "applied_at": active.applied_at,
        "active_config_version": "cfg-13",
    }
    assert ack["source_id"] == "tier0-01"
    assert sync.health_fields() == {"config_sync": "CURRENT", "config_version": "cfg-13"}
    # 연결 직후 첫 알림은 활성 묶음과 다르다는 확인(STALE)을 거쳐 전환한다
    assert [(t.before, t.after) for t in sync.transitions()] == [
        (S.UNVERIFIED, S.STALE),
        (S.STALE, S.PENDING),
        (S.PENDING, S.CURRENT),
    ]


def test_switch_requested_mid_tick_applies_at_next_boundary(kit):
    sync = connected_sync_with(kit, "cfg-12")
    tick = sync.begin_tick()
    assert tick.config_version == "cfg-12"
    sync.receive(kit.publish("cfg-13"))  # 판정 중 전환 요청
    assert tick.config_version == "cfg-12" and sync.active.config_version == "cfg-12"
    assert_consistent(tick)
    kit.clock.advance(100)
    nxt = sync.begin_tick()
    assert nxt.config_version == "cfg-13"
    assert_consistent(nxt)


@pytest.mark.parametrize("tamper", ["manifest", "zones"])
def test_rejected_bundle_keeps_previous_and_reports(kit, tamper):
    sync = connected_sync_with(kit, "cfg-12")
    before = sync.active
    result = sync.receive(kit.publish("cfg-13", tamper=tamper))
    assert not result.accepted and result.reason is RejectReason.HASH_MISMATCH
    assert_ack_valid(result.ack)
    assert result.ack["payload"]["result"] == "REJECTED"
    assert result.ack["payload"]["active_config_version"] == "cfg-12"
    assert sync.take_acks() == [result.ack]
    assert sync.begin_tick() is before
    assert sync.state is S.STALE  # EC2 최신(cfg-13)과 활성(cfg-12)이 다르다


def test_reference_broken_rejected(kit):
    sync = connected_sync_with(kit, "cfg-12")
    result = sync.receive(kit.publish("cfg-13", rule_versions=(("R9", 1),)))
    assert result.reason is RejectReason.REFERENCE_INVALID
    assert sync.begin_tick().config_version == "cfg-12"


def test_schema_invalid_without_header_has_no_ack(kit):
    sync = connected_sync_with(kit, "cfg-12")
    result = sync.receive({"msg_type": "config_bundle", "payload": {}})
    assert result.reason is RejectReason.SCHEMA_INVALID and result.ack is None
    message = kit.publish("cfg-13")
    del message["payload"]["components"]
    result = sync.receive(message)
    assert result.reason is RejectReason.SCHEMA_INVALID
    assert_ack_valid(result.ack)


# --- 덮어쓰기 없음 ------------------------------------------------------------------


def test_same_version_different_content_rejected(kit):
    sync = connected_sync_with(kit, "cfg-12")
    original = sync.active.bundle
    impostor = kit.publish("cfg-12", zones=[])  # 같은 버전, 다른 내용
    result = sync.receive(impostor)
    assert result.reason is RejectReason.REFERENCE_INVALID
    assert sync.active.bundle is original
    assert sync.state is S.STALE


def test_older_bundle_rejected_and_history_kept(kit):
    sync = connected_sync_with(kit, "cfg-12")
    kit.clock.advance(1000)
    sync.receive(kit.publish("cfg-13"))
    sync.begin_tick()
    first, second = sync.versions()
    assert (first.config_version, second.config_version) == ("cfg-12", "cfg-13")
    late = kit.publish("cfg-11", issued_at=first.issued_at - 1)
    assert sync.receive(late).reason is RejectReason.REFERENCE_INVALID
    again = kit.publish("cfg-12b", issued_at=second.issued_at)
    assert sync.receive(again).reason is RejectReason.REFERENCE_INVALID
    # 이전 묶음은 그대로 남는다(같은 객체, 같은 내용)
    assert sync.versions() == (first, second)
    assert [r.config_version for r in sync.applied()] == ["cfg-12", "cfg-13"]
    assert sync.version_at(sync.applied()[1].applied_at - 1) == "cfg-12"
    assert sync.version_at(sync.applied()[1].applied_at) == "cfg-13"
    assert sync.version_at(sync.applied()[0].applied_at - 1) is None


def test_newer_staged_replaces_unapplied_staged(kit):
    sync = connected_sync_with(kit, "cfg-12")
    kit.clock.advance(10)
    sync.receive(kit.publish("cfg-13"))
    kit.clock.advance(10)
    sync.receive(kit.publish("cfg-14"))
    kit.clock.advance(10)
    assert sync.begin_tick().config_version == "cfg-14"
    acks = sync.take_acks()
    assert [a["payload"]["config_version"] for a in acks] == ["cfg-14"]  # cfg-13 은 판정에 쓰이지 않았다
    assert [b.config_version for b in sync.versions()] == ["cfg-12", "cfg-13", "cfg-14"]


# --- 연결 단절·복구 -----------------------------------------------------------------


def test_offline_keeps_bundle_and_unverified(kit):
    sync = connected_sync_with(kit, "cfg-12")
    active = sync.active
    sync.on_disconnected()
    assert sync.state is S.UNVERIFIED
    for _ in range(50):
        kit.clock.advance(100)
        assert sync.begin_tick() is active  # 판정은 마지막 검증 묶음으로 계속
    assert sync.health_fields() == {"config_sync": "UNVERIFIED", "config_version": "cfg-12"}


def test_reconnect_same_bundle_becomes_current_with_original_applied_at(kit):
    sync = connected_sync_with(kit, "cfg-12")
    applied_at = sync.active.applied_at
    sync.on_disconnected()
    kit.clock.advance(5000)
    sync.on_connected()
    assert sync.state is S.UNVERIFIED  # 알림을 받기 전에는 확인할 수 없다
    result = sync.receive(_resend(kit, sync.active.bundle))
    assert result.accepted
    assert result.ack["payload"]["applied_at"] == applied_at
    assert_ack_valid(result.ack)
    assert sync.state is S.CURRENT


def _resend(kit, bundle):
    from simai_ai.config.bundle import thaw_json

    return thaw_json(bundle.notice.message)


def test_reconnect_with_newer_bundle_goes_stale_then_switches(kit):
    sync = connected_sync_with(kit, "cfg-12")
    sync.on_disconnected()
    kit.clock.advance(60_000)
    newer = kit.publish("cfg-13")
    sync.on_connected()
    sync.receive(newer)
    assert sync.state is S.PENDING
    kit.clock.advance(50)
    assert sync.begin_tick().config_version == "cfg-13"
    assert sync.state is S.CURRENT
    states = [(t.before, t.after) for t in sync.transitions()]
    assert states[-4:] == [
        (S.CURRENT, S.UNVERIFIED),
        (S.UNVERIFIED, S.STALE),
        (S.STALE, S.PENDING),
        (S.PENDING, S.CURRENT),
    ]


def test_reconnect_with_bad_newer_bundle_stays_stale(kit):
    sync = connected_sync_with(kit, "cfg-12")
    sync.on_disconnected()
    sync.on_connected()
    sync.receive(kit.publish("cfg-13", tamper="rules"))
    assert sync.state is S.STALE
    assert sync.begin_tick().config_version == "cfg-12"


def test_bundle_verified_while_disconnecting_still_switches_atomically(kit):
    """검증 도중 연결이 끊겨도 검증을 마친 묶음은 다음 경계에서 전환하고 ACK 는 남겨 둔다."""
    sync = connected_sync_with(kit, "cfg-12")
    original_fetch = kit.fetcher.fetch

    def fetch_then_drop(uri):
        if uri.endswith("manifest.json"):
            sync.on_disconnected()
        return original_fetch(uri)

    kit.fetcher.fetch = fetch_then_drop
    assert sync.receive(kit.publish("cfg-13")).accepted
    assert sync.state is S.UNVERIFIED
    kit.clock.advance(10)
    assert sync.begin_tick().config_version == "cfg-13"
    assert sync.state is S.UNVERIFIED
    [ack] = sync.take_acks()
    assert ack["payload"]["result"] == "APPLIED"


# --- 이벤트의 config_version --------------------------------------------------------


def _world(t, inside=True):
    return WorldState.from_payload(
        {
            "t_latest_global": t,
            "calibration_version": "cal-7",
            "scene_version": "scene-3",
            "tracks": [
                {
                    "world_id": "w-1",
                    "t_last_valid": t,
                    "observation_state": "OBSERVED",
                    "track_lifecycle": "CONFIRMED",
                    "identity_confidence": "HIGH",
                    "identity_revision": 1,
                    "position_xy": [3.4, 5.0] if inside else [8.0, 9.0],
                    "position_xy_sigma": [0.05, 0.05],
                    "pelvis_xyz": [3.4, 5.0, 0.9],
                    "ground_anchor_xyz": [3.4, 5.0, 0.0],
                    "support_surface_id": "floor",
                    "support_surface_validity": "VALID",
                    "contact_validity": "VALID",
                    "z_validity": "VALID",
                    "head_z": 1.7,
                    "cameras": ["cam1", "cam2"],
                }
            ],
        }
    )


def test_events_carry_config_version_of_their_tick(kit):
    """lifecycle 을 고치지 않고 tick 의 ActiveConfig 로 이벤트에 config_version 이 실린다."""
    sync = connected_sync_with(kit, "cfg-12")
    tracker = LifecycleTracker(
        origin=Origin.LIVE, gateway_id="tier0-01", clock=kit.clock, id_factory=EventIdFactory(kit.clock, lambda n: 0)
    )
    transitions = []
    for i in range(6):
        if i == 3:
            sync.receive(kit.publish("cfg-13"))
        kit.clock.advance(100)
        active = sync.begin_tick()
        world = _world(kit.clock.now_ms() - 50)
        evaluation = active.evaluate(world, Origin.LIVE)
        step = tracker.apply(evaluation, world, config_version=active.config_version)
        transitions += [(t, active.config_version) for t in step.transitions]
    assert transitions, "R1 이벤트가 생겨야 한다"
    for t, tick_version in transitions:
        assert t.state.config_version == tick_version
        assert t.to_payload()["config_version"] == tick_version
    assert {v for _, v in transitions} == {"cfg-12", "cfg-13"}


# --- 동시성·반복 무작위 -------------------------------------------------------------


def test_concurrent_receive_never_yields_half_applied_tick(kit):
    sync = connected_sync_with(kit, "cfg-0")
    messages = [kit.publish(f"cfg-{i}", issued_at=kit.clock.now_ms() + i) for i in range(1, 41)]
    stop = threading.Event()
    seen = []
    errors = []

    def judge():
        try:
            while not stop.is_set():
                active = sync.begin_tick()
                assert_consistent(active)
                seen.append(active.config_version)
                time.sleep(0.0005)  # 판정 주기 흉내(수신 스레드에 GIL 을 넘긴다)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=judge)
    thread.start()
    try:
        for message in messages:
            assert sync.receive(message).accepted
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert sync.begin_tick().config_version == "cfg-40"
    # 판정이 본 버전은 순서대로만 바뀐다(되돌아가지 않는다)
    order = [int(v.split("-")[1]) for v in seen]
    assert order == sorted(order)
    applied = [r.config_version for r in sync.applied()]
    assert len(applied) == len(set(applied))


def _random_run(kit, rng):
    """무작위 연결 단절·발행(정상·변조·참조 깨짐)·판정 뒤, 복구 + 새 묶음 + 판정 한 번으로 새 묶음이 적용되는지."""
    sync = connected_sync_with(kit, "cfg-r0")
    n = 0
    history_snapshot = list(sync.versions())
    for _ in range(rng.randint(5, 40)):
        op = rng.random()
        kit.clock.advance(rng.randint(1, 500))
        if op < 0.15:
            sync.on_disconnected()
        elif op < 0.3:
            sync.on_connected()
        elif op < 0.6:
            n += 1
            bad = rng.choice([None, None, "manifest", "zones", "ref"])
            if bad == "ref":
                message = kit.publish(f"cfg-r{n}", rule_versions=(("R9", 1),))
            else:
                message = kit.publish(f"cfg-r{n}", tamper=bad)
            before = sync.active
            result = sync.receive(message)
            if bad is not None:
                assert not result.accepted and sync.active is before
        else:
            active = sync.begin_tick()
            assert_consistent(active)
            if not sync.connected:
                assert sync.state is S.UNVERIFIED
        # 덮어쓰기 없음: 이전 묶음 목록은 앞부분이 그대로다
        versions = list(sync.versions())
        assert versions[: len(history_snapshot)] == history_snapshot
        history_snapshot = versions
    # 복구 후 새 묶음 원자 전환
    sync.on_disconnected()
    kit.clock.advance(1000)
    sync.on_connected()
    final = kit.publish(f"cfg-r{n + 1}")
    assert sync.receive(final).accepted
    kit.clock.advance(1)
    active = sync.begin_tick()
    assert_consistent(active)
    ok = active.config_version == f"cfg-r{n + 1}" and sync.state is S.CURRENT
    acks = sync.take_acks()
    applied = [a for a in acks if a["payload"]["result"] == "APPLIED"]
    ok = ok and applied[-1]["payload"]["config_version"] == f"cfg-r{n + 1}"
    for ack in acks:
        assert_ack_valid(ack)
    return ok


def test_random_offline_recovery_switch_is_always_atomic(kit):
    """부록 D-G3-06: 오프라인 중 유지·UNVERIFIED, 복구 후 새 묶음 원자적 전환 100%."""
    runs = 200
    passed = sum(_random_run(type(kit)(), random.Random(seed)) for seed in range(runs))
    assert passed == runs


# --- 리뷰 회귀: 검증 중 표시(_inflight)·EC2 최신(_remote) 관리 ------------------------


@pytest.mark.parametrize("error", [TimeoutError, ConnectionResetError])
def test_os_error_during_download_rejects_and_clears_inflight(kit, error):
    sync = connected_sync_with(kit, "cfg-12")
    message = kit.publish("cfg-13")
    original = kit.fetcher.fetch

    def failing(uri):
        raise error("timed out")

    kit.fetcher.fetch = failing
    result = sync.receive(message)
    assert result.reason is RejectReason.DOWNLOAD_FAILED
    assert_ack_valid(result.ack)
    assert sync.state is S.STALE and not sync._inflight
    # 같은 알림이 다시 오면 다시 검증하고 적용한다
    kit.fetcher.fetch = original
    assert sync.receive(message).accepted
    kit.clock.advance(1)
    assert sync.begin_tick().config_version == "cfg-13"
    assert sync.state is S.CURRENT
    assert sync.take_acks()[-1]["payload"]["result"] == "APPLIED"


def test_unexpected_error_does_not_leave_pending(kit):
    sync = connected_sync_with(kit, "cfg-12")
    message = kit.publish("cfg-13")
    original = kit.fetcher.fetch

    def broken(uri):
        raise RuntimeError("버그")

    kit.fetcher.fetch = broken
    result = sync.receive(message)
    # 계약 enum 안에서 가장 가까운 DOWNLOAD_FAILED 로 거부 ACK 를 낸다(EC2 가 '적용 대기'에 머물지 않게)
    assert not result.accepted and result.reason is RejectReason.DOWNLOAD_FAILED
    assert_ack_valid(result.ack)
    assert "RuntimeError" in result.ack["payload"]["reject_detail"]
    assert result.ack["payload"]["active_config_version"] == "cfg-12"
    assert not sync._inflight and sync.state is S.STALE
    kit.fetcher.fetch = original
    assert sync.receive(message).accepted  # 삼켜지지 않고 다시 검증된다
    kit.clock.advance(1)
    assert sync.begin_tick().config_version == "cfg-13"
    assert sync.state is S.CURRENT


def test_reannounce_during_verification_after_reconnect_becomes_current(kit):
    sync = connected_sync_with(kit, "cfg-12")
    message = kit.publish("cfg-13")
    original = kit.fetcher.fetch
    results = []

    def reconnect_and_resend(uri):
        if uri.endswith("manifest.json") and not results:
            sync.on_disconnected()
            sync.on_connected()
            results.append(sync.receive(message))  # 재연결 규약대로 같은 최신 알림을 다시 보낸다
        return original(uri)

    kit.fetcher.fetch = reconnect_and_resend
    assert sync.receive(message).accepted
    assert results[0].accepted
    kit.clock.advance(1)
    assert sync.begin_tick().config_version == "cfg-13"
    assert sync.state is S.CURRENT


def test_older_notice_during_verification_does_not_roll_back_remote(kit):
    sync = connected_sync_with(kit, "cfg-0")
    newer = kit.publish("cfg-2", issued_at=kit.clock.now_ms() + 20)
    older = kit.publish("cfg-1", issued_at=kit.clock.now_ms() + 10)
    original = kit.fetcher.fetch
    results = []

    def deliver_older_meanwhile(uri):
        if uri.endswith("cfg-2/manifest.json") and not results:
            results.append(sync.receive(older))
        return original(uri)

    kit.fetcher.fetch = deliver_older_meanwhile
    assert sync.receive(newer).accepted
    assert results[0].reason is RejectReason.REFERENCE_INVALID  # 검증 중인 더 새 묶음보다 오래됐다
    kit.clock.advance(1)
    assert sync.begin_tick().config_version == "cfg-2"
    assert sync.state is S.CURRENT


def _concurrent_run(kit, rng):
    """수신 스레드 여럿이 순서가 뒤바뀐 알림을 무작위 지연으로 동시에 보낸다. 끝나면 최신 묶음이 CURRENT 여야 한다."""
    sync = connected_sync_with(kit, "cfg-c0")
    t0 = kit.clock.now_ms()
    messages = [kit.publish(f"cfg-c{i}", issued_at=t0 + i) for i in range(1, 21)]
    rng.shuffle(messages)
    original = kit.fetcher.fetch
    delays = {m["payload"]["bundle_uri"]: rng.random() * 0.002 for m in messages}

    def slow(uri):
        time.sleep(delays.get(uri, 0.0))
        return original(uri)

    kit.fetcher.fetch = slow
    chunks = [messages[i::3] for i in range(3)]
    stop = threading.Event()
    errors = []

    def receiver(chunk):
        try:
            for m in chunk:
                sync.receive(m)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def judge():
        while not stop.is_set():
            active = sync.begin_tick()
            assert_consistent(active)
            time.sleep(0.0003)

    judge_thread = threading.Thread(target=judge)
    judge_thread.start()
    threads = [threading.Thread(target=receiver, args=(c,)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    judge_thread.join()
    assert not errors and not sync._inflight
    # 최신(cfg-c20)은 어떤 순서로 와도 거부되지 않는다. 오래된 알림이 EC2 최신 기록을 뒤로 돌리지 않으면 CURRENT 다
    final = sync.begin_tick()
    return final.config_version == "cfg-c20" and sync.state is S.CURRENT


def test_concurrent_reordered_receive_ends_current(kit):
    runs = 50
    failures = [seed for seed in range(runs) if not _concurrent_run(type(kit)(), random.Random(seed))]
    assert failures == []


def test_release_only_removes_own_inflight_entry(kit):
    """같은 버전을 다시 받아 새 검증 중 항목이 생겼으면, 앞선 수신의 정리가 그 항목을 지우지 않는다."""
    from simai_ai.config.sync import _Remote

    sync = connected_sync_with(kit, "cfg-12")
    old = _Remote("cfg-13", "sha256:" + "a" * 64, 1)
    new = _Remote("cfg-13", "sha256:" + "a" * 64, 1)  # 값은 같아도 다른 수신의 항목
    sync._inflight["cfg-13"] = new
    assert not sync._release(old)
    assert sync._inflight["cfg-13"] is new
    assert sync._release(new) and not sync._inflight


def test_failed_receive_cleanup_keeps_resend_in_flight(kit):
    """앞선 수신이 거부로 끝나 정리하는 사이 같은 묶음이 다시 와 검증 중이면, 그 표시는 남고 결국 적용된다."""
    sync = connected_sync_with(kit, "cfg-12")
    message = kit.publish("cfg-13")
    original = kit.fetcher.fetch
    calls = {"n": 0}
    resent = []
    orig_release = sync._release

    def flaky(uri):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("첫 시도 시간 초과")
        return original(uri)

    def release_then_resend(entry):
        released = orig_release(entry)
        if released and not resent:
            # 정리 직후(잠금 안) 다른 스레드가 같은 알림을 다시 받아 검증을 시작한 상황을 흉내 낸다
            again = type(entry)(entry.config_version, entry.bundle_hash, entry.issued_at)
            sync._inflight[entry.config_version] = again
            resent.append(again)
        return released

    kit.fetcher.fetch = flaky
    sync._release = release_then_resend
    result = sync.receive(message)
    assert result.reason is RejectReason.DOWNLOAD_FAILED
    # 앞선 수신의 finally 가 다시 받은 항목을 지우지 않았다
    assert sync._inflight.get("cfg-13") is resent[0]
    assert sync.state is S.PENDING
