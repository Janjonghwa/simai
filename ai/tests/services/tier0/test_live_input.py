"""Tier 0 LIVE 입력 경로 (S15P31A101-182): I0 TLS 수신 → 링 버퍼·디코딩(실제 H.264, PyAV CPU) → 최신성 대기열 → 검출 → 추적.

반례 장면(리뷰 대상 Tier 0): 키프레임 없이 시작, 코덱 변경 중, 카메라 한 대 끊김·같은 epoch 재접속, 검출기 예외,
검출기가 시각을 바꾼 결과, 같은 촬영 시각 두 번, 프레임 밀림·대기 상한, 촬영 시각 헤더 없음, 수신자별 health 구간.
검출기는 그림에 새긴 번호를 읽어 헤더와 맞는지(G3 결합 오류) 세는 대역이다. 시각은 FixedClock 으로만 움직인다.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_video as lv  # noqa: E402
from test_video_ingest import _make_cert  # noqa: E402

from services.tier0 import video_ingest as vi  # noqa: E402
from services.tier0.clips.ring_buffer import PacketRingBuffer  # noqa: E402
from services.tier0.live_input import LiveInput  # noqa: E402
from services.tier0.perception import DetectorFeed  # noqa: E402
from simai_ai.io import FreshnessConfig, I0Ingest, PyAvDecoder  # noqa: E402
from simai_ai.risk.clock import FixedClock  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections, MultiCameraLocalTracker, PoseDetection  # noqa: E402

T0 = 1_760_342_400_000
TOKEN = "laptop-device-token-182"
CAMS = ("cam_A", "cam_B")
KP17 = tuple((30.0 + i, 50.0 + i, 0.9) for i in range(17))


def wait_until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("시간 안에 조건이 맞지 않았다")
        time.sleep(0.01)


class BarcodeDetector:
    """검출기 대역: 그림의 번호를 읽어 헤더와 비교하고(결합 오류 수), 사람 하나를 낸다. 결과 식별자·시각은 넣은 프레임 값."""

    def __init__(self, index_of) -> None:
        self.index_of = index_of  # (camera_id, frame_sequence) → 그림에 새긴 번호
        self.seen: list[tuple[str, int, int]] = []
        self.misbound = 0
        self.raise_next = 0
        self.shift_time_ms = 0
        self.lock = threading.Lock()

    def detect(self, frames):
        if self.raise_next:
            self.raise_next -= 1
            raise RuntimeError("가짜 검출기 오류(CUDA 오류 흉내)")
        out = []
        for f in frames:
            seq = f.frame_ref["frame_sequence"]
            got = lv.read_index(f.image)
            with self.lock:
                self.seen.append((f.camera_id, seq, got))
                if self.index_of(f.camera_id, seq) != got:
                    self.misbound += 1
            out.append(CameraDetections(f.camera_id, f.frame_ref["stream_epoch"], seq,
                                        f.estimated_capture_time + self.shift_time_ms, f.timestamp_source,
                                        f.timestamp_uncertainty_ms, (PoseDetection((10.0, 10.0, 50.0, 90.0), 0.9, KP17),)))
        return out


class OneTrackBackend:
    def __init__(self, _config) -> None:
        self._last: set[int] = set()

    def update(self, boxes, scores, features=None):
        out = [(i + 1, i) for i in range(len(boxes))]
        self._last = {t for t, _ in out}
        return out

    def live_track_ids(self):
        return frozenset(self._last), frozenset()


@pytest.fixture(scope="module")
def video():
    return lv.encode(list(range(1000, 1060)), gop=10)


class Rig:
    def __init__(self, tmp: Path, video) -> None:
        cert, key = _make_cert(tmp)
        self.video = video
        self.clock = FixedClock(T0)
        self.ingest = I0Ingest(CAMS, lambda _c: PyAvDecoder(), self.clock,
                               FreshnessConfig(queue_depth=1, max_queue_wait_ms=150, reorder_window_ms=0))
        self.ring = PacketRingBuffer(CAMS)
        self.live = LiveInput(self.ingest, lambda consumer: vi.VideoIngestServer(
            [vi.ListenSpec(0, frozenset(CAMS), "127.0.0.1")], vi.server_context(str(cert), str(key)),
            vi.TokenVerifier(frozenset({vi.token_sha256(TOKEN)})), consumer, hello_timeout_s=2.0))
        self.live.bridge.attach_clips(self.ring.receive)
        self.index: dict[tuple[str, int], int] = {}
        self.detector = BarcodeDetector(lambda cam, seq: self.index.get((cam, seq)))
        self.feed = DetectorFeed(camera_ids=CAMS, tickets=self.ingest.next_batch, detector=self.detector,
                                 tracker=MultiCameraLocalTracker(camera_ids=CAMS, backend_factory=OneTrackBackend),
                                 connected=self.live.connected, on_close=self.live.stop)
        self.live.start()
        self.port = self.live.server.ports[0]

    def sender(self, cam: str) -> lv.VideoSender:
        return lv.VideoSender(self.port, cam, TOKEN)

    def send(self, s: lv.VideoSender, seqs, *, epoch=1, enc=None, index_base=1000, capture=None, keyframes=None):
        """frame_sequence s 로 enc.frames[i] 를 보낸다(i = s % len, 번호 index_base + i). 키프레임 여부는 영상 그대로."""
        enc = enc or self.video
        for seq in seqs:
            i = seq % len(enc.frames)
            au, key = enc.frames[i]
            if keyframes is not None:
                key = seq in keyframes
            self.index[(s.camera_id, seq)] = index_base + i
            t = T0 + 40 * seq if capture is None else capture(seq)
            s.frame(lv.frame_meta(s.camera_id, epoch, seq, t, key), au)

    def received(self, cam: str) -> int:
        return self.live.frame_totals()[cam]["received"]

    def decoded(self, cam: str) -> int:
        """디코딩을 마치고 대기열 처리까지 끝난 프레임 수(poll 전에 이것으로 기다린다. 대기열에 넣는 중이면 덜 센다)."""
        c = self.ingest.log.counts(cam)
        r = c["skipped_by_reason"]
        placed = (self.ingest.scheduler.pending(cam) + c["inferred"] + r.get("SUPERSEDED", 0)
                  + r.get("OUT_OF_ORDER", 0) + r.get("QUEUE_WAIT_EXCEEDED", 0) + r.get("STREAM_RESET", 0))
        return min(c["decoded"], placed)

    def health(self, consumer: str = "i1") -> dict:
        payload = {"cameras": [{"id": c, "fps": 0.0, "last_capture": None, "sync_offset_ms": None,
                                "status": "OK" if self.live.connected(c) else "NO_SIGNAL"} for c in CAMS]}
        return {e["id"]: e for e in self.live.merge_health(payload, consumer)["cameras"]}

    def close(self) -> None:
        self.feed.close()
        assert self.live.stop(5.0) == []  # 수신 스레드가 모두 끝났다(다음 시험에 남지 않는다)


@pytest.fixture
def rig(tmp_path, video):
    r = Rig(tmp_path, video)
    yield r
    r.close()


def test_start_without_keyframe_waits_reports_waiting_and_then_binds_correctly(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, range(3, 10))  # GOP 중간부터: 키프레임 없음
    wait_until(lambda: rig.received("cam_A") == 7)
    assert rig.live.video_connected("cam_A") and not rig.live.connected("cam_A")
    assert rig.feed.poll(T0) == []
    h = rig.health()["cam_A"]
    assert h["status"] == "WAITING_KEYFRAME" and h["stream_epoch"] == 1
    assert h["frames"] == {"received": 7, "gateway_to_runpod_loss": 0, "skipped_by_policy": 7, "laptop_skipped": 0,
                           "unattributed_gap": 0, "late_dropped": 0}  # 새 epoch 첫 번호 앞은 손실로 세지 않는다
    assert rig.ring.camera("cam_A").stats.accepted == 0
    rig.send(s, range(10, 12))  # 키프레임(10)부터
    wait_until(lambda: rig.decoded("cam_A") == 2)
    [obs] = rig.feed.poll(T0)
    assert (obs.camera_id, obs.frame_sequence, obs.estimated_capture_time_ms) == ("cam_A", 11, T0 + 440)
    assert rig.detector.misbound == 0 and rig.detector.seen == [("cam_A", 11, 1011)]
    h = rig.health()["cam_A"]
    assert h["status"] == "OK" and h["frames"]["received"] == 2
    assert h["frames"]["skipped_by_policy"] == 1  # 10 은 11 에 밀림(latest-frame-wins)
    assert rig.ring.camera("cam_A").stats.accepted == 2  # 링 버퍼는 키프레임부터 압축 그대로
    s.close()


def test_codec_change_over_tls_never_shows_old_codec_frame_with_new_time(rig) -> None:
    other = lv.encode(list(range(2000, 2020)), gop=10, size=(320, 176))
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, range(0, 5))
    wait_until(lambda: rig.decoded("cam_A") == 5)
    s.codec(lv.codec_meta("cam_A", 1, other))  # 같은 epoch, 해상도 변경
    rig.send(s, [5, 6], enc=other, index_base=2000, keyframes=set())  # 새 코덱 키프레임 전
    wait_until(lambda: rig.received("cam_A") == 7)
    assert rig.feed.poll(T0) == [] and not rig.live.connected("cam_A")
    au, key = other.frames[10]
    assert key
    rig.index[("cam_A", 7)] = 2010
    s.frame(lv.frame_meta("cam_A", 1, 7, T0 + 280, True), au)
    wait_until(lambda: rig.live.connected("cam_A"))
    [obs] = rig.feed.poll(T0)
    assert obs.frame_sequence == 7 and obs.estimated_capture_time_ms == T0 + 280
    assert rig.detector.seen[-1] == ("cam_A", 7, 2010) and rig.detector.misbound == 0
    reasons = rig.ingest.log.counts("cam_A")["skipped_by_reason"]
    assert reasons.get("STREAM_RESET", 0) >= 1
    s.close()


def test_one_camera_drop_keeps_other_and_same_epoch_reconnect_counts_loss(rig) -> None:
    a, b = rig.sender("cam_A"), rig.sender("cam_B")
    for s in (a, b):
        s.codec(lv.codec_meta(s.camera_id, 1, rig.video))
        rig.send(s, range(0, 3))
    wait_until(lambda: rig.decoded("cam_A") == 3 and rig.decoded("cam_B") == 3)
    assert {o.camera_id for o in rig.feed.poll(T0)} == {"cam_A", "cam_B"}
    b.close()  # cam_B 끊김
    wait_until(lambda: not rig.live.video_connected("cam_B"))
    assert not rig.live.connected("cam_B")
    h = rig.health()
    assert h["cam_B"]["status"] == "NO_SIGNAL"  # 끊김은 키프레임 대기가 아니다
    rig.send(a, range(3, 6))
    wait_until(lambda: rig.decoded("cam_A") == 6)
    assert [o.camera_id for o in rig.feed.poll(T0)] == ["cam_A"]  # 한 카메라 끊김이 다른 카메라를 막지 않는다
    # 같은 epoch 전송 재접속: 번호를 이어 쓰고, 끊긴 동안 빠진 3~9 는 손실, 키프레임 전 11~... 없이 10 부터
    b2 = rig.sender("cam_B")
    b2.codec(lv.codec_meta("cam_B", 1, rig.video))
    rig.send(b2, [10, 11])
    wait_until(lambda: rig.decoded("cam_B") == 5)
    [obs] = rig.feed.poll(T0)
    assert (obs.camera_id, obs.frame_sequence) == ("cam_B", 11) and rig.detector.misbound == 0
    hb = rig.health()["cam_B"]
    assert hb["frames"]["gateway_to_runpod_loss"] == 7 and hb["status"] == "OK"
    for s in (a, b2):
        s.close()


def test_detector_exception_drops_that_batch_and_next_batch_works(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, [0])
    wait_until(lambda: rig.decoded("cam_A") == 1)
    rig.detector.raise_next = 1
    assert rig.feed.poll(T0) == []
    assert rig.feed.stats.detector_errors == 1 and rig.feed.stats.frames_lost_to_errors == 1
    assert rig.feed.poll(T0) == []  # 버린 묶음을 다시 내지 않는다
    rig.send(s, [1])
    wait_until(lambda: rig.decoded("cam_A") == 2)
    [obs] = rig.feed.poll(T0)
    assert obs.frame_sequence == 1
    s.close()


def test_detector_result_with_changed_time_or_duplicate_time_is_not_used(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, [0])
    wait_until(lambda: rig.decoded("cam_A") == 1)
    rig.detector.shift_time_ms = 1  # 검출기가 시각을 바꿨다(다른 프레임에 시각이 붙는 결함)
    assert rig.feed.poll(T0) == [] and rig.feed.stats.mismatched == 1
    rig.detector.shift_time_ms = 0
    rig.send(s, [1], capture=lambda seq: T0 + 40)
    wait_until(lambda: rig.decoded("cam_A") == 2)
    [obs] = rig.feed.poll(T0)
    rig.send(s, [2], capture=lambda seq: T0 + 40)  # 같은 촬영 시각이 또 왔다(시각 추정 충돌)
    wait_until(lambda: rig.decoded("cam_A") == 3)
    assert rig.feed.poll(T0) == [] and rig.feed.stats.stale_results == 1
    s.close()


def test_backlog_keeps_only_latest_and_queue_wait_cap_is_reported(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, range(0, 10))  # 추론이 밀린 동안 10장
    wait_until(lambda: rig.ingest.log.counts("cam_A")["decoded"] == 10)
    [obs] = rig.feed.poll(T0)
    assert obs.frame_sequence == 9
    assert rig.health()["cam_A"]["frames"]["skipped_by_policy"] == 9
    rig.send(s, range(10, 13))
    wait_until(lambda: rig.ingest.log.counts("cam_A")["decoded"] == 13)
    rig.clock.advance(151)  # 대기 상한(150 ms) 초과
    assert rig.feed.poll(T0 + 151) == []
    reasons = rig.ingest.log.counts("cam_A")["skipped_by_reason"]
    assert reasons["QUEUE_WAIT_EXCEEDED"] == 1 and reasons["SUPERSEDED"] == 11
    assert rig.health()["cam_A"]["frames"]["skipped_by_policy"] == 3
    s.close()


def test_frame_without_capture_time_header_never_reaches_decoder_or_ring(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    meta = lv.frame_meta("cam_A", 1, 0, T0, True)
    del meta["estimated_capture_time_ms"]
    s.raw(vi.FRAME, meta, rig.video.frames[0][0])
    wait_until(lambda: rig.live.server.streams["cam_A"].stats.protocol_errors == 1)
    wait_until(lambda: not rig.live.video_connected("cam_A"))
    assert rig.ingest.log.counts("cam_A")["received"] == 0 and rig.ring.camera("cam_A").stats.accepted == 0
    assert rig.health()["cam_A"]["frames"]["received"] == 0  # 계약 위반 기록은 받은 프레임으로 세지 않는다
    assert rig.feed.poll(T0) == []
    s.close()


def test_health_frame_reports_are_per_consumer(rig) -> None:
    s = rig.sender("cam_A")
    s.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(s, range(0, 4))
    wait_until(lambda: rig.received("cam_A") == 4)
    assert rig.health("i1")["cam_A"]["frames"]["received"] == 4
    assert rig.health("laptop")["cam_A"]["frames"]["received"] == 4  # 다른 수신자의 보고가 구간을 먹지 않는다
    assert rig.health("i1")["cam_A"]["frames"]["received"] == 0
    s.close()


# ---------------------------------------------------------------------------- 리뷰(F2·F3·F4) 재현 회귀


def test_same_epoch_reconnect_is_not_observable_until_new_codec_and_keyframe(rig) -> None:
    """F2: 같은 epoch TLS 재접속 뒤 codec_info·키프레임 전에는 관측 가능이 아니고, 이전 연결의 대기 프레임도 내지 않는다."""
    a = rig.sender("cam_A")
    a.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(a, [0])  # 키프레임 0: 디코딩해 대기열에 남는다(추론 전)
    wait_until(lambda: rig.decoded("cam_A") == 1)
    assert rig.ingest.scheduler.pending("cam_A") == 1 and rig.live.connected("cam_A")
    a.close()
    wait_until(lambda: not rig.live.video_connected("cam_A"))
    b = rig.sender("cam_A")
    wait_until(lambda: rig.live.video_connected("cam_A"))
    assert rig.feed.poll(T0) == []  # 이전 연결의 대기 프레임을 내지 않는다
    assert not rig.live.connected("cam_A") and rig.health()["cam_A"]["status"] == "WAITING_KEYFRAME"
    b.codec(lv.codec_meta("cam_A", 1, rig.video))  # 같은 codec_info
    rig.send(b, [1, 2])  # 비키프레임만
    wait_until(lambda: rig.received("cam_A") == 3)
    assert not rig.live.connected("cam_A") and rig.health()["cam_A"]["status"] == "WAITING_KEYFRAME"
    assert rig.feed.poll(T0) == []
    rig.send(b, [10])  # 키프레임: 번호는 이어 센다(3~9 손실)
    wait_until(lambda: rig.live.connected("cam_A"))
    [obs] = rig.feed.poll(T0)
    assert obs.frame_sequence == 10 and rig.detector.misbound == 0
    reasons = rig.ingest.log.counts("cam_A")["skipped_by_reason"]
    assert reasons.get("STREAM_RESET", 0) == 1  # 끊길 때 대기열에 있던 0
    assert rig.health()["cam_A"]["frames"]["gateway_to_runpod_loss"] == 7
    b.close()


def test_rejected_stale_codec_info_is_not_counted_as_frame(rig) -> None:
    """F4: 낮은 epoch 의 codec_info 만 거절한 구간은 health 프레임 집계(received·skipped_by_policy)를 늘리지 않는다."""
    a = rig.sender("cam_A")
    a.codec(lv.codec_meta("cam_A", 2, rig.video))
    rig.send(a, [0], epoch=2)
    wait_until(lambda: rig.decoded("cam_A") == 1)
    a.close()
    wait_until(lambda: not rig.live.video_connected("cam_A"))
    before = rig.live.frame_totals()["cam_A"]  # 끊길 때 대기열의 0 은 STREAM_RESET(정책 버림 1)
    b = rig.sender("cam_A")
    b.codec(lv.codec_meta("cam_A", 1, rig.video))
    wait_until(lambda: rig.live.server.streams["cam_A"].stats.stream_rejects == 1)
    after = rig.live.frame_totals()["cam_A"]
    assert after == before == {**before, "received": 1, "skipped_by_policy": 1}
    # 낮은 epoch FRAME 은 여전히 받은·정책 버림으로 센다
    rig.live.server.streams["cam_A"].on_frame(lv.frame_meta("cam_A", 1, 5, T0, False))
    assert rig.live.frame_totals()["cam_A"]["received"] == 2
    b.close()


def test_partial_port_start_failure_closes_already_opened_listeners(tmp_path) -> None:
    """F3: 두 번째 포트 bind 가 실패하면 먼저 연 포트·accept 스레드를 닫는다. stop 은 부분 기동에서도 안전하다."""
    import socket

    cert, key = _make_cert(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        for _attempt in range(5):  # 이 Windows 환경은 가끔 첫 포트 bind 도 WinError 10014 로 실패한다: 첫 포트가 열린 경우만 본다
            ingest = I0Ingest(CAMS, lambda _c: PyAvDecoder(), FixedClock(T0))
            live = LiveInput(ingest, lambda consumer: vi.VideoIngestServer(
                [vi.ListenSpec(0, frozenset({"cam_A"}), "127.0.0.1"),
                 vi.ListenSpec(occupied.getsockname()[1], frozenset({"cam_B"}), "127.0.0.1")],
                vi.server_context(str(cert), str(key)), vi.TokenVerifier(frozenset({vi.token_sha256(TOKEN)})),
                consumer))
            with pytest.raises(OSError):
                live.start()
            if live.server.ports:
                break
        assert not live.started and len(live.server.ports) == 1
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", live.server.ports[0])) != 0  # 먼저 연 리스너가 닫혔다
        assert live.stop(3.0) == []  # 남은 수신 스레드 없음(다시 불러도 안전)
        assert live.stop(1.0) == []


def test_codec_info_resent_on_same_connection_waits_for_keyframe_on_the_wire(rig) -> None:
    """같은 연결에서 같은 codec_info 가 다시 오면 video_ingest 는 키프레임 전 프레임을 버린다. 디코더 쪽이 준비돼 있어도 그동안은
    관측 가능이 아니다(WAITING_KEYFRAME). 키프레임이 오면 다시 관측한다."""
    a = rig.sender("cam_A")
    a.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(a, [0, 1])
    wait_until(lambda: rig.decoded("cam_A") == 2)
    assert rig.live.connected("cam_A") and len(rig.feed.poll(T0)) == 1
    a.codec(lv.codec_meta("cam_A", 1, rig.video))  # 같은 codec_info 다시(노트북 송신 재시작 등)
    rig.send(a, [2, 3])  # 키프레임 전: video_ingest 가 버린다
    wait_until(lambda: rig.received("cam_A") == 4)
    assert rig.ingest.camera("cam_A").ready  # 디코더 쪽은 그대로 준비 상태
    assert not rig.live.connected("cam_A") and rig.health()["cam_A"]["status"] == "WAITING_KEYFRAME"
    rig.send(a, [10])
    wait_until(lambda: rig.live.connected("cam_A"))
    [obs] = rig.feed.poll(T0)
    assert obs.frame_sequence == 10 and rig.detector.misbound == 0
    a.close()


def test_stop_joins_accept_and_open_connection_threads(rig) -> None:
    """종료: 열린 연결이 있어도 stop(timeout) 이 accept·연결 스레드가 끝날 때까지 기다리고, 남은 스레드가 없다."""
    a = rig.sender("cam_A")
    a.codec(lv.codec_meta("cam_A", 1, rig.video))
    rig.send(a, [0])
    wait_until(lambda: rig.decoded("cam_A") == 1)
    assert len(rig.live.server.alive_threads()) >= 2  # accept + 연결
    assert rig.live.stop(5.0) == []
    assert rig.live.server.alive_threads() == []
    a.close()
