"""이벤트 클립 업로더 연결(S15P31A101-748): EVENT_CLIP 서명, 판정 루프를 막지 않는 업로드 스레드, 실패 재시도, I1 기록 출구,
단일 경로·프로세스 분리 경로(검출 프로세스 업로드 → 판정 프로세스 I1 중계). 설계 docs/design/S15P31A101-748-event-clip-upload.md."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from clip_stream import T0, Stream, transition  # noqa: E402

from services.tier0.clips.async_upload import (  # noqa: E402
    BackgroundClipUploader,
    ClipIo,
    EventClipSigner,
    I1ClipRecordSink,
)
from services.tier0.clips.media import ExportClip  # noqa: E402
from services.tier0.clips.ring_buffer import PacketRingBuffer  # noqa: E402
from services.tier0.clips.service import EventClipService  # noqa: E402
from services.tier0.clips.upload import (  # noqa: E402
    ClipUploader,
    RetryPolicy,
    SignedUpload,
    SignerUnavailable,
    UploadError,
    UploadSignRequest,
)
from services.tier0.i1_client import MESSAGE_SCHEMA_VERSIONS, RELIABLE_TYPES, UPSTREAM_TYPES, QueueFull  # noqa: E402
from services.tier0.i1_publisher import EVENT_TYPES  # noqa: E402
from services.tier0.split_side_work import ChildSideWork, RemoteClips, SideWorkSpec  # noqa: E402
from simai_ai.risk import FixedClock, Severity  # noqa: E402
from simai_ai.risk.lifecycle import Change, Lifecycle  # noqa: E402

EVENT = "0192a0a0-0000-7000-8000-000000000001"
CLIP = "0192a0a1-0000-7000-8000-000000000009"


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


# --------------------------------------------------------------------------- 서명 클라이언트


class _Resp:
    def __init__(self, body: dict, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Opener:
    def __init__(self, result) -> None:
        self.result = result
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _sign_request() -> UploadSignRequest:
    return UploadSignRequest("EVENT_CLIP", CLIP, EVENT, "cam_A", "video/h264", 1234, "sha256:" + "ab" * 32)


def _signed(key: str = f"event-clips/{EVENT}/{CLIP}.h264", **extra) -> dict:
    return {"object_key": key, "object_uri": f"s3://bucket/{key}", "method": "PUT",
            "upload_url": "https://bucket.example/put?sig=1", "headers": {"Content-Type": "video/h264"},
            "expires_at_ms": T0 + 600_000, **extra}


def test_signer_sends_event_clip_request_with_tier0_token_and_takes_server_key() -> None:
    opener = _Opener(_Resp(_signed()))
    signer = EventClipSigner("https://ec2.example", "tok-1", opener=opener)
    signed = signer.sign(_sign_request())
    req, timeout = opener.requests[0]
    assert req.full_url == "https://ec2.example/api/uploads/sign" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok-1" and timeout == 5.0
    assert json.loads(req.data) == {"purpose": "EVENT_CLIP", "event_id": EVENT, "clip_id": CLIP,
                                    "content_type": "video/h264", "size_bytes": 1234, "sha256": "ab" * 32}
    assert signed.object_key == f"event-clips/{EVENT}/{CLIP}.h264" and signed.headers == {"Content-Type": "video/h264"}
    assert "sig=1" not in repr(signed) and "tok-1" not in repr(signed)


def test_signer_refuses_a_key_for_another_clip_and_plain_http() -> None:
    with pytest.raises(ValueError):
        EventClipSigner("http://ec2.example", "tok")
    opener = _Opener(_Resp(_signed(key=f"event-clips/{EVENT}/other.h264")))
    with pytest.raises(UploadError) as e:
        EventClipSigner("https://ec2.example", "tok", opener=opener).sign(_sign_request())
    assert e.value.retryable is False


@pytest.mark.parametrize(("error", "kind", "retryable"), [
    (urllib.error.URLError("refused"), SignerUnavailable, None),
    (TimeoutError(), SignerUnavailable, None),
    (urllib.error.HTTPError("u", 502, "bad gateway", {}, io.BytesIO()), SignerUnavailable, None),
    (urllib.error.HTTPError("u", 409, "conflict", {}, io.BytesIO()), UploadError, False),
    (urllib.error.HTTPError("u", 401, "unauthorized", {}, io.BytesIO()), UploadError, False),
    (urllib.error.HTTPError("u", 500, "error", {}, io.BytesIO()), UploadError, True),
])
def test_signer_maps_ec2_outage_to_spool_and_rejections_to_upload_errors(error, kind, retryable) -> None:
    signer = EventClipSigner("https://ec2.example", "tok", opener=_Opener(error))
    with pytest.raises(kind) as e:
        signer.sign(_sign_request())
    if retryable is not None:
        assert e.value.retryable is retryable


# --------------------------------------------------------------------------- 업로드 스레드


class _Signer:
    def __init__(self) -> None:
        self.requests: list[UploadSignRequest] = []

    def sign(self, request: UploadSignRequest) -> SignedUpload:
        self.requests.append(request)
        key = f"event-clips/{request.event_id}/{request.clip_id}.h264"
        return SignedUpload(f"https://bucket.example/{key}?n={len(self.requests)}", key, T0 + 10**9, {})


class _Transport:
    def __init__(self, failures: int = 0, gate: threading.Event | None = None) -> None:
        self.failures = failures
        self.gate = gate
        self.puts: list[bytes] = []
        self.started = threading.Event()

    def put(self, signed, content, content_type) -> None:
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.failures > 0:
            self.failures -= 1
            raise UploadError("PUT 응답 503")
        self.puts.append(content)


class _Events:
    """I1Publisher.send_event 대역. full 이 남아 있으면 QueueFull."""

    def __init__(self, full: int = 0) -> None:
        self.full = full
        self.sent: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, msg_type: str, payload) -> dict:
        with self.lock:
            if self.full > 0:
                self.full -= 1
                raise QueueFull(msg_type)
            self.sent.append((msg_type, dict(payload)))
        return {}


def _draft_service(clock, uploader, cams=("cam_A",)):
    service = EventClipService(PacketRingBuffer(cams), clock, uploader=uploader)
    stream = Stream(cams)
    service.ring.receive_many(stream.start())
    return service, stream


def _run(service, clock, stream, ms: int) -> None:
    for _ in range(ms // 1_000):
        for f in stream.advance(1_000):
            service.receive(f)
        clock.set(stream.t_ms)
        service.poll()


def test_enqueue_returns_while_a_put_is_still_running() -> None:
    """판정 lock 안에서 부르는 poll·enqueue 가 PUT 을 기다리지 않는다(단일 경로 정리 주기는 판정 lock 을 잡고 clips.poll)."""
    clock = FixedClock(T0)
    gate = threading.Event()
    transport = _Transport(gate=gate)
    events = _Events()
    background = BackgroundClipUploader(ClipUploader(_Signer(), transport, I1ClipRecordSink(events), clock), poll_s=0.01)
    background.start()
    try:
        service, stream = _draft_service(clock, background, cams=("cam_A", "cam_B"))
        _run(service, clock, stream, 25_000)
        service.on_transitions([transition(observation_time=stream.t_ms)])
        started = time.monotonic()
        _run(service, clock, stream, 25_000)  # 사후 구간이 차 두 카메라를 잘라 넘긴다
        assert transport.started.wait(5)
        lock_free = time.monotonic() - started
        assert lock_free < 2.0 and events.sent == []  # PUT 이 막혀 있어도 넘기는 쪽은 바로 돌아왔다
        gate.set()
        assert _wait(lambda: len(events.sent) == 2)
    finally:
        gate.set()
        assert background.stop(2.0)
    kinds = {m for m, _ in events.sent}
    payloads = [p for _, p in events.sent]
    assert kinds == {"media_clip"} and sorted(p["camera_id"] for p in payloads) == ["cam_A", "cam_B"]
    assert all(p["object_key"] == f"event-clips/{p['event_id']}/{p['clip_id']}.h264" for p in payloads)
    assert all(p["source"] == "LIVE" and p["severity"] == "HIGH" and p["upload_attempts"] == 1 for p in payloads)


def test_failed_puts_are_retried_with_backoff_then_recorded_once() -> None:
    clock = FixedClock(T0)
    transport = _Transport(failures=2)
    signer = _Signer()
    events = _Events()
    background = BackgroundClipUploader(ClipUploader(signer, transport, I1ClipRecordSink(events), clock,
                                                     retry=RetryPolicy(initial_backoff_ms=1_000)))
    service, stream = _draft_service(clock, background)
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    background.run_once()  # 1번째 실패 → 1 s 뒤
    assert events.sent == [] and background.uploader.stats.failures == 1
    background.run_once()
    assert background.uploader.stats.failures == 1  # 백오프 동안 다시 시도하지 않는다
    clock.advance(1_000)
    background.run_once()  # 2번째 실패 → 2 s 뒤
    clock.advance(2_000)
    background.run_once()  # 성공
    assert len(transport.puts) == 1 and len(signer.requests) == 3  # 시도마다 새 서명 주소
    ((kind, payload),) = events.sent
    assert kind == "media_clip" and payload["upload_attempts"] == 3
    assert background.status()["uploaded"] == 1 and background.pending == 0


def test_full_i1_queue_re_emits_the_record_without_uploading_again() -> None:
    clock = FixedClock(T0)
    transport = _Transport()
    events = _Events(full=1)
    background = BackgroundClipUploader(ClipUploader(_Signer(), transport, I1ClipRecordSink(events), clock))
    service, stream = _draft_service(clock, background)
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    background.run_once()
    assert events.sent == [] and len(transport.puts) == 1
    clock.advance(1_000)
    background.run_once()
    assert [m for m, _ in events.sent] == ["media_clip"] and len(transport.puts) == 1


def test_critical_after_upload_sends_a_retention_record_through_the_thread() -> None:
    clock = FixedClock(T0)
    events = _Events()
    background = BackgroundClipUploader(ClipUploader(_Signer(), _Transport(), I1ClipRecordSink(events), clock))
    service, stream = _draft_service(clock, background)
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    background.run_once()
    service.on_transitions([transition(severity=Severity.CRITICAL, lifecycle=Lifecycle.UPDATED, changes=(Change.SEVERITY,),
                                       observation_time=stream.t_ms, event_sequence=2)])
    background.run_once()
    assert [m for m, _ in events.sent] == ["media_clip", "media_clip_retention"]
    assert events.sent[1][1]["retention_hold"] is True and events.sent[1][1]["reason"] == "CRITICAL_AUTO"


def test_inbox_cap_drops_instead_of_growing_memory() -> None:
    clock = FixedClock(T0)
    background = BackgroundClipUploader(ClipUploader(_Signer(), _Transport(), I1ClipRecordSink(_Events()), clock),
                                        max_inbox_bytes=100)
    service, stream = _draft_service(clock, background)
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)  # 클립 하나가 상한(100 B)보다 크다
    assert background.inbox_dropped == 1 and background.status()["inbox"] == 0
    with pytest.raises(TypeError):
        background.enqueue(b"raw", None)  # type: ignore[arg-type]


def test_i1_accepts_clip_records_on_the_reliable_queue() -> None:
    for kind in ("media_clip", "media_clip_retention"):
        assert kind in EVENT_TYPES and kind in UPSTREAM_TYPES and kind in RELIABLE_TYPES
        assert MESSAGE_SCHEMA_VERSIONS[kind] == "1.0.0"
    example = json.loads((AI_DIR.parent / "contracts" / "examples" / "media_clip.json").read_text(encoding="utf-8"))
    assert example["schema_version"] == MESSAGE_SCHEMA_VERSIONS["media_clip"]


# --------------------------------------------------------------------------- 프로세스 분리 경로


def _signer_factory():
    return _Signer()


def _transport_factory():
    return _Transport()


class _InProcess:
    """DetectorProcess 대역: 곁가지 명령은 같은 프로세스의 ChildSideWork 로, 자식이 out 큐에 넣은 곁가지 결과는 on_side 함수로."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.side: ChildSideWork | None = None
        self.handlers: dict = {}

    def put(self, item) -> bool:
        assert item[0] == "side"
        self.handlers[item[1]](item[2])
        return True

    def send_side(self, cmd, *, required=False) -> bool:
        assert self.side is not None
        self.side.handle(self.clock.now_ms(), *cmd)
        return True

    def snapshot(self):
        assert self.side is not None
        return self.side.status()

    def on_side(self, kind, handler) -> None:
        self.handlers[kind] = handler


def test_split_child_uploads_and_judge_side_relays_records_to_i1(tmp_path) -> None:
    cams = ("cam_A", "cam_B")
    clock = FixedClock(T0)
    process = _InProcess(clock)
    io_ = ClipIo("https://ec2.example", "tok", spool_dir=str(tmp_path / "clip-spool"),
                 signer_factory=_signer_factory, transport_factory=_transport_factory)
    side = ChildSideWork(SideWorkSpec(clips=True, clip_io=io_), cams, clock=clock, put=process.put,
                         put_blocking=lambda _m: None)
    process.side = side
    remote = RemoteClips(process)
    events = _Events(full=2)  # I1 신뢰 큐가 잠깐 차도 같은 기록을 붙든 채 기다린다
    remote.start_relay(events)
    try:
        assert remote.export_enabled and side.clips.export_enabled
        stream = Stream(cams)
        side.clips.ring.receive_many(stream.start())
        for i in range(50):
            for f in stream.advance(1_000):
                side.clips.receive(f)
            clock.set(stream.t_ms)
            if i == 25:
                remote.on_transitions([transition(severity=Severity.CRITICAL, observation_time=stream.t_ms)])
            remote.poll()
        assert _wait(lambda: len(events.sent) == 2)
    finally:
        assert remote.stop(2.0)
        side.close(1.0)
    payloads = [p for m, p in events.sent if m == "media_clip"]
    assert sorted(p["camera_id"] for p in payloads) == list(cams)
    assert all(p["retention_hold"] is True and p["severity"] == "CRITICAL" for p in payloads)  # CRITICAL 자동 보존
    status = remote.status()
    assert status["cut"] == 2 and status["upload"]["uploaded"] == 2 and status["relayed"] == 2
    assert not any((tmp_path / "clip-spool").glob("*"))  # 올린 클립은 디스크에 남지 않는다


def test_split_child_without_clip_io_keeps_counting_only() -> None:
    clock = FixedClock(T0)
    side = ChildSideWork(SideWorkSpec(clips=True), ("cam_A",), clock=clock, put=lambda _m: True,
                         put_blocking=lambda _m: None)
    assert side.clip_uploader is None and not side.clips.export_enabled
    assert "clip_upload" not in side.status()["side"]
    assert isinstance(ExportClip(b"x", "video/h264", "h264-annexb"), ExportClip)


# --------------------------------------------------------------------------- 조립(service.py)


def _settings(tmp_path, *, mode="live", enabled=True, i1=True):
    from types import SimpleNamespace

    return SimpleNamespace(event_clips_enabled=enabled, input_mode=mode, env={}, state_dir=tmp_path,
                           i1=SimpleNamespace(url="wss://ec2.example/ws/tier0", token="tok", ca_file=None) if i1 else None)


@pytest.mark.parametrize(("mode", "enabled", "i1"), [("file", True, True), ("live", False, True), ("live", True, False)])
def test_clip_io_only_for_live_input_with_i1(tmp_path, mode, enabled, i1) -> None:
    from types import SimpleNamespace

    from services.tier0.service import Tier0Service

    assert Tier0Service._clip_io(SimpleNamespace(settings=_settings(tmp_path, mode=mode, enabled=enabled, i1=i1))) is None
    io_ = Tier0Service._clip_io(SimpleNamespace(settings=_settings(tmp_path)))
    assert io_.base_url == "https://ec2.example" and io_.spool_dir == str(tmp_path / "clip-spool")
    assert "tok" not in repr(io_)


def test_service_attaches_uploader_on_single_path_and_relay_on_split_path(tmp_path) -> None:
    from types import SimpleNamespace

    from services.tier0.service import Tier0Service

    clock = FixedClock(T0)
    events = _Events()
    io_ = ClipIo("https://ec2.example", "tok", signer_factory=_signer_factory, transport_factory=_transport_factory)
    single = SimpleNamespace(link=SimpleNamespace(publisher=SimpleNamespace(send_event=events)), clock=clock,
                             clips=EventClipService(PacketRingBuffer(("cam_A",)), clock), clip_uploader=None,
                             _clip_io=lambda: io_)
    Tier0Service._attach_clip_upload(single)
    try:
        assert single.clips.export_enabled and isinstance(single.clip_uploader, BackgroundClipUploader)
        assert single.clips.status()["upload"]["uploaded"] == 0
    finally:
        single.clip_uploader.stop(1.0)
    process = _InProcess(clock)
    split = SimpleNamespace(link=single.link, clock=clock, clips=RemoteClips(process), clip_uploader=None,
                            _clip_io=lambda: io_)
    Tier0Service._attach_clip_upload(split)
    try:
        assert split.clips.export_enabled and split.clip_uploader is None
    finally:
        split.clips.stop(1.0)
    offline = SimpleNamespace(link=None, clock=clock, clips=EventClipService(PacketRingBuffer(("cam_A",)), clock),
                              clip_uploader=None, _clip_io=lambda: io_)
    Tier0Service._attach_clip_upload(offline)
    assert not offline.clips.export_enabled
