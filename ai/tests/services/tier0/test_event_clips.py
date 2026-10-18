"""이벤트 클립 생성·업로드(S15P31A101-57): 대상 이벤트, 앞뒤 구간, 얼굴 흐림 기본 꺼짐(켜면 반드시 거침), 다시 시도, EC2 단절 임시 저장, 저장 경로 없음."""

from __future__ import annotations

import builtins
import http.client
import io
import os
import pathlib
import re
import shutil
import socket
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
for p in (AI_DIR, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from clip_stream import FRAME_MS, GOP, T0, Stream, transition  # noqa: E402

from services.tier0.clips.media import (  # noqa: E402
    FACE_BLUR_BEFORE_EXPORT,
    FACE_REDACTION_NONE,
    ExportClip,
    MuxedClip,
)
from services.tier0.clips.record import DAY_MS  # noqa: E402
from services.tier0.clips.ring_buffer import PacketRingBuffer  # noqa: E402
from services.tier0.clips.service import ClipConfig, EventClipService  # noqa: E402
from services.tier0.clips.trigger import clip_trigger_for  # noqa: E402
from services.tier0.clips.upload import (  # noqa: E402
    ClipUploader,
    DirectoryClipSpool,
    HttpPutTransport,
    RetryPolicy,
    SignedUpload,
    SignerUnavailable,
    UploadError,
)
from simai_ai.risk.clock import FixedClock  # noqa: E402
from simai_ai.risk.common import Origin, Severity  # noqa: E402
from simai_ai.risk.lifecycle import Change, Lifecycle  # noqa: E402

CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")
GOP_MS = GOP * FRAME_MS
BLUR_TAG = b"[BLURRED]"
FRAME_MARK = re.compile(rb"\|(cam_[A-Z])\|0\|(\d{6})\|")


class FakeRedactor:
    """시험용 흐림 단계: 내용을 바꿔(원본 바이트가 그대로 나가지 않게) 흐림을 거쳤음을 표시한다."""

    version = "fake-blur-1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def redact(self, clip: MuxedClip, cut) -> MuxedClip:
        self.calls += 1
        if self.fail:
            raise RuntimeError("흐림 실패")
        return MuxedClip(BLUR_TAG + bytes(b ^ 0x5A for b in clip.content), clip.content_type, clip.container)


class FakeSigner:
    def __init__(self) -> None:
        self.down = False
        self.requests = []

    def sign(self, request):
        if self.down:
            raise SignerUnavailable("EC2 단절")
        self.requests.append(request)
        n = len(self.requests)
        return SignedUpload(f"https://bucket.example/{request.clip_id}?sig={n}", f"clips/{request.clip_id}.h264",
                            T0 + 10**9, {"x-amz-checksum": request.sha256})


class FakeTransport:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.puts: list[tuple[SignedUpload, bytes, str]] = []
        self.calls = 0

    def put(self, signed, content, content_type):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise UploadError("PUT 응답 503", retryable=True)
        self.puts.append((signed, content, content_type))


class Sink:
    def __init__(self) -> None:
        self.records = []
        self.retention = []

    def emit(self, record):
        self.records.append(record)

    def emit_retention(self, update):
        self.retention.append(update)


def _ids():
    n = iter(range(1, 10**6))
    return lambda: f"0192a0a0-0000-7000-8000-{next(n):012d}"


def _setup(*, redactor=None, transport=None, spool=None, retry=RetryPolicy(), signer=None, cameras=CAMS):
    clock = FixedClock(T0)
    ring = PacketRingBuffer(cameras)
    signer = signer or FakeSigner()
    transport = transport or FakeTransport()
    sink = Sink()
    uploader = ClipUploader(signer, transport, sink, clock, spool=spool, retry=retry)
    service = EventClipService(ring, clock, redactor=redactor, uploader=uploader, clip_id_factory=_ids())
    stream = Stream(cameras)
    service.ring.receive_many(stream.start())
    return clock, service, stream, signer, transport, sink, uploader


def _run(service, clock, stream, duration_ms, step_ms=1_000):
    """스트림과 시계를 함께 진행하며 poll 한다(촬영 시각 ≈ 주입 시계)."""
    for _ in range(duration_ms // step_ms):
        for f in stream.advance(step_ms):
            service.receive(f)
        clock.set(stream.t_ms)
        service.poll()


# ---------------------------------------------------------------------------- 대상 이벤트


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.REPLAY, Origin.WHAT_IF])
def test_non_live_events_make_no_clip(origin):
    assert clip_trigger_for(transition(origin=origin, severity=Severity.CRITICAL)) is None


@pytest.mark.parametrize("severity", [Severity.CAUTION, Severity.INFO])
def test_caution_and_info_make_no_clip(severity):
    assert clip_trigger_for(transition(severity=severity)) is None


@pytest.mark.parametrize(
    ("lifecycle", "changes"),
    [
        (Lifecycle.RESOLVED, (Change.CLEARED,)),
        (Lifecycle.UPDATED, (Change.ACKNOWLEDGED,)),
        (Lifecycle.UPDATED, (Change.METRICS,)),
        (Lifecycle.UPDATED, (Change.RESTORED,)),
        (Lifecycle.UPDATED, (Change.RESTORED, Change.SEVERITY)),
    ],
)
def test_only_onset_transitions_make_a_clip(lifecycle, changes):
    assert clip_trigger_for(transition(lifecycle=lifecycle, changes=changes)) is None


@pytest.mark.parametrize(
    ("severity", "lifecycle", "changes"),
    [
        (Severity.HIGH, Lifecycle.RAISED, (Change.RAISED,)),
        (Severity.CRITICAL, Lifecycle.RAISED, (Change.RAISED,)),
        (Severity.HIGH, Lifecycle.UPDATED, (Change.SEVERITY,)),  # CAUTION → HIGH
        (Severity.HIGH, Lifecycle.UPDATED, (Change.RESUMED,)),
    ],
)
def test_live_critical_high_onsets_make_a_clip(severity, lifecycle, changes):
    trig = clip_trigger_for(transition(severity=severity, lifecycle=lifecycle, changes=changes, observation_time=T0 + 7))
    assert trig is not None and trig.anchor_capture_ms == T0 + 7 and trig.severity is severity


def test_ignored_events_never_reach_the_uploader():
    clock, service, stream, signer, transport, sink, _ = _setup(redactor=FakeRedactor())
    _run(service, clock, stream, 30_000)
    service.on_transitions([
        transition(origin=Origin.WHAT_IF, severity=Severity.CRITICAL, observation_time=stream.t_ms),
        transition(origin=Origin.REPLAY, observation_time=stream.t_ms),
        transition(severity=Severity.CAUTION, observation_time=stream.t_ms),
    ])
    _run(service, clock, stream, 40_000)
    assert service.stats.triggers == 0 and service.stats.ignored_transitions == 3
    assert signer.requests == [] and transport.calls == 0 and sink.records == []


# ---------------------------------------------------------------------------- 앞뒤 구간과 기록


def test_live_high_event_clips_every_camera_around_the_anchor():
    redactor = FakeRedactor()
    clock, service, stream, signer, transport, sink, _ = _setup(redactor=redactor)
    _run(service, clock, stream, 35_000)
    anchor = stream.t_ms - 300
    service.on_transitions([transition(observation_time=anchor)])
    _run(service, clock, stream, 10_000)
    assert sink.records == [] and service.pending == 1  # 사후 20초가 아직 안 찼다
    _run(service, clock, stream, 11_000)
    assert service.pending == 0
    assert [r.draft.camera_id for r in sink.records] == list(CAMS)
    raw = b"".join(f.payload for f in Stream(CAMS).advance(80_000))
    for rec, (signed, content, ctype) in zip(sink.records, transport.puts, strict=True):
        d = rec.draft
        assert d.starts_with_keyframe
        assert anchor - 20_000 - GOP_MS < d.clip_start_capture_ms <= anchor - 20_000
        assert anchor + 20_000 - FRAME_MS < d.clip_end_capture_ms <= anchor + 20_000
        assert not d.truncated_start and not d.truncated_end
        assert d.requested_start_ms == anchor - 20_000 and d.requested_end_ms == anchor + 20_000
        assert d.frame_count == (d.clip_end_capture_ms - d.clip_start_capture_ms) // FRAME_MS + 1
        assert d.source == "LIVE" and d.severity == "HIGH" and d.event_id == transition().event_id
        assert d.face_redaction == FACE_BLUR_BEFORE_EXPORT and d.redactor_version == "fake-blur-1"
        assert d.delete_after_ms == d.created_at_ms + 14 * DAY_MS and d.retention_hold is False
        # 올린 내용은 흐림 단계 출력뿐이다. 원본 접근 단위가 그대로 들어 있지 않다.
        assert content.startswith(BLUR_TAG) and ctype == "video/h264"
        assert raw[:200] not in content and b"|cam_" not in content
        assert d.size_bytes == len(content) and rec.object_key == signed.object_key
        assert rec.upload_attempts == 1 and rec.spooled is False
    assert redactor.calls == len(CAMS)


def test_critical_event_gets_auto_retention_hold_and_upgrade_does_not_reclip():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    t = stream.t_ms
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=t)])
    service.on_transitions([transition(severity=Severity.CRITICAL, lifecycle=Lifecycle.UPDATED,
                                       changes=(Change.SEVERITY,), observation_time=t + 500, event_sequence=2)])
    _run(service, clock, stream, 25_000)
    assert len(sink.records) == 1 and sink.records[0].draft.retention_hold is True
    assert service.stats.duplicate_triggers == 1


def test_resumed_event_gets_a_new_clip():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(lifecycle=Lifecycle.UPDATED, changes=(Change.RESUMED,), resume_count=1,
                                       event_sequence=5, observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    assert [(r.draft.event_id, r.draft.resume_count) for r in sink.records] == [
        (transition().event_id, 0), (transition().event_id, 1)]


def test_stalled_camera_is_cut_at_the_deadline_with_truncation():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    anchor = stream.t_ms
    service.on_transitions([transition(observation_time=anchor)])
    _run(service, clock, stream, 5_000)
    clock.advance(19_000)  # 카메라 정지(프레임 없음), 시계만 간다
    service.poll()
    assert sink.records == []
    clock.advance(1_000)  # 대기 시작 + 20초 + 5초 마감
    service.poll()
    (rec,) = sink.records
    assert rec.draft.truncated_end and rec.draft.clip_end_capture_ms < anchor + 20_000


def test_clip_window_must_fit_in_the_ring():
    with pytest.raises(ValueError):
        EventClipService(PacketRingBuffer(CAMS), FixedClock(T0), config=ClipConfig(pre_ms=30_000, post_ms=30_000))


# ---------------------------------------------------------------------------- 얼굴 흐림: 기본 꺼짐(사용자 결정), 켜면 반드시 거침


def test_default_uploads_clips_without_face_blur():
    clock, service, stream, signer, transport, sink, uploader = _setup()  # 흐림 단계 없음(기본 조립)
    assert service.export_enabled
    _run(service, clock, stream, 25_000)
    anchor = stream.t_ms
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=anchor)])
    _run(service, clock, stream, 30_000)
    assert [r.draft.camera_id for r in sink.records] == list(CAMS) and uploader.pending == 0
    for rec, (_, content, _) in zip(sink.records, transport.puts, strict=True):
        d = rec.draft
        assert d.face_redaction == FACE_REDACTION_NONE and d.redactor_version is None
        assert d.retention_hold is True and d.source == "LIVE"
        # 흐림 없이 재다중화한 내용 그대로다. 그래도 이벤트 구간 프레임뿐이다
        marks = list(FRAME_MARK.finditer(content))
        assert {m.group(1).decode() for m in marks} == {d.camera_id} and len(marks) == d.frame_count
        times = [T0 + int(m.group(2)) * FRAME_MS for m in marks]
        assert anchor - 20_000 - GOP_MS < min(times) and max(times) <= anchor + 20_000


def test_enabled_redactor_is_always_applied_before_export():
    redactor = FakeRedactor()
    clock, service, stream, _, transport, sink, _ = _setup(redactor=redactor, cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 30_000)
    (rec,) = sink.records
    assert redactor.calls == 1 and transport.puts[0][1].startswith(BLUR_TAG) and b"|cam_" not in transport.puts[0][1]
    assert rec.draft.face_redaction == FACE_BLUR_BEFORE_EXPORT and rec.draft.redactor_version == "fake-blur-1"


def test_redactor_failure_does_not_upload_the_unblurred_clip():
    clock, service, stream, signer, transport, sink, _ = _setup(redactor=FakeRedactor(fail=True))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 30_000)
    assert service.stats.failed == len(CAMS)
    assert transport.calls == 0 and sink.records == []


def test_upload_paths_accept_only_export_clips(tmp_path):
    clock = FixedClock(T0)
    uploader = ClipUploader(FakeSigner(), FakeTransport(), Sink(), clock)
    raw = MuxedClip(b"raw", "video/h264", "h264-annexb")
    with pytest.raises(TypeError):
        uploader.enqueue(raw, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DirectoryClipSpool(tmp_path).save(raw, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExportClip(b"x", "video/h264", "h264-annexb", FACE_BLUR_BEFORE_EXPORT, "")  # 흐림인데 버전 없음
    with pytest.raises(ValueError):
        ExportClip(b"x", "video/h264", "h264-annexb", FACE_REDACTION_NONE, "v1")  # 흐림 없음인데 버전 있음
    with pytest.raises(ValueError):
        ExportClip(b"x", "video/h264", "h264-annexb", "BLURRED_AT_VIEW", None)
    assert ExportClip(b"x", "video/h264", "h264-annexb").face_redaction == FACE_REDACTION_NONE


# ---------------------------------------------------------------------------- 다시 시도·EC2 단절


def test_upload_failure_is_retried_with_backoff_and_fresh_signature():
    transport = FakeTransport(failures=2)
    clock, service, stream, signer, _, sink, uploader = _setup(
        redactor=FakeRedactor(), transport=transport, cameras=("cam_A",),
        retry=RetryPolicy(initial_backoff_ms=2_000, max_backoff_ms=8_000))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    assert transport.calls == 1 and sink.records == []  # 첫 시도 실패
    clock.advance(1_999)
    uploader.poll()
    assert transport.calls == 1  # 백오프 2초 전에는 다시 시도하지 않는다
    clock.advance(1)
    uploader.poll()
    assert transport.calls == 2  # 두 번째 실패 → 4초 백오프
    clock.advance(4_000)
    uploader.poll()
    (rec,) = sink.records
    assert transport.calls == 3 and rec.upload_attempts == 3
    urls = {r.clip_id for r in signer.requests}
    assert len(signer.requests) == 3 and len(urls) == 1  # 시도마다 새 서명 주소


def test_upload_gives_up_after_max_attempts_without_spool():
    transport = FakeTransport(failures=100)
    clock, service, stream, _, _, sink, uploader = _setup(
        redactor=FakeRedactor(), transport=transport, cameras=("cam_A",),
        retry=RetryPolicy(initial_backoff_ms=1_000, max_backoff_ms=1_000, max_attempts=3))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 30_000)
    assert transport.calls == 3 and uploader.pending == 0 and uploader.stats.dropped == 1 and sink.records == []


def test_ec2_outage_spools_to_network_volume_and_uploads_after_recovery(tmp_path):
    spool = DirectoryClipSpool(tmp_path / "clips")
    signer = FakeSigner()
    clock, service, stream, _, transport, sink, uploader = _setup(
        redactor=FakeRedactor(), spool=spool, signer=signer, cameras=("cam_A", "cam_B"))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    files = sorted(p.name for p in (tmp_path / "clips").iterdir())
    assert len(files) == 4 and {Path(f).suffix for f in files} == {".clip", ".json"}
    for f in (tmp_path / "clips").glob("*.clip"):
        assert f.read_bytes().startswith(BLUR_TAG)  # 흐림을 켰으면 흐림 단계 출력만 저장한다
    assert transport.calls == 0 and sink.records == [] and uploader.stats.spooled == 2
    _run(service, clock, stream, 120_000)  # 단절이 길어도 시도 상한으로 버리지 않는다
    assert uploader.pending == 2 and uploader.stats.dropped == 0
    signer.down = False
    _run(service, clock, stream, 61_000)
    assert len(sink.records) == 2 and all(r.spooled for r in sink.records)
    assert list((tmp_path / "clips").iterdir()) == []


def test_spooled_clips_are_restored_after_restart(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, _, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    # Tier 0 재시작: 메모리 대기열은 사라지고 Network Volume 의 클립만 남는다
    sink, transport = Sink(), FakeTransport()
    restarted = ClipUploader(FakeSigner(), transport, sink, FixedClock(clock.now_ms()), spool=spool)
    assert restarted.restore_spooled() == 1
    restarted.poll()
    (rec,) = sink.records
    assert rec.spooled and transport.puts[0][1].startswith(BLUR_TAG)
    assert rec.draft.sha256 == ExportClip(transport.puts[0][1], "video/h264", "h264-annexb").sha256
    assert list(tmp_path.iterdir()) == []


def test_tampered_spool_entry_is_not_uploaded(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, _, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    transport = FakeTransport()
    restarted = ClipUploader(FakeSigner(), transport, Sink(), clock, spool=spool,
                             retry=RetryPolicy(max_attempts=2, parked_retry_ms=1_000))
    assert restarted.restore_spooled() == 1
    (clip_file,) = tmp_path.glob("*.clip")
    clip_file.write_bytes(b"not the blurred clip")  # 복원 뒤 실행 중에 내용이 바뀌었다
    for _ in range(5):
        clock.advance(1_000)
        restarted.poll()  # 해시가 기록과 달라 읽기에서 실패한다. 예외는 poll 밖으로 나가지 않는다
    assert transport.calls == 0 and restarted.stats.failures >= 2
    # 다음 기동: 내용을 확인할 수 없는 클립은 올리지 않고 지운다
    again = ClipUploader(FakeSigner(), transport, Sink(), clock, spool=spool)
    assert again.restore_spooled() == 0 and list(tmp_path.iterdir()) == [] and again.stats.swept_files == 2


def test_crash_leftovers_are_swept_at_startup_and_periodically(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, _, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                cameras=("cam_A", "cam_B"))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    good = sorted(p.name for p in tmp_path.iterdir())
    assert len(good) == 4
    cid = "0192a0a0-0000-7000-8000-00000000abcd"
    (tmp_path / f"{cid}.clip").write_bytes(b"orphan content")  # 초안을 쓰기 전에 멈춤
    (tmp_path / f"{cid}.clip.part").write_bytes(b"half")  # 쓰다 만 임시 파일
    bad = "0192a0a0-0000-7000-8000-00000000beef"
    (tmp_path / f"{bad}.clip").write_bytes(b"x")
    (tmp_path / f"{bad}.json").write_text("{broken", encoding="utf-8")  # 깨진 초안
    lone = "0192a0a0-0000-7000-8000-00000000cafe"
    (tmp_path / f"{lone}.json").write_text("{}", encoding="utf-8")  # 내용 없는 초안(지우다 멈춤)
    restarted = ClipUploader(FakeSigner(), FakeTransport(), Sink(), clock, spool=spool)
    assert restarted.restore_spooled() == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == good and restarted.stats.swept_files == 5
    # 실행 중 주기 정리(구조만 확인)
    (tmp_path / f"{cid}.clip.part").write_bytes(b"half")
    restarted.poll()  # 기동 정리 직후라 아직 간격 전
    assert (tmp_path / f"{cid}.clip.part").exists()
    clock.advance(3_600_000)
    restarted.poll()
    assert not (tmp_path / f"{cid}.clip.part").exists()


def test_critical_after_restart_holds_the_spooled_high_clip(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, _, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    assert [d.retention_hold for d in spool.drafts()] == [False]
    # Tier 0 재기동: 새 서비스·업로더, 중복 제거 기억 없음
    signer2, sink2 = FakeSigner(), Sink()
    signer2.down = True
    uploader2 = ClipUploader(signer2, FakeTransport(), sink2, clock, spool=spool)
    assert uploader2.restore_spooled() == 1
    ring2 = PacketRingBuffer(("cam_A",))
    service2 = EventClipService(ring2, clock, redactor=FakeRedactor(), uploader=uploader2, clip_id_factory=_ids())
    stream2 = Stream(("cam_A",), t_ms=stream.t_ms, seq=0, epoch=1)
    ring2.receive_many(stream2.start())
    _run(service2, clock, stream2, 5_000)
    service2.on_transitions([transition(severity=Severity.CRITICAL, lifecycle=Lifecycle.UPDATED,
                                        changes=(Change.SEVERITY,), observation_time=stream2.t_ms, event_sequence=7)])
    assert [d.retention_hold for d in spool.drafts()] == [True]  # 임시 저장된 HIGH 클립에도 보존 표시
    signer2.down = False
    _run(service2, clock, stream2, 30_000)
    old = [r for r in sink2.records if r.draft.severity == "HIGH"]
    assert len(old) == 1 and old[0].draft.retention_hold and old[0].spooled
    assert all(r.draft.retention_hold for r in sink2.records)  # 재기동 뒤 새 CRITICAL 클립도 보존


# ---------------------------------------------------------------------------- 저장 경로 없음(11.5 원본 영상 비저장)


@pytest.fixture
def no_storage(monkeypatch, tmp_path):
    """파일·소켓을 여는 모든 경로를 막고 시도를 기록한다(tmp_path 는 막기 전에 만든다)."""
    attempts: list[str] = []

    def guard(name):
        def blocked(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"저장·전송 경로 호출: {name}")
        return blocked

    for mod, attr in [(builtins, "open"), (io, "open"), (os, "open"), (os, "replace"), (os, "rename"),
                      (os, "makedirs"), (os, "mkdir"), (shutil, "copyfile"), (tempfile, "mkstemp"),
                      (tempfile, "NamedTemporaryFile"), (tempfile, "TemporaryFile"), (socket, "socket"),
                      (socket, "create_connection")]:
        monkeypatch.setattr(mod, attr, guard(f"{mod.__name__}.{attr}"))
    for attr in ("open", "write_bytes", "write_text", "mkdir", "touch"):
        monkeypatch.setattr(pathlib.Path, attr, guard(f"Path.{attr}"))
    return attempts


def test_ring_and_non_target_events_write_nothing(no_storage, tmp_path):
    # Network Volume 임시 저장소가 조립돼 있어도 클립 대상이 아니면 아무것도 쓰지 않는다
    clock, service, stream, signer, transport, sink, _ = _setup(redactor=FakeRedactor(),
                                                                spool=DirectoryClipSpool(tmp_path))
    _run(service, clock, stream, 90_000)  # 링 버퍼가 여러 번 밀려나도
    service.on_transitions([transition(severity=Severity.CAUTION, observation_time=stream.t_ms),
                            transition(origin=Origin.PLANNED, severity=Severity.CRITICAL, observation_time=stream.t_ms)])
    _run(service, clock, stream, 40_000)
    assert no_storage == [] and transport.calls == 0 and signer.requests == []


def test_target_event_with_working_upload_writes_nothing_to_disk(no_storage, tmp_path):
    clock, service, stream, _, transport, sink, _ = _setup(redactor=FakeRedactor(), spool=DirectoryClipSpool(tmp_path))
    _run(service, clock, stream, 60_000)
    anchor = stream.t_ms
    service.on_transitions([transition(observation_time=anchor)])
    _run(service, clock, stream, 60_000)
    assert no_storage == []
    assert len(sink.records) == len(CAMS)
    # 내보낸 것은 이벤트 구간 클립뿐이다: 흐림을 되돌려 보면 구간 밖 프레임은 어느 업로드에도 없다
    for rec, (_, content, _) in zip(sink.records, transport.puts, strict=True):
        plain = bytes(b ^ 0x5A for b in content[len(BLUR_TAG):])
        seqs = [int(m.group(2)) for m in FRAME_MARK.finditer(plain)]
        cams = {m.group(1).decode() for m in FRAME_MARK.finditer(plain)}
        times = [T0 + seq * FRAME_MS for seq in seqs]
        assert cams == {rec.draft.camera_id} and len(seqs) == rec.draft.frame_count
        assert anchor - 20_000 - GOP_MS < min(times) and max(times) <= anchor + 20_000
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_default_unblurred_upload_writes_nothing_to_disk(no_storage, tmp_path):
    # 흐림 없는 기본 조립에서도 EC2 가 살아 있으면 클립은 서명 주소 업로드로만 나가고 디스크에 쓰지 않는다
    clock, service, stream, _, transport, sink, _ = _setup(spool=DirectoryClipSpool(tmp_path))
    _run(service, clock, stream, 60_000)
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=stream.t_ms)])
    _run(service, clock, stream, 30_000)
    assert no_storage == [] and len(sink.records) == len(CAMS) == len(transport.puts)
    assert all(r.draft.face_redaction == FACE_REDACTION_NONE for r in sink.records)


def test_storage_guard_catches_a_real_write(no_storage, tmp_path):
    """위 시험들의 막기 장치가 실제 쓰기를 잡는지 확인한다(EC2 단절 + 클립 → Network Volume 저장 시도)."""
    clock, service, stream, signer, _, _, _ = _setup(redactor=FakeRedactor(), spool=DirectoryClipSpool(tmp_path),
                                                     cameras=("cam_A",))
    signer.down = True
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)  # 업로더는 저장 실패를 삼키고(Tier 0 루프 보호) 시도는 기록에 남는다
    assert no_storage


# ---------------------------------------------------------------------------- HTTP PUT


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_put_transport_maps_errors_and_refuses_plain_http():
    seen = []

    def ok(request, timeout):
        seen.append((request.get_method(), request.full_url, request.headers))
        return _Resp()

    signed = SignedUpload("https://bucket.example/k?sig=1", "k", T0, {"x-amz-meta-a": "b"})
    HttpPutTransport(opener=ok).put(signed, b"data", "video/h264")
    assert seen[0][0] == "PUT" and seen[0][2]["Content-type"] == "video/h264"
    with pytest.raises(UploadError) as e:
        HttpPutTransport(opener=ok).put(SignedUpload("http://bucket.example/k", "k", T0), b"d", "video/h264")
    assert not e.value.retryable
    HttpPutTransport(opener=ok).put(SignedUpload("http://127.0.0.1:9/k", "k", T0), b"d", "video/h264")

    def fail(code):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return opener

    for code, retryable in [(503, True), (429, True), (403, False)]:
        with pytest.raises(UploadError) as e:
            HttpPutTransport(opener=fail(code)).put(signed, b"d", "video/h264")
        assert e.value.retryable is retryable


def test_http_put_transport_wraps_http_client_exceptions():
    def opener(request, timeout):
        raise http.client.IncompleteRead(b"partial")

    with pytest.raises(UploadError) as e:
        HttpPutTransport(opener=opener).put(SignedUpload("https://b.example/k?sig=s", "k", T0), b"d", "video/h264")
    assert e.value.retryable and "sig=" not in str(e.value)


# ---------------------------------------------------------------------------- 리뷰 반영: 상향 보존, 임시 저장 보관, 예외 격리


def _escalation(t_ms, seq=2):
    return transition(severity=Severity.CRITICAL, lifecycle=Lifecycle.UPDATED, changes=(Change.SEVERITY,),
                      observation_time=t_ms, event_sequence=seq)


def test_high_to_critical_before_cut_records_critical_with_hold():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A", "cam_B"))
    _run(service, clock, stream, 25_000)
    anchor = stream.t_ms
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=anchor)])
    _run(service, clock, stream, 2_000)
    service.on_transitions([_escalation(stream.t_ms)])
    _run(service, clock, stream, 25_000)
    assert len(sink.records) == 2 and sink.retention == []
    for r in sink.records:
        assert r.draft.retention_hold is True and r.draft.severity == "CRITICAL" and r.draft.anchor_capture_ms == anchor
    assert service.stats.retention_escalations == 1


def test_high_to_critical_after_upload_appends_retention_update():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A", "cam_B"))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    assert [r.draft.retention_hold for r in sink.records] == [False, False]
    service.on_transitions([_escalation(stream.t_ms)])
    service.on_transitions([_escalation(stream.t_ms + 100, seq=3)])  # 두 번 와도 추가 기록은 한 번
    _run(service, clock, stream, 2_000)
    # 원래 기록은 바꾸지 않고(덮어쓰기 없음) 클립마다 보존 표시 추가 기록을 낸다
    assert [r.draft.retention_hold for r in sink.records] == [False, False] and len(sink.records) == 2
    assert sorted(u.clip_id for u in sink.retention) == sorted(r.draft.clip_id for r in sink.records)
    for u in sink.retention:
        assert u.retention_hold and u.severity == "CRITICAL" and u.reason == "CRITICAL_AUTO" and u.source == "LIVE"
        assert u.to_payload()["event_id"] == transition().event_id


def test_high_to_critical_while_spooled_updates_the_unsent_record(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                   cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    service.on_transitions([_escalation(stream.t_ms)])
    assert [d.retention_hold for d in spool.drafts()] == [True]  # 재시작해도 보존 표시가 남는다
    signer.down = False
    _run(service, clock, stream, 61_000)
    (rec,) = sink.records
    assert rec.draft.retention_hold and rec.spooled and sink.retention == []


def test_restore_deletes_expired_spool_entries_but_keeps_held_ones(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, _, _, _ = _setup(redactor=FakeRedactor(), spool=spool, signer=signer,
                                                cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=stream.t_ms,
                                       event_id="0192a0a0-0000-7000-8000-000000000099")])
    _run(service, clock, stream, 21_000)
    assert len(spool.drafts()) == 2
    later = FixedClock(clock.now_ms() + 15 * DAY_MS)  # 삭제 예정일(14일) 지남
    sink, transport = Sink(), FakeTransport()
    restarted = ClipUploader(FakeSigner(), transport, sink, later, spool=spool)
    assert restarted.restore_spooled() == 1  # CRITICAL(보존 표시)만 되돌린다
    assert restarted.stats.expired == 1
    restarted.poll()
    (rec,) = sink.records
    assert rec.draft.retention_hold and transport.calls == 1
    assert list(tmp_path.iterdir()) == []


def test_queued_clip_past_delete_after_is_dropped_and_spool_cleaned(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    signer = FakeSigner()
    clock, service, stream, _, transport, sink, uploader = _setup(redactor=FakeRedactor(), spool=spool,
                                                                  signer=signer, cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    signer.down = True
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    _run(service, clock, stream, 21_000)
    assert uploader.pending == 1 and any(tmp_path.iterdir())
    clock.advance(14 * DAY_MS)
    service.poll()
    assert uploader.pending == 0 and uploader.stats.expired == 1 and list(tmp_path.iterdir()) == []
    assert transport.calls == 0 and sink.records == []


def test_exhausted_clip_stays_spooled_and_is_retried_periodically(tmp_path):
    spool = DirectoryClipSpool(tmp_path)
    transport = FakeTransport(failures=4)
    clock, service, stream, _, _, sink, uploader = _setup(
        redactor=FakeRedactor(), spool=spool, transport=transport, cameras=("cam_A",),
        retry=RetryPolicy(initial_backoff_ms=1_000, max_backoff_ms=1_000, max_attempts=3, parked_retry_ms=60_000))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    assert transport.calls == 3 and uploader.pending == 1 and uploader.memory_bytes() == 0
    assert len(list(tmp_path.glob("*.clip"))) == 1  # 상한을 넘겨 임시 저장소에 둔다
    _run(service, clock, stream, 50_000)
    assert transport.calls == 3  # 긴 간격 전에는 다시 시도하지 않는다
    _run(service, clock, stream, 15_000)
    assert transport.calls == 4 and sink.records == []  # 4번째도 실패 → 다시 60초 뒤
    _run(service, clock, stream, 61_000)
    (rec,) = sink.records
    assert rec.upload_attempts == 5 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("where", ["transport", "signer", "sink", "spool"])
def test_unexpected_exceptions_never_escape_poll(tmp_path, where):
    remaining = {"n": 2}

    def maybe(exc):
        if remaining["n"] > 0:
            remaining["n"] -= 1
            raise exc

    class Transport(FakeTransport):
        def put(self, signed, content, content_type):
            if where == "transport":
                maybe(http.client.IncompleteRead(b"x"))
            super().put(signed, content, content_type)

    class Signer(FakeSigner):
        def sign(self, request):
            if where == "signer":
                maybe(RuntimeError("bad response"))
            return super().sign(request)

    class BadSink(Sink):
        def emit(self, record):
            if where == "sink":
                maybe(RuntimeError("journal down"))
            super().emit(record)

    class BadSpool(DirectoryClipSpool):
        def save(self, clip, draft):
            if where == "spool":
                maybe(RuntimeError("volume error"))
            super().save(clip, draft)

    clock = FixedClock(T0)
    ring = PacketRingBuffer(("cam_A",))
    signer, transport, sink = Signer(), Transport(), BadSink()
    signer.down = where == "spool"
    uploader = ClipUploader(signer, transport, sink, clock, spool=BadSpool(tmp_path),
                            retry=RetryPolicy(initial_backoff_ms=1_000, max_backoff_ms=1_000))
    service = EventClipService(ring, clock, redactor=FakeRedactor(), uploader=uploader, clip_id_factory=_ids())
    stream = Stream(("cam_A",))
    ring.receive_many(stream.start())
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)  # 예외가 새면 여기서 시험이 실패한다
    signer.down = False
    _run(service, clock, stream, 70_000)
    assert remaining["n"] == 0 and len(sink.records) == 1 and uploader.pending == 0


def test_queue_overflow_spools_and_releases_memory(tmp_path):
    signer = FakeSigner()
    signer.down = True
    clock, service, stream, _, _, _, uploader = _setup(
        redactor=FakeRedactor(), spool=DirectoryClipSpool(tmp_path), signer=signer,
        retry=RetryPolicy(max_queue_bytes=1_000))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    service.poll()
    assert uploader.pending == 0
    _run(service, clock, stream, 21_000)
    assert uploader.pending == len(CAMS) and uploader.memory_bytes() == 0
    assert len(list(tmp_path.glob("*.clip"))) == len(CAMS)


def test_clip_records_gaps_inside_the_window():
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor(), cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(observation_time=stream.t_ms)])
    stream.seq += 3  # 프레임 3개 손실
    stream.t_ms += 3 * FRAME_MS
    _run(service, clock, stream, 25_000)
    (rec,) = sink.records
    assert rec.draft.has_gaps


# --- 계약 media_clip·media_clip_retention (S15P31A101-169) ----------------------------------------------------------

CONTRACTS = AI_DIR.parent / "contracts"


@pytest.fixture(scope="module")
def contract_errors():
    """errors(msg_type, payload): 그 계약 예시의 봉투에 payload 를 넣어 검증한 오류 목록."""
    import json

    import jsonschema
    import referencing

    registry = referencing.Registry()
    by_name = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], referencing.Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema

    def errors(name: str, payload: dict) -> list[str]:
        example = json.loads((CONTRACTS / "examples" / f"{name}.json").read_text(encoding="utf-8"))
        doc = {**example, "payload": payload}
        validator = jsonschema.Draft202012Validator(by_name[name], registry=registry)
        return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(doc)]

    return errors


@pytest.mark.parametrize("blur", [False, True])
def test_media_clip_records_match_contract(contract_errors, blur):
    clock, service, stream, _, _, sink, _ = _setup(redactor=FakeRedactor() if blur else None, cameras=("cam_A", "cam_B"))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(severity=Severity.CRITICAL, observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    assert len(sink.records) == 2
    for rec in sink.records:
        payload = rec.to_payload()
        assert payload["face_redaction"] == (FACE_BLUR_BEFORE_EXPORT if blur else FACE_REDACTION_NONE)
        assert contract_errors("media_clip", payload) == [], payload


def test_media_clip_retention_updates_match_contract(contract_errors):
    clock, service, stream, _, _, sink, _ = _setup(cameras=("cam_A",))
    _run(service, clock, stream, 25_000)
    service.on_transitions([transition(severity=Severity.HIGH, observation_time=stream.t_ms)])
    _run(service, clock, stream, 25_000)
    (rec,) = sink.records
    assert contract_errors("media_clip", rec.to_payload()) == []
    service.on_transitions([_escalation(stream.t_ms)])
    _run(service, clock, stream, 2_000)
    (update,) = sink.retention
    assert contract_errors("media_clip_retention", update.to_payload()) == [], update.to_payload()
