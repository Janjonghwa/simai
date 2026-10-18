"""카메라별 TLS 전송(S15P31A101-83): 4대 동시, 한 카메라 정지가 다른 카메라를 막지 않음, 재접속 시 codec_info → 키프레임,
공개키 고정·토큰 비노출, 송신 큐 넘침, 엔드포인트 변경. 로컬 TLS(자체 서명, tmp_path 에서 만들고 커밋하지 않음)로 돈다."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import SystemClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import SyntheticSource
from simai_site_alert.endpoint import EndpointUnavailable, Tier0Endpoint
from tls_helpers import FakeTier0Video, RawTlsServer, wait_until

from simai_relay import cli, wire
from simai_relay.bench import SelfSigned, codec_info, frame, make_self_signed
from simai_relay.config import secret_values
from simai_relay.relay import RelaySession
from simai_relay.tls_sink import EndpointWatcher, SendQueue, TlsCameraSink, TlsSinkFactory, VideoTarget

TOKEN = "device-token-S3CRET-83"
CAMS = ["cam_A", "cam_B", "cam_C", "cam_D"]

# 양쪽(노트북 wire, Tier 0 video_ingest) 시험이 같은 바이트를 쓴다: frame_header 예시 + 압축 프레임 8바이트
GOLDEN_FRAME = bytes.fromhex(
    "04000000eb000000087b2263616d6572615f6964223a2263616d5f41222c2273747265616d5f65706f6368223a31322c226672616d655f73"
    "657175656e6365223a31303332342c22736f757263655f707473223a393837363534332c227074735f74696d6562617365223a22312f3930"
    "303030222c22657374696d617465645f636170747572655f74696d655f6d73223a313736303334323430303130302c2274696d657374616d"
    "705f736f75726365223a22524543454956455f455354494d41544544222c2274696d657374616d705f756e6365727461696e74795f6d7322"
    "3a31382c226b65796672616d65223a747275657d0000000165888400")
GOLDEN_SKIP = bytes.fromhex(
    "0500000054000000007b2263616d6572615f6964223a2263616d5f41222c2273747265616d5f65706f6368223a31322c2266697273745f73"
    "657175656e6365223a31303330302c226c6173745f73657175656e6365223a31303332337d")


@pytest.fixture
def cert(tmp_path: Path) -> SelfSigned:
    return make_self_signed(tmp_path / "a", "tier0")


@pytest.fixture
def other_cert(tmp_path: Path) -> SelfSigned:
    return make_self_signed(tmp_path / "b", "impostor")


def _sink(cam: str, target, cert: SelfSigned, **kw) -> TlsCameraSink:
    kw.setdefault("reconnect_delays_s", (0.05, 0.1))
    return TlsCameraSink(cam, target if callable(target) else (lambda: target), device_token=TOKEN,
                         pins=kw.pop("pins", frozenset({cert.fingerprint})), **kw)


def _feed(sinks: dict, seconds: float, *, fps: float = 25, gop: int = 25, key: int = 2000, p: int = 500) -> dict:
    """카메라마다 합성 프레임을 fps 로 넣는다(같은 스레드에서 돌아가며). 카메라별로 보낸 마지막 번호를 돌려준다."""
    for cam, sink in sinks.items():
        sink.send_codec_info(codec_info(cam))
    t0 = time.perf_counter()
    i = 0
    while time.perf_counter() - t0 < seconds:
        for cam, sink in sinks.items():
            sink.send_frame(frame(cam, i, i % gop == 0, b"\x00" * (key if i % gop == 0 else p)))
        i += 1
        time.sleep(max(0.0, t0 + i / fps - time.perf_counter()))
    return dict.fromkeys(sinks, i - 1)


# ---------------------------------------------------------------------------- 형식


def test_wire_matches_golden_bytes():
    header = {"camera_id": "cam_A", "stream_epoch": 12, "frame_sequence": 10324, "source_pts": 9876543,
              "pts_timebase": "1/90000", "estimated_capture_time_ms": 1760342400100,
              "timestamp_source": "RECEIVE_ESTIMATED", "timestamp_uncertainty_ms": 18, "keyframe": True}
    assert wire.encode(wire.FRAME, header, bytes.fromhex("0000000165888400")) == GOLDEN_FRAME
    assert wire.encode(wire.SKIP_NOTICE, {"camera_id": "cam_A", "stream_epoch": 12, "first_sequence": 10300,
                                          "last_sequence": 10323}) == GOLDEN_SKIP
    with pytest.raises(wire.WireError):
        wire.encode(wire.FRAME, header, b"\x00" * (wire.MAX_DATA + 1))


# ---------------------------------------------------------------------------- 송신 큐 (소켓 없이)


def _f(seq: int, key: bool, cam: str = "cam_A", epoch: int = 0):
    return frame(cam, seq, key, b"x", epoch=epoch)


def test_send_queue_overflow_skips_until_keyframe_and_notices_range():
    q = SendQueue(max_frames=3)
    q.put_codec(codec_info("cam_A"))
    assert q.link_up()[0] is not None
    for seq, key in [(0, True), (1, False), (2, False)]:
        q.put_frame(_f(seq, key))
    q.put_frame(_f(3, False))  # 가득: 여기부터 다음 키프레임까지 버림
    q.put_frame(_f(4, False))
    assert q.stats.skipped == 2 and q.stats.skip_runs == 1
    q.get(0)  # 하나 비워도 키프레임 전까지는 계속 버린다
    q.put_frame(_f(5, False))
    assert q.stats.skipped == 3
    q.put_frame(_f(6, True))  # 키프레임 + 자리 있음: SKIP_NOTICE 뒤 키프레임
    items = []
    while (item := q.get(0)) is not None:
        items.append(item)
    kinds = [k for k, _ in items]
    assert kinds == ["frame", "frame", "skip", "frame"]
    notice = items[2][1]
    assert notice == {"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 3, "last_sequence": 5}
    assert items[3][1].header["frame_sequence"] == 6
    assert q.stats.dropped_link == 0


def test_send_queue_keyframe_dropped_when_still_full():
    q = SendQueue(max_frames=1)
    q.link_up()
    q.put_frame(_f(0, True))
    q.put_frame(_f(1, False))  # 넘침
    q.put_frame(_f(2, True))  # 키프레임이지만 아직 가득 → 버림
    assert q.stats.skipped == 2
    q.get(0)
    q.put_frame(_f(3, False))
    q.put_frame(_f(4, True))
    items = [q.get(0), q.get(0)]
    assert items[0][1]["first_sequence"] == 1 and items[0][1]["last_sequence"] == 3
    assert items[1][1].header["frame_sequence"] == 4


def test_send_queue_link_down_and_resync_start_at_keyframe():
    q = SendQueue(max_frames=5)
    q.put_codec(codec_info("cam_A", epoch=3))
    q.put_frame(_f(0, True, epoch=3))  # 연결 없음
    assert q.stats.dropped_link == 1 and q.depth == 0
    codec, notices = q.link_up()
    assert notices == []
    assert codec is not None and codec.codec_info["stream_epoch"] == 3  # 연결 첫 레코드로 보낼 codec_info
    q.put_frame(_f(1, False, epoch=3))  # 키프레임 전
    q.put_frame(_f(2, True, epoch=3))
    q.put_frame(_f(3, False, epoch=3))
    assert q.stats.dropped_link == 2 and q.depth == 2
    q.link_down()  # 끊길 때 큐에 남은 프레임은 dropped_link (skipped 와 따로)
    assert q.stats.dropped_link == 4 and q.stats.skipped == 0 and q.depth == 0
    # 새 epoch codec_info 는 연결 중이면 큐에 들어가고, 그 뒤 키프레임부터
    q.link_up()
    q.put_codec(codec_info("cam_A", epoch=4))
    q.put_frame(_f(0, False, epoch=4))
    q.put_frame(_f(1, True, epoch=4))
    kinds = [q.get(0)[0], q.get(0)[0]]
    assert kinds == ["codec", "frame"] and q.get(0) is None


# ---------------------------------------------------------------------------- 실제 TLS


def test_four_cameras_relay_concurrently(cert, contract_validator):
    with FakeTier0Video(cert, {TOKEN}) as server:
        target = VideoTarget("127.0.0.1", server.port, cert.fingerprint)
        factory = TlsSinkFactory(lambda cam: (lambda: target), device_token=TOKEN, pins=frozenset({cert.fingerprint}),
                                 reconnect_delays_s=(0.05,))
        clock = SystemClock()
        cams = [CameraSpec(c, SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=0.4),
                           CaptureTimeModel()) for c in CAMS]
        session = RelaySession(cams, factory, clock, reconnect_delays_s=(0.1,))
        session.start(2.5)
        wait_until(lambda: sorted(factory.video_cameras()) == CAMS, 5)
        assert session.join(10)
        for cam in CAMS:
            [conn] = server.by_camera(cam)
            assert conn.hello["protocol"] == wire.PROTOCOL and conn.hello["device_token"] == TOKEN
            kinds = conn.kinds()
            assert kinds[0] == wire.CODEC_INFO and set(kinds[1:]) == {wire.FRAME}
            contract_validator("codec_info", conn.records[0][1])
            frames = conn.frames()
            assert len(frames) >= 40
            assert frames[0]["keyframe"] is True
            seqs = [f["frame_sequence"] for f in frames]
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # 빈칸 없음
            for f in frames:
                contract_validator("frame_header", f)
                assert f["camera_id"] == cam and f["stream_epoch"] == conn.records[0][1]["stream_epoch"]
        for snap in factory.snapshots():
            assert snap["skipped"] == 0 and snap["pin_rejections"] == 0 and snap["auth_rejections"] == 0


def test_stalled_camera_does_not_block_others(cert):
    with FakeTier0Video(cert, {TOKEN}, stall={"cam_A"}, rcvbuf=16 * 1024) as server:
        target = VideoTarget("127.0.0.1", server.port, cert.fingerprint)
        sinks = {c: _sink(c, target, cert, max_queue_frames=5, sndbuf_bytes=16 * 1024, send_timeout_s=30)
                 for c in CAMS}
        wait_until(lambda: all(s.connected for s in sinks.values()), 5)
        last = _feed(sinks, 3.0, key=40_000, p=20_000)
        for cam in CAMS[1:]:
            wait_until(lambda cam=cam: len(server.frames(cam)) == last[cam] + 1, 5)
        a = sinks["cam_A"].snapshot()
        assert a["skipped"] > 0 and a["skip_runs"] >= 1  # 멈춘 카메라만 큐가 넘쳐 다음 키프레임까지 버림
        for cam in CAMS[1:]:
            frames = server.frames(cam)
            assert [f["frame_sequence"] for f in frames] == list(range(last[cam] + 1))
            snap = sinks[cam].snapshot()
            assert snap["skipped"] == 0 and snap["dropped_link"] == 0
        for s in sinks.values():
            s.close()


def test_skip_notice_reaches_tier0_with_skipped_range(cert):
    with FakeTier0Video(cert, {TOKEN}) as server:
        target = VideoTarget("127.0.0.1", server.port, cert.fingerprint)
        sink = _sink("cam_A", target, cert, max_queue_frames=2)
        wait_until(lambda: sink.connected, 5)
        q = sink.queue
        sink.send_codec_info(codec_info("cam_A"))
        with q._cond:  # 송신 스레드가 꺼내지 못하게 잡고 넘치게 만든다(결정적. Condition 기본 잠금은 재진입 가능)
            for seq, key in [(0, True), (1, False), (2, False), (3, False), (4, False)]:
                sink.send_frame(frame("cam_A", seq, key, b"x"))
        wait_until(lambda: q.depth == 0, 5)  # 0·1 을 보낸 뒤에 끝내는 키프레임
        sink.send_frame(frame("cam_A", 5, True, b"x"))
        wait_until(lambda: any(f["frame_sequence"] == 5 for f in server.frames("cam_A")), 5)
        sink.close()
        [conn] = server.by_camera("cam_A")
        notices = [m for k, m, _, _ in conn.records if k == wire.SKIP_NOTICE]
        seqs = [f["frame_sequence"] for f in conn.frames()]
        skipped = sink.snapshot()["skipped"]
        assert skipped == 3
        assert notices == [{"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 2, "last_sequence": 4}]
        assert seqs == [0, 1, 5]


def test_reconnect_sends_codec_info_then_keyframe(cert, contract_validator):
    with FakeTier0Video(cert, {TOKEN}, close_after={"cam_A": 10}) as server:
        target = VideoTarget("127.0.0.1", server.port, cert.fingerprint)
        sink = _sink("cam_A", target, cert)
        wait_until(lambda: sink.connected, 5)
        _feed({"cam_A": sink}, 2.5, gop=10)
        wait_until(lambda: len(server.by_camera("cam_A")) >= 2 and len(server.by_camera("cam_A")[1].frames()) > 5, 5)
        first, second = server.by_camera("cam_A")[:2]
        assert second.kinds()[0] == wire.CODEC_INFO  # 재접속 첫 레코드
        contract_validator("codec_info", second.records[0][1])
        assert second.records[0][1]["stream_epoch"] == first.records[0][1]["stream_epoch"]
        resumed = second.frames()
        assert resumed[0]["keyframe"] is True  # 키프레임부터
        assert resumed[0]["frame_sequence"] > first.frames()[-1]["frame_sequence"]
        snap = sink.snapshot()
        assert snap["connects"] >= 2 and snap["dropped_link"] > 0 and snap["skipped"] == 0
        sink.close()


def test_pin_mismatch_sends_nothing_and_never_logs_token(cert, other_cert, caplog):
    caplog.set_level(logging.DEBUG)
    with RawTlsServer(other_cert) as impostor:  # 고정 목록에 있는 지문을 내세우지만 다른 키를 가진 서버
        sink = _sink("cam_A", VideoTarget("127.0.0.1", impostor.port, cert.fingerprint), cert)
        wait_until(lambda: sink.snapshot()["pin_rejections"] >= 2, 5)
        sink.send_codec_info(codec_info("cam_A"))
        sink.send_frame(frame("cam_A", 0, True, b"x"))
        sink.close()
        assert impostor.accepted >= 2
        assert bytes(impostor.received) == b""  # HELLO(기기 토큰)를 포함해 아무것도 보내지 않았다
    assert sink.snapshot()["sent_frames"] == 0
    assert TOKEN not in caplog.text and TOKEN not in repr(sink)


def test_fingerprint_outside_pins_does_not_even_connect(cert, other_cert):
    with RawTlsServer(cert) as server:
        sink = _sink("cam_A", VideoTarget("127.0.0.1", server.port, cert.fingerprint), cert,
                     pins=frozenset({other_cert.fingerprint}))
        wait_until(lambda: sink.snapshot()["pin_rejections"] >= 2, 5)
        sink.close()
        assert server.accepted == 0


def test_rejected_token_counts_and_stays_out_of_logs(cert, caplog):
    caplog.set_level(logging.DEBUG)
    with FakeTier0Video(cert, {"another-token"}) as server:
        sink = _sink("cam_A", VideoTarget("127.0.0.1", server.port, cert.fingerprint), cert)
        wait_until(lambda: sink.snapshot()["auth_rejections"] >= 2, 5)
        sink.close()
    assert not sink.connected and sink.snapshot()["sent_frames"] == 0
    assert TOKEN not in caplog.text
    assert "unauthorized" in caplog.text


def _endpoint(port: int, fp: str, cams: list[str]) -> Tier0Endpoint:
    return Tier0Endpoint("tier0-01", "127.0.0.1", 1, dict.fromkeys(cams, port), fp, 1760340000100)


def test_endpoint_change_reconnects_to_new_port(cert):
    with FakeTier0Video(cert, {TOKEN}) as one, FakeTier0Video(cert, {TOKEN}) as two:
        current = {"ep": _endpoint(one.port, cert.fingerprint, ["cam_A"])}
        watcher = EndpointWatcher(lambda: current["ep"], frozenset({cert.fingerprint}))
        assert watcher.refresh()
        sink = _sink("cam_A", watcher.source("cam_A"), cert)
        wait_until(lambda: sink.connected and sink.current_target.port == one.port, 5)
        sink.send_codec_info(codec_info("cam_A"))
        sink.send_frame(frame("cam_A", 0, True, b"x"))
        wait_until(lambda: len(one.frames("cam_A")) == 1, 5)
        current["ep"] = _endpoint(two.port, cert.fingerprint, ["cam_A"])  # Pod 재설정: 포트가 바뀜
        assert watcher.refresh()
        wait_until(lambda: sink.connected and sink.current_target.port == two.port, 5)
        sink.send_frame(frame("cam_A", 1, False, b"x"))  # 키프레임 전: 버림
        sink.send_frame(frame("cam_A", 2, True, b"x"))
        wait_until(lambda: len(two.frames("cam_A")) == 1, 5)
        [conn] = two.by_camera("cam_A")
        assert conn.kinds() == [wire.CODEC_INFO, wire.FRAME] and conn.frames()[0]["frame_sequence"] == 2
        sink.close()


def test_watcher_keeps_previous_endpoint_on_unpinned_or_failed_lookup(cert, other_cert):
    good = _endpoint(1111, cert.fingerprint, ["cam_A"])
    answers = [good, _endpoint(2222, other_cert.fingerprint, ["cam_A"]), EndpointUnavailable("조회 실패")]

    def fetch():
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    watcher = EndpointWatcher(fetch, frozenset({cert.fingerprint}))
    assert watcher.refresh() and watcher.target_for("cam_A").port == 1111
    assert not watcher.refresh() and watcher.target_for("cam_A").port == 1111  # 고정 밖 지문: 쓰지 않음
    assert not watcher.refresh() and watcher.target_for("cam_A").port == 1111  # 조회 실패: 그대로
    assert watcher.target_for("cam_Z") is None
    with pytest.raises(ValueError):
        EndpointWatcher(fetch, frozenset())


# ---------------------------------------------------------------------------- 설정·비밀 값


def test_send_requires_env_and_names_only_missing_keys(tmp_path, capsys, monkeypatch):
    for k in ("SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN", "SIMAI_TIER0_PINNED_SPKI_SHA256"):
        monkeypatch.delenv(k, raising=False)
    config = tmp_path / "c.toml"
    config.write_text('[[cameras]]\ncamera_id = "cam_A"\nkind = "synthetic"\n', encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(f"SIMAI_DEVICE_TOKEN={TOKEN}\n", encoding="utf-8")
    code = cli.main(["run", "--config", str(config), "--seconds", "0.2", "--env-file", str(env_file), "--send"])
    err = capsys.readouterr().err
    assert code == 2
    assert "SIMAI_EC2_BASE_URL" in err and "SIMAI_TIER0_PINNED_SPKI_SHA256" in err and TOKEN not in err


def test_device_token_is_redacted_from_logs():
    assert TOKEN in secret_values({"SIMAI_DEVICE_TOKEN": TOKEN, "SIMAI_LAPTOP_ID": "laptop-01"})
    assert "laptop-01" not in secret_values({"SIMAI_LAPTOP_ID": "laptop-01"})


# ---------------------------------------------------------------------------- 리뷰 반영: epoch 예약, SKIP_NOTICE 재전송, 거절


def test_send_queue_resends_notices_even_after_earlier_frame_completes():
    """앞선 프레임이 끝나도 아직(또는 이미) 보낸 SKIP_NOTICE 를 잊지 않고, 새 연결마다 지금 epoch 알림을 다시 보낸다."""
    q = SendQueue(max_frames=1)
    q.put_codec(codec_info("cam_A", epoch=5))
    q.link_up()
    q.put_frame(_f(0, True, epoch=5))
    assert q.get(0)[1].header["frame_sequence"] == 0  # 0 을 보내는 중
    q.put_frame(_f(1, False, epoch=5))  # 큐에 1
    q.put_frame(_f(2, False, epoch=5))  # 가득: 2 부터 버림
    assert q.get(0)[1].header["frame_sequence"] == 1  # 1 을 보내는 중(0 은 끝남)
    q.put_frame(_f(3, True, epoch=5))  # SKIP_NOTICE(2~2) + 3 이 큐에 들어감(아직 안 보냄)
    q.link_down()  # 1 만 끝내고 끊김
    codec, notices = q.link_up()
    assert codec.codec_info["stream_epoch"] == 5
    assert notices == [{"camera_id": "cam_A", "stream_epoch": 5, "first_sequence": 2, "last_sequence": 2}]
    # 버리던 중에 끊긴 범위도 알림이 된다. 이미 보낸 알림도 다음 연결에서 또 보낸다(Tier 0 는 지난 범위를 무시)
    q.put_frame(_f(10, True, epoch=5))
    q.put_frame(_f(11, False, epoch=5))
    q.put_frame(_f(12, False, epoch=5))
    q.link_down()
    _, notices = q.link_up()
    assert [(n["first_sequence"], n["last_sequence"]) for n in notices] == [(2, 2), (11, 12)]
    q.put_codec(codec_info("cam_A", epoch=6))  # 새 epoch: 이전 epoch 알림은 버린다
    q.link_down()
    assert q.link_up()[1] == []


def test_reconnect_resends_skip_notice_before_resumed_keyframe(cert):
    with FakeTier0Video(cert, {TOKEN}) as server:
        target = {"t": None}
        sink = _sink("cam_A", lambda: target["t"], cert)
        q = sink.queue
        # 지난 연결: codec_info → 키프레임 0 → 넘침(1~4 버림) → 끝내는 키프레임 전에 끊김
        q.put_codec(codec_info("cam_A"))
        q.link_up()
        sink.send_frame(frame("cam_A", 0, True, b"x"))
        with q._cond:
            q.max_frames = 1
        for seq in range(1, 5):
            sink.send_frame(frame("cam_A", seq, False, b"x"))
        q.link_down()
        with q._cond:
            q.max_frames = 25
        target["t"] = VideoTarget("127.0.0.1", server.port, cert.fingerprint)  # 다시 붙는다
        wait_until(lambda: sink.connected, 5)
        sink.send_frame(frame("cam_A", 5, True, b"x"))
        wait_until(lambda: len(server.frames("cam_A")) == 1, 5)
        [conn] = server.by_camera("cam_A")
        assert conn.kinds() == [wire.CODEC_INFO, wire.SKIP_NOTICE, wire.FRAME]
        assert conn.records[1][1] == {"camera_id": "cam_A", "stream_epoch": 0, "first_sequence": 1,
                                      "last_sequence": 4}
        sink.close()


def test_skip_during_earlier_frame_send_then_disconnect_is_renoticed(cert):
    """큰 앞선 프레임을 보내는 중에 새로 버린 범위가 생기고 끊겨도, 다음 연결이 그 범위를 키프레임 앞에 알린다(실제 TLS)."""
    big = b"\x01" * (1024 * 1024)
    with FakeTier0Video(cert, {TOKEN}, rcvbuf=32 * 1024, pause_s=0.5, close_after={"cam_A": 1}) as server:
        sink = _sink("cam_A", VideoTarget("127.0.0.1", server.port, cert.fingerprint), cert, max_queue_frames=1,
                     sndbuf_bytes=8 * 1024, send_timeout_s=20)
        wait_until(lambda: sink.connected, 5)
        sink.send_codec_info(codec_info("cam_A"))
        sink.send_frame(frame("cam_A", 0, True, big))  # 보내는 중(서버가 0.5초 멈춤)
        wait_until(lambda: sink.queue.depth == 0, 5)
        for seq in range(1, 4):  # 1 은 큐, 2·3 은 버림
            sink.send_frame(frame("cam_A", seq, False, b"p"))
        sink.send_frame(frame("cam_A", 4, True, b"k"))  # 가득이면 버림, 자리가 있으면 알림+키프레임
        wait_until(lambda: sink.snapshot()["connects"] >= 2, 10)  # 서버가 프레임 0 뒤 끊음 → 다시 붙음
        wait_until(lambda: sink.connected, 5)
        sink.send_frame(frame("cam_A", 9, True, b"k"))
        wait_until(lambda: len(server.by_camera("cam_A")) >= 2 and server.by_camera("cam_A")[-1].frames(), 5)
        second = server.by_camera("cam_A")[-1]
        kinds = second.kinds()
        assert kinds[0] == wire.CODEC_INFO and kinds[-1] == wire.FRAME
        notices = [m for k, m, _, _ in second.records if k == wire.SKIP_NOTICE]
        covered = {n for m in notices for n in range(m["first_sequence"], m["last_sequence"] + 1)}
        assert {2, 3} <= covered  # 버린 2·3 은 손실이 아니라 skipped 로 알림
        assert kinds.index(wire.SKIP_NOTICE) < kinds.index(wire.FRAME)
        sink.close()


def test_epoch_store_never_reuses_epoch_across_restart_clock_regression_and_same_ms(tmp_path):
    from simai_relay.epoch_store import EpochStore

    path = tmp_path / "epochs.json"
    store = EpochStore(path)
    assert store.reserve("cam_A", floor=1_000) == 1_000  # 첫 실행: 시작 ms
    assert store.reserve("cam_A", floor=1_000) == 1_001  # RTSP 재접속
    assert store.reserve("cam_B", floor=1_000) == 1_000  # 카메라마다 따로
    again = EpochStore(path)  # 정상 재시작(시계가 앞으로)
    assert again.reserve("cam_A", floor=9_000) == 9_000
    back = EpochStore(path)  # 시계가 뒤로 감
    assert back.reserve("cam_A", floor=500) == 9_001
    same = EpochStore(path)  # 같은 시작 ms
    assert same.reserve("cam_A", floor=9_001) == 9_002 and same.reserve("cam_B", floor=1_000) == 1_001
    assert sorted(p.name for p in tmp_path.iterdir()) == ["epochs.json"]  # 임시 파일이 남지 않음
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        EpochStore(path)  # 깨진 파일을 0 부터 다시 세지 않는다


def test_relay_restart_with_epoch_store_and_injected_clock(tmp_path):
    """중계 재시작(시계 역행 포함)·RTSP 재접속 뒤에도 epoch 가 줄지 않고, 저장에 실패한 epoch 로는 보내지 않는다."""
    import threading

    from simai_recorder.clock import FakeClock
    from simai_recorder.sources import FaultPlan, FaultySource

    from simai_relay.epoch_store import EpochStore
    from simai_relay.relay import CameraRelay
    from simai_relay.sink import I0CodecInfo, MemorySink

    def run(clock, store, disconnects=(), allocator=None):
        sink = MemorySink("cam_A")
        src = FaultySource(SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=0.2),
                           FaultPlan(disconnect_after=disconnects), clock)
        start = clock.now_ms()
        relay = CameraRelay(CameraSpec("cam_A", src, CaptureTimeModel()), sink, clock, threading.Event(),
                            clock.monotonic() + 1.5, reconnect_delays_s=(0.1,),
                            epoch_allocator=allocator or (lambda: store.reserve("cam_A", floor=start)))
        relay.run()
        assert relay.error is None
        return [r.codec_info["stream_epoch"] for r in sink.records if isinstance(r, I0CodecInfo)], relay

    path = tmp_path / "epochs.json"
    first, _ = run(FakeClock(1_760_342_400_000), EpochStore(path), disconnects=(10, 10))
    assert first == [1_760_342_400_000, 1_760_342_400_001, 1_760_342_400_002]
    second, _ = run(FakeClock(1_760_342_000_000), EpochStore(path))  # 재시작 + 시계가 400초 뒤로
    assert second == [1_760_342_400_003]
    third, _ = run(FakeClock(1_760_342_000_000), EpochStore(path))  # 같은 시작 ms
    assert third == [1_760_342_400_004]
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("디스크 가득")
        return 77

    fourth, relay = run(FakeClock(), EpochStore(tmp_path / "other.json"), allocator=flaky)
    assert fourth == [77] and relay.stats.snapshot()["open_failures"] == 1


def test_relay_restart_with_default_epoch_has_higher_epoch_than_previous_session():
    from simai_recorder.sources import FaultPlan, FaultySource

    from simai_relay.sink import I0CodecInfo, MemorySink

    clock = SystemClock()
    runs = []
    for disconnects in ((5, 5, 5), ()):
        sinks: dict = {}

        def factory(cam, sinks=sinks):
            sinks[cam] = MemorySink(cam)
            return sinks[cam]

        src = FaultySource(SyntheticSource(clock, width=64, height=48, fps=25, keyframe_interval_s=0.04),
                           FaultPlan(disconnect_after=disconnects), clock)
        session = RelaySession([CameraSpec("cam_A", src, CaptureTimeModel())], factory, clock,
                               reconnect_delays_s=(0.01,))
        session.start(0.8)
        assert session.join(10)
        runs.append([r.codec_info["stream_epoch"] for s in sinks.values() for r in s.records
                     if isinstance(r, I0CodecInfo)])
    first, second = runs
    assert len(first) >= 3 and first == sorted(first) and len(set(first)) == len(first)
    assert min(second) > max(first)


def test_session_uses_epoch_store(tmp_path):
    from simai_relay.epoch_store import EpochStore
    from simai_relay.sink import I0CodecInfo, MemorySink

    path = tmp_path / "epochs.json"
    EpochStore(path).reserve("cam_A", floor=10**15)  # 이전 실행이 아주 큰 epoch 를 썼다
    clock = SystemClock()
    sinks: dict = {}

    def factory(cam):
        sinks[cam] = MemorySink(cam)
        return sinks[cam]

    session = RelaySession([CameraSpec("cam_A", SyntheticSource(clock, width=64, height=48), CaptureTimeModel())],
                           factory, clock, epoch_store=EpochStore(path))
    session.start(0.5)
    assert session.join(10)
    epochs = [r.codec_info["stream_epoch"] for r in sinks["cam_A"].records if isinstance(r, I0CodecInfo)]
    assert epochs == [10**15 + 1] and EpochStore(path).last("cam_A") == 10**15 + 1


def test_stream_reject_stops_resending_until_new_epoch(cert, caplog):
    caplog.set_level(logging.WARNING)
    with FakeTier0Video(cert, {TOKEN}, reject_epochs_below=10) as server:
        sink = _sink("cam_A", VideoTarget("127.0.0.1", server.port, cert.fingerprint), cert)
        wait_until(lambda: sink.connected, 5)
        sink.send_codec_info(codec_info("cam_A", epoch=3))
        sink.send_frame(frame("cam_A", 0, True, b"x", epoch=3))
        wait_until(lambda: sink.snapshot()["tier0_rejection"] is not None, 5)
        assert sink.snapshot()["tier0_rejection"] == {"reason": "stale_epoch", "stream_epoch": 3, "current_epoch": 10}
        assert sink.snapshot()["stream_rejections"] == 1 and "stream_epoch 3" in caplog.text
        # 거절된 epoch 로는 다시 붙지 않는다: 송신 스레드가 거절 때문에 접속을 여러 번 건너뛸 때까지 기다린 뒤 본다
        wait_until(lambda: sink.snapshot()["held_by_rejection"] >= 3, 5)
        assert len(server.by_camera("cam_A")) == 1 and sink.snapshot()["connects"] == 1
        sink.send_codec_info(codec_info("cam_A", epoch=11))  # 새 스트림
        # 서버는 HELLO 를 받자마자 연결을 기록하지만 노트북 큐는 HELLO_ACK 뒤에야 열린다. 큐가 열린 뒤에 보낸다
        wait_until(lambda: sink.connected and sink.snapshot()["connects"] == 2, 5)
        sink.send_frame(frame("cam_A", 0, True, b"x", epoch=11))
        wait_until(lambda: server.by_camera("cam_A")[1].frames(), 5)
        assert server.by_camera("cam_A")[1].records[0][1]["stream_epoch"] == 11
        assert sink.snapshot()["tier0_rejection"] is None
        sink.close()


def test_partial_writes_under_timeouts_keep_records_intact(cert):
    """수신이 잠시 멈춰 송신이 짧은 시간 제한으로 여러 번 끊겨도 큰 레코드와 뒤 레코드가 바이트 그대로 도착한다(Windows TLS)."""
    import hashlib

    big = bytes(i * 7919 % 251 for i in range(2 * 1024 * 1024))
    with FakeTier0Video(cert, {TOKEN}, rcvbuf=32 * 1024, pause_s=0.7) as server:
        sink = _sink("cam_A", VideoTarget("127.0.0.1", server.port, cert.fingerprint), cert, sndbuf_bytes=8 * 1024,
                     send_timeout_s=20)
        wait_until(lambda: sink.connected, 5)
        sink.send_codec_info(codec_info("cam_A"))
        sink.send_frame(frame("cam_A", 0, True, big))
        sink.send_frame(frame("cam_A", 1, False, b"tail-frame"))
        wait_until(lambda: len(server.frames("cam_A")) == 2, 20)
        [conn] = server.by_camera("cam_A")
        assert conn.digests == [hashlib.sha256(big).hexdigest(), hashlib.sha256(b"tail-frame").hexdigest()]
        assert [f["frame_sequence"] for f in conn.frames()] == [0, 1]
        sink.close()


def _overflow_runs(q: SendQueue, runs: int, epoch: int = 0) -> None:
    """키프레임 → 넘침(1프레임 버림) → 끝내는 키프레임 을 runs 번. 송신 쪽이 바로바로 꺼낸다고 본다."""
    seq = 0
    for _ in range(runs):
        q.put_frame(_f(seq, True, epoch=epoch))  # 큐에 1 (상한 1)
        q.put_frame(_f(seq + 1, False, epoch=epoch))  # 가득: 버림
        while q.get(0) is not None:
            pass
        seq += 2
    q.put_frame(_f(seq, True, epoch=epoch))


def test_more_than_64_skip_runs_are_all_resent_as_spans():
    q = SendQueue(max_frames=1)
    q.put_codec(codec_info("cam_A"))
    q.link_up()
    _overflow_runs(q, 200)
    q.link_down()
    _, notices = q.link_up()
    assert len(notices) == 200 and q.stats.spans_truncated == 0
    assert [(n["first_sequence"], n["last_sequence"]) for n in notices[:2]] == [(1, 1), (3, 3)]
    assert all("unknown_before" not in n for n in notices)
    # 같은 구간 집합을 여러 번 다시 만들어도(재접속 반복) 크기가 늘지 않는다
    for _ in range(50):
        q.link_down()
        assert len(q.link_up()[1]) == 200


def test_span_cap_truncates_oldest_and_marks_unknown_before():
    q = SendQueue(max_frames=1, max_spans=10)
    q.put_codec(codec_info("cam_A"))
    q.link_up()
    _overflow_runs(q, 15)  # 구간 15개 → 오래된 5개(1,3,5,7,9)를 잘라 낸다
    q.link_down()
    _, notices = q.link_up()
    assert len(notices) == 10 and q.stats.spans_truncated == 5
    assert notices[0]["unknown_before"] == 10 and notices[0]["first_sequence"] == 11
    assert all("unknown_before" not in n for n in notices[1:])
    q.put_codec(codec_info("cam_A", epoch=1))  # 새 epoch 에서 비운다
    q.link_down()
    assert q.link_up()[1] == []
