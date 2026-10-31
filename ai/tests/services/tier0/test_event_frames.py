"""합성 H.264 → JPEG → 서명 업로드 → 실제 I1 신뢰 큐. 사람 사진·네트워크 없음."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

av = pytest.importorskip("av")
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from clip_stream import T0, transition  # noqa: E402
from live_video import codec_meta, encode, read_index  # noqa: E402

from services.tier0.clips.upload import SignedUpload  # noqa: E402
from services.tier0.event_frames import EventFrameService, EventFrameSigner  # noqa: E402
from services.tier0.event_frame_buffer import EventFrameBuffer, MAX_FRAME_BYTES, MAX_HISTORY, MAX_CURRENT  # noqa: E402
from services.tier0.i1_client import Envelope, I1Client, QueueFull  # noqa: E402
from services.tier0.i1_publisher import I1Publisher  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.io.messages import codec_info_from_contract  # noqa: E402
from simai_ai.io.pyav_decoder import PyAvDecoder  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.risk import FixedClock, Origin, Severity  # noqa: E402
from simai_ai.risk.lifecycle import Change, Lifecycle  # noqa: E402

ANCHOR = T0 + 4_000


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture
def ring():
    ring = EventFrameBuffer(("cam_A", "cam_B", "cam_C"))
    encoded = encode(list(range(101)))
    for camera in ring.camera_ids:
        decoder = PyAvDecoder()
        decoder.configure(codec_info_from_contract(codec_meta(camera, 0, encoded)))
        pictures = []
        for sequence, (data, keyframe) in enumerate(encoded.frames):
            pictures.extend(decoder.decode(data, (0, sequence), keyframe))
        pictures.extend(decoder.flush())
        decoder.reset()
        for picture in pictures:
            sequence = picture.key[1]
            ring.offer(Frame(camera, T0 + sequence * 40, "RECEIVE_ESTIMATED",
                             {"stream_epoch": 0, "frame_sequence": sequence}, 17.5, picture.image))
    return ring


class Signer:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def sign(self, request):
        with self.lock:
            self.calls.append(request)
            key = f"event-frames/{request.event_id}/{len(self.calls)}.jpg"
        return SignedUpload("https://objects.example/put?secret=hidden", key, ANCHOR + 10_000,
                            object_uri=f"s3://frames/{key}")


class Transport:
    def __init__(self, *, fail=False, block=None):
        self.fail, self.block = fail, block
        self.entered = threading.Event()
        self.puts = []

    def put(self, signed, content, content_type):
        self.entered.set()
        if self.block is not None:
            assert self.block.wait(5)
        if self.fail:
            raise OSError("https://objects.example/put?secret=hidden")
        self.puts.append((signed, content, content_type))


def service_for(ring, *, transport=None, send=None, **kwargs):
    messages = []
    clock = FixedClock(ANCHOR + 200)
    signer = Signer()
    transport = transport or Transport()
    service = EventFrameService(ring, clock, signer=signer, transport=transport,
                                send_event=send or (lambda kind, payload: messages.append((kind, payload))),
                                **kwargs)
    return service, messages, signer, transport


def raised(**kwargs):
    return transition(observation_time=ANCHOR, **({"severity": Severity.CRITICAL} | kwargs))


def test_critical_event_uploads_four_past_frames_once_to_reliable_i1(ring, caplog):
    caplog.set_level(logging.INFO)
    clock = FixedClock(ANCHOR + 200)
    client = I1Client(lambda: None, on_message=lambda _: None)
    publisher = I1Publisher(client, Envelope("tier0-01", clock), health_payload=lambda: None)
    sent = []

    def send(kind, payload):
        sent.append(publisher.send_event(kind, payload))

    service, _, signer, transport = service_for(ring, send=send)
    tracks = [SimpleNamespace(world_id="w-1", cameras=("cam_B", "cam_C"))]
    service.start()
    try:
        first = raised()
        service.on_transitions([first, first], tracks)
        service.on_transitions([raised(lifecycle=Lifecycle.UPDATED, changes=(Change.METRICS,))], tracks)
        service.on_transitions([raised(lifecycle=Lifecycle.UPDATED, changes=(Change.RESUMED,), resume_count=1)], tracks)
        assert wait_until(lambda: len(sent) == 1)
    finally:
        assert service.stop(5)
    assert len(sent) == 1 and len(transport.puts) == 4
    message = sent[0]
    assert message["schema_version"] == "1.0.0"
    assert schema_errors(message, "event_frame_ready") == []
    frames = message["payload"]["frames"]
    assert {f["camera_id"] for f in frames} == {"cam_B", "cam_C"}
    assert {f["estimated_capture_time_ms"] for f in frames} == {ANCHOR, ANCHOR - 3000}
    for frame in frames:
        signed, content, content_type = next(p for p in transport.puts if p[0].object_uri == frame["frame_uri"])
        request = next(r for r in signer.calls if r.sha256 == hashlib.sha256(content).hexdigest()
                       and r.camera_id == frame["camera_id"])
        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert read_index(image) == (frame["estimated_capture_time_ms"] - T0) // 40
        assert content_type == "image/jpeg" and request.purpose == "EVENT_FRAME"
        assert request.sha256 == hashlib.sha256(content).hexdigest()
        assert frame["timestamp_source"] == "RECEIVE_ESTIMATED" and frame["timestamp_uncertainty_ms"] == 17.5
        assert frame["frame_uri"].startswith("s3://")
    assert "event_to_upload_ms=50" in caplog.text
    assert "secret=hidden" not in caplog.text
    # 연결 전에도 reliable 에 보관한다. telemetry/latest 로 가지 않는다.
    assert client.queue_sizes()["reliable"] == 1


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY])
def test_non_live_never_uploads(ring, origin):
    service, sent, signer, _ = service_for(ring)
    service.on_transitions([raised(origin=origin)])
    assert service._queue.empty() and not signer.calls and not sent


@pytest.mark.parametrize("severity", [Severity.HIGH, Severity.CAUTION, Severity.INFO])
def test_non_critical_never_uploads(ring, severity):
    service, sent, signer, _ = service_for(ring)
    service.on_transitions([raised(severity=severity)])
    assert service._queue.empty() and not signer.calls and not sent


def test_only_r1_through_r4_initial_raised_and_disable(ring):
    service, sent, signer, _ = service_for(ring, enabled=False)
    service.start()
    service.on_transitions([raised()])
    assert service.stop(1) and not sent and not signer.calls
    service, _, _, _ = service_for(ring)
    first = raised()
    r5 = replace(first, state=replace(first.state, key=replace(first.state.key, rule_id="R5")))
    service.on_transitions([r5, raised(lifecycle=Lifecycle.UPDATED, changes=(Change.RESTORED,))])
    assert service._queue.empty()


def test_empty_ring_and_upload_failure_only_log(ring, caplog):
    for buffer, transport in ((EventFrameBuffer(ring.camera_ids), Transport()), (ring, Transport(fail=True))):
        service, sent, _, _ = service_for(buffer, transport=transport)
        service.start()
        try:
            service.on_transitions([raised()])
            assert wait_until(lambda: "reason=no_frames" in caplog.text)
        finally:
            assert service.stop(5)
        assert not sent
        caplog.clear()


def test_blocked_upload_cannot_hold_judgment_and_queue_is_bounded(ring, caplog):
    release = threading.Event()
    transport = Transport(block=release)
    service, sent, _, _ = service_for(ring, transport=transport, queue_depth=1)
    service.start()
    try:
        service.on_transitions([raised()])
        assert transport.entered.wait(5)
        producer_finished = threading.Event()

        def produce():
            for n in (2, 3):
                service.on_transitions([raised(event_id=f"0192a0a0-0000-7000-8000-{n:012d}")])
            producer_finished.set()

        producer = threading.Thread(target=produce)
        producer.start()
        assert producer_finished.wait(1)  # 업로드가 멈춰 있어도 판정 쪽은 돌아온다
        producer.join(1)
        assert service._queue.qsize() == 1 and "reason=queue_full" in caplog.text
    finally:
        release.set()
        assert service.stop(5)


def test_i1_full_retries_message_without_uploading_again(ring):
    attempted = threading.Event()
    allow = threading.Event()
    sent = []

    def send(kind, payload):
        attempted.set()
        if not allow.is_set():
            raise QueueFull("full")
        sent.append(payload)

    service, _, _, transport = service_for(ring, send=send)
    service.start()
    try:
        service.on_transitions([raised()])
        assert attempted.wait(5)
        assert len(transport.puts) == 4
        allow.set()
        assert wait_until(lambda: len(sent) == 1)
        assert len(transport.puts) == 4
    finally:
        assert service.stop(5)


def test_no_future_or_stale_frame_substitution_and_unknown_uncertainty(ring):
    selected = ring.snapshot(("cam_A",), ANCHOR + 2000, 2)
    assert [p.capture_time_ms for p in selected] == [ANCHOR - 1000]
    selected = ring.snapshot(("cam_A",), ANCHOR, 2)
    assert [p.capture_time_ms for p in selected] == [ANCHOR, ANCHOR - 3000]
    frame = Frame("cam_A", ANCHOR + 840, "RECEIVE_ESTIMATED",
                  {"stream_epoch": 0, "frame_sequence": 121}, None, selected[0].image)
    ring.offer(frame)
    assert ring.snapshot(("cam_A",), frame.estimated_capture_time, 2)[0].timestamp_uncertainty_ms is None


def test_settings_default_on_and_explicit_off(tmp_path):
    config = tmp_path / "tier0.json"
    doc = {"input": {"mode": "sim", "run_dir": "run"}, "state_dir": str(tmp_path / "state")}
    config.write_text(json.dumps(doc), encoding="utf-8")
    assert load_settings(config, env={}).event_frames_enabled is True
    doc["event_frames_enabled"] = False
    config.write_text(json.dumps(doc), encoding="utf-8")
    assert load_settings(config, env={}).event_frames_enabled is False
    doc["event_frames_enabled"] = "false"
    config.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Tier0ConfigError, match="event_frames_enabled"):
        load_settings(config, env={})


def test_signer_uses_server_request_and_persistent_object_uri():
    signer = EventFrameSigner("https://ec2.example", "synthetic-token")
    first = raised()
    key = f"event-frames/{first.state.event_id}/test.jpg"
    result = {"upload_url": "https://objects.example/put?sig=fake", "object_key": key,
              "object_uri": f"s3://bucket/{key}", "expires_at_ms": ANCHOR + 10_000,
              "headers": {"Content-Type": "image/jpeg"}, "method": "PUT"}
    posted = []

    def open_request(request, timeout):
        posted.append(json.loads(request.data))
        response = io.BytesIO(json.dumps(result).encode())
        response.status = 200
        return response

    signer._opener = SimpleNamespace(open=open_request)
    request = SimpleNamespace(event_id=first.state.event_id, content_type="image/jpeg", size_bytes=10, sha256="a" * 64)
    assert signer.sign(request).object_uri == result["object_uri"]
    assert posted == [{"purpose": "EVENT_FRAME", "event_id": first.state.event_id, "content_type": "image/jpeg",
                       "size_bytes": 10, "sha256": "a" * 64}]
    result["object_uri"] = result["upload_url"]
    with pytest.raises(ValueError):
        signer.sign(request)


def test_event_frames_slow_puts_overlap_and_ready_before_serial_budget(ring, monkeypatch):
    def unexpected_decode(*_args, **_kwargs):
        pytest.fail("이벤트 처리에서 이미 디코딩한 영상을 다시 디코딩했다")

    monkeypatch.setattr(PyAvDecoder, "decode", unexpected_decode)
    monkeypatch.setattr(PyAvDecoder, "flush", unexpected_decode)
    class SlowTransport(Transport):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.active = self.peak = 0

        def put(self, signed, content, content_type):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.15)
                super().put(signed, content, content_type)
            finally:
                with self.lock:
                    self.active -= 1

    transport = SlowTransport()
    service, sent, _, _ = service_for(ring, transport=transport)
    service.start()
    started = time.monotonic()
    try:
        service.on_transitions([raised()])
        assert wait_until(lambda: len(sent) == 1)
        elapsed = time.monotonic() - started
        assert transport.peak == 4
        assert elapsed < 0.5  # 직렬 PUT만 0.6s. 디코딩·JPEG·준비 통지도 포함한다.
        print(f"event_frames_slow_put peak={transport.peak} ready_ms={elapsed * 1000:.1f} serial_put_ms=600")
    finally:
        assert service.stop(5)


def test_event_frames_buffer_memory_is_bounded_and_references_are_not_copied():
    cameras = ("cam_A", "cam_B", "cam_C", "cam_D")
    ring = EventFrameBuffer(cameras)
    # 시험 배열은 작게 두되 서로 다른 실제 ndarray를 넣어 참조 수·바이트를 검증한다.
    for n in range(1000):
        for camera in cameras:
            image = np.full((12, 16, 3), n % 256, dtype=np.uint8)
            frame = Frame(camera, T0 + n * 40, "RECEIVE_ESTIMATED",
                          {"stream_epoch": 0, "frame_sequence": n}, 7.5, image)
            assert ring.offer(frame)
            assert ring._current[camera][-1].image is image
    held = [p for c in cameras for p in (*ring._history[c], *ring._current[c])]
    assert len(held) <= len(cameras) * (MAX_HISTORY + MAX_CURRENT)
    assert sum(p.image.nbytes for p in held) <= len(cameras) * (MAX_HISTORY + MAX_CURRENT) * image.nbytes
    oversized = replace(frame, image=SimpleNamespace(nbytes=MAX_FRAME_BYTES + 1))
    assert not ring.offer(oversized)
    assert not ring.offer(replace(frame, camera_id="unknown"))
    ring.clear()
    assert ring.snapshot(cameras, frame.estimated_capture_time, 2) == ()


def test_event_frames_current_window_epoch_and_frozen_queue_snapshot(ring):
    service, _, _, _ = service_for(ring)
    service.on_transitions([raised()])
    frozen = service._queue.get_nowait()
    original = tuple((p.capture_time_ms, p.image) for p in frozen.pictures)
    for n in range(1, 7):
        ring.offer(Frame("cam_A", ANCHOR + n * 100, "RECEIVE_ESTIMATED",
                         {"stream_epoch": 0, "frame_sequence": 100 + n}, None, original[0][1]))
    selected = ring.snapshot(("cam_A",), ANCHOR + 350, 2)
    assert selected[0].capture_time_ms == ANCHOR + 300  # 최신 600ms는 미래라 버리고 300ms를 쓴다.
    assert all(p.capture_time_ms <= ANCHOR + 350 for p in selected)
    ring.offer(Frame("cam_A", ANCHOR + 700, "RECEIVE_ESTIMATED",
                     {"stream_epoch": 1, "frame_sequence": 0}, None, original[0][1]))
    assert [p.capture_time_ms for p in ring.snapshot(("cam_A",), ANCHOR + 700, 2)] == [ANCHOR + 700]
    assert not ring.offer(Frame("cam_A", ANCHOR + 800, "RECEIVE_ESTIMATED",
                                {"stream_epoch": 0, "frame_sequence": 200}, None, original[0][1]))
    assert all(p.capture_time_ms == at and p.image is image for p, (at, image) in zip(frozen.pictures, original))


def test_event_frames_partial_upload_failure_keeps_successes(ring, caplog):
    class PartialTransport(Transport):
        def put(self, signed, content, content_type):
            if read_index(cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)) == 100:
                raise TimeoutError("synthetic")
            super().put(signed, content, content_type)

    service, sent, _, _ = service_for(ring, transport=PartialTransport())
    service.start()
    try:
        service.on_transitions([raised()])
        assert wait_until(lambda: bool(sent))
        assert len(sent[0][1]["frames"]) == 2
        assert "event_frames_upload_failed" in caplog.text
    finally:
        assert service.stop(5)


def test_event_frames_feed_shares_matched_frames_with_background():
    from services.tier0.perception import DetectorFeed

    image = np.zeros((12, 16, 3), dtype=np.uint8)
    frame = Frame("cam_A", ANCHOR, "RECEIVE_ESTIMATED",
                  {"stream_epoch": 0, "frame_sequence": 1}, None, image)
    result = SimpleNamespace(camera_id="cam_A", stream_epoch=0, frame_sequence=1,
                             estimated_capture_time_ms=ANCHOR)
    feed = DetectorFeed(camera_ids=("cam_A",), tickets=lambda: [frame],
                        detector=SimpleNamespace(detect=lambda _: [result]),
                        tracker=SimpleNamespace(update_batch=lambda _: []))
    background = []
    feed.observe_decoded = lambda *args: background.append(args)
    ring = EventFrameBuffer(("cam_A",))
    service, _, _, _ = service_for(ring)
    service.attach(feed)
    feed.poll(ANCHOR)
    assert len(background) == 1
    assert ring.snapshot(("cam_A",), ANCHOR, 2)[0].image is image
    feed.poll(ANCHOR)  # 검출 중복은 배경과 사진 양쪽에 전달하지 않는다.
    assert len(background) == 1
    result.frame_sequence = 2  # 잘못 결합한 결과도 전달하지 않는다.
    feed.poll(ANCHOR)
    assert len(background) == 1


def test_event_frames_stop_releases_waiting_images_and_reports_running_put(ring):
    release = threading.Event()
    service, _, _, transport = service_for(ring, transport=Transport(block=release))
    service.start()
    try:
        service.on_transitions([raised()])
        assert transport.entered.wait(2)
        for n in range(2, 12):
            service.on_transitions([raised(event_id=f"0192a0a0-0000-7000-8000-{n:012d}")])
        queued = list(service._queue.queue)
        assert len(queued) == service._queue.maxsize == 8
        assert all(len(r.pictures) <= 4 for r in queued)
        assert not service.stop(0.01)
        assert service._queue.empty()
        assert not ring.snapshot(ring.camera_ids, ANCHOR, 2)
    finally:
        release.set()
        assert service.stop(5)
