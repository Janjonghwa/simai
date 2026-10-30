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
from live_video import codec_meta, encode, frame_meta, read_index  # noqa: E402

from services.tier0.clips.ring_buffer import PacketRingBuffer  # noqa: E402
from services.tier0.clips.upload import SignedUpload  # noqa: E402
from services.tier0.event_frames import EventFrameService, EventFrameSigner, decode_selected, selected_packets  # noqa: E402
from services.tier0.i1_client import Envelope, I1Client, QueueFull  # noqa: E402
from services.tier0.i1_publisher import I1Publisher  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.io.messages import codec_info_from_contract, frame_from_contract  # noqa: E402
from simai_ai.io.pyav_decoder import PyAvDecoder  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
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
    ring = PacketRingBuffer(("cam_A", "cam_B", "cam_C"))
    encoded = encode(list(range(121)))
    for camera in ring.camera_ids:
        ring.receive(codec_info_from_contract(codec_meta(camera, 0, encoded)))
        for sequence, (data, keyframe) in enumerate(encoded.frames):
            header = frame_meta(camera, 0, sequence, T0 + sequence * 40, keyframe,
                                source="RECEIVE_ESTIMATED", uncertainty=17.5)
            ring.receive(frame_from_contract(header, data))
    return ring


class Signer:
    def __init__(self):
        self.calls = []

    def sign(self, request):
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
                                decoder_factory=PyAvDecoder, **kwargs)
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
    for frame, (_, content, content_type), request in zip(frames, transport.puts, signer.calls):
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
    for buffer, transport in ((PacketRingBuffer(ring.camera_ids), Transport()), (ring, Transport(fail=True))):
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
    cut = ring.camera("cam_A").cut(T0, T0 + 4800)
    selected = selected_packets(cut, ANCHOR + 2000)
    assert [p.capture_time_ms for p in selected] == [ANCHOR - 1000]  # 현재는 1.2초 전이라 버림
    selected = tuple(replace(p, timestamp_uncertainty_ms=None) for p in selected_packets(cut, ANCHOR))
    pictures = decode_selected(cut, selected, PyAvDecoder)
    assert len(pictures) == 2 and all(p.timestamp_uncertainty_ms is None for p, _ in pictures)


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
