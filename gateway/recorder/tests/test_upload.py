"""관제 녹화 작업 구간 업로드(S15P31A101-668): 합성 녹화로 조각 자르기·mp4 포장·시각 대응·재시도·이어 올리기를 본다.

EC2·S3 는 가짜 opener 로 대신한다(실제 네트워크 없음). 시험 영상은 막대 무늬 H.264(사람 이미지 없음)다.
"""

from __future__ import annotations

import io
import json
import urllib.error
from fractions import Fraction
from pathlib import Path

import av
import pytest
from videos import make_video

from simai_recorder.segment import SegmentWriter
from simai_recorder.upload import plan as plan_mod
from simai_recorder.upload.client import Ec2Client, RequestRejected, TransientError, check_base_url, with_retry
from simai_recorder.upload.mux import mux_chunk
from simai_recorder.upload.plan import Window, plan_chunks, read_tracks, recording_span, segment_uuid
from simai_recorder.upload.runner import STATE_FILE, Uploader, monitoring_active

T0 = 1_794_614_400_000
FRAMES = 60
GOP = 10
#: 21~29 번 프레임은 녹화 큐 넘침으로 빠졌다(중계 녹화는 넘치면 다음 키프레임까지 버린다, -554). frame_sequence 빈칸, 촬영 시각 400 ms 건너뜀
DROPPED = range(21, 30)


def _packets(raw: Path) -> list:
    codec = av.CodecContext.create("h264", "r")
    return codec.parse(raw.read_bytes()) + codec.parse(None)


def capture_ms(i: int) -> int:
    return T0 + i * 40


def make_recording(tmp_path: Path, name: str = "live-20261113-091500", cameras: tuple[str, ...] = ("cam_A",),
                   t0_shift: int = 0) -> Path:
    """카메라마다 epoch 0 하나(60프레임 중 9장 빠짐). 촬영 시각 = T0 + 40 ms × 프레임 번호."""
    rec = tmp_path / name
    rec.mkdir()
    (rec / "session.json").write_text("{}", encoding="utf-8")
    packets = _packets(make_video(tmp_path / f"{name}.h264", frames=FRAMES, gop=GOP))
    assert len(packets) == FRAMES
    for camera in cameras:
        writer = SegmentWriter(rec / camera, {"camera_id": camera, "stream_epoch": 0, "nominal_fps": 25}, False)
        for i, packet in enumerate(packets):
            if i in DROPPED:
                continue
            header = {"camera_id": camera, "stream_epoch": 0, "frame_sequence": i, "keyframe": bool(packet.is_keyframe),
                      "estimated_capture_time_ms": capture_ms(i) + t0_shift}
            writer.write(header, bytes(packet))
        writer.close()
    return rec


# ---------- 조각 자르기 ----------


def test_구간_앞_마지막_키프레임부터_자르고_길이를_넘으면_다음_키프레임에서_자른다(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_mod, "CHUNK_MS", 600)  # 시험: 0.6 s 넘으면 다음 키프레임에서 자름
    tracks = read_tracks(make_recording(tmp_path))
    # 작업 구간: 프레임 13(키프레임 10 뒤) ~ 프레임 47
    window = Window(42, capture_ms(13), capture_ms(47))
    chunks = plan_chunks("live-20261113-091500", tracks, [window])
    assert [c.frames[0].sequence for c in chunks] == [10, 30, 50][: len(chunks)]
    assert chunks[0].frames[0].keyframe and all(c.frames[0].keyframe for c in chunks)
    # 키프레임 10 부터 시작(구간 앞 GOP 하나), 끝은 구간 끝 프레임 47 까지
    assert chunks[0].start_ms == capture_ms(10)
    assert chunks[-1].frames[-1].sequence == 47
    # 이웃 조각은 빈틈 없이 이어진다(앞 조각 끝 = 다음 조각 첫 프레임 촬영 시각)
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.end_ms == b.start_ms
    # 마지막 조각 끝 = 마지막 프레임 + 다음 프레임까지(40 ms)
    assert chunks[-1].end_ms == capture_ms(48)
    # 빠진 자리(20 → 30)는 조각 경계라 두 조각 모두 빈칸이 없고, 앞 조각 끝이 빈칸을 덮는다(마지막 프레임 길이 400 ms)
    assert [c.has_gaps for c in chunks] == [False, False]
    assert chunks[0].frames[-1].sequence == 20 and chunks[0].end_ms == capture_ms(30)
    whole = plan_chunks("live-20261113-091500", tracks, [Window(42, capture_ms(0), capture_ms(59))])
    assert any(c.has_gaps for c in whole)
    assert all(c.work_id == 42 and c.camera_id == "cam_A" and c.stream_epoch == 0 for c in chunks)


def test_조각_id_는_다시_계산해도_같고_UUIDv7_시각_칸은_첫_프레임_촬영_시각이다(tmp_path):
    tracks = read_tracks(make_recording(tmp_path))
    window = Window(7, capture_ms(0), capture_ms(59))
    a = plan_chunks("live-x", tracks, [window])
    b = plan_chunks("live-x", tracks, [window])
    assert [c.segment_id for c in a] == [c.segment_id for c in b]
    sid = a[0].segment_id
    assert sid[14] == "7" and sid[19] in "89ab"
    assert int(sid.replace("-", "")[:12], 16) == capture_ms(0)
    assert segment_uuid(capture_ms(0), "other") != sid


def test_구간_앞_키프레임이_30초보다_멀면_구간_안_첫_키프레임부터(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_mod, "LEAD_MS", 100)
    tracks = read_tracks(make_recording(tmp_path))
    chunks = plan_chunks("r", tracks, [Window(1, capture_ms(13), capture_ms(40))])
    assert chunks[0].frames[0].sequence == 20  # 키프레임 10 은 120 ms 앞이라 버리고 구간 안 첫 키프레임 20 부터


def test_구간과_겹치지_않으면_조각이_없고_epoch_끝_프레임은_공칭_간격으로_끝난다(tmp_path):
    tracks = read_tracks(make_recording(tmp_path))
    assert plan_chunks("r", tracks, [Window(1, capture_ms(100), capture_ms(200))]) == []
    chunks = plan_chunks("r", tracks, [Window(1, capture_ms(0), capture_ms(500))])
    assert chunks[-1].end_ms == capture_ms(59) + 40


def test_비정상_종료로_잘린_사이드카는_앞까지만_읽고_녹화_범위를_파일에서_계산한다(tmp_path):
    rec = make_recording(tmp_path)
    frames = rec / "cam_A" / "epoch_0000.frames.jsonl"
    frames.write_text(frames.read_text(encoding="utf-8") + '{"header": {"camera_id": "cam_A", "stream', encoding="utf-8")
    tracks = read_tracks(rec)
    assert len(tracks[0].frames) == FRAMES - len(DROPPED)
    assert recording_span(tracks) == (capture_ms(0), capture_ms(59))


# ---------- mp4 포장(시각 대응) ----------


def test_mp4_PTS_는_촬영_시각_빼기_조각_시작이고_빠진_프레임_뒤_시각이_밀리지_않는다(tmp_path):
    tracks = read_tracks(make_recording(tmp_path))
    chunk = plan_chunks("r", tracks, [Window(1, capture_ms(0), capture_ms(59))])[0]
    out = tmp_path / "out.mp4"
    mux_chunk(chunk, out)
    with av.open(str(out)) as c:
        assert c.format.name.startswith("mov")
        stream = c.streams.video[0]
        pts_ms = [round(p.pts * stream.time_base * 1000) for p in c.demux(stream) if p.size]
        assert c.duration is not None
        duration_ms = round(c.duration / 1000)
    expected = [row.capture_ms - chunk.start_ms for row in chunk.frames]
    assert pts_ms == expected
    assert 800 in pts_ms and 1200 in pts_ms and 840 not in pts_ms  # 20 → 30 사이 400 ms 빈칸 그대로
    assert duration_ms == chunk.end_ms - chunk.start_ms
    # 재인코딩 없이 압축 프레임 그대로이고 모두 디코딩된다
    with av.open(str(out)) as c:
        assert sum(1 for _ in c.decode(video=0)) == chunk.frame_count
    with av.open(str(out)) as c, chunk.paths.h264.open("rb") as h264:
        stream = c.streams.video[0]
        first = next(p for p in c.demux(stream) if p.size)
        h264.seek(chunk.frames[0].offset)
        original = h264.read(chunk.frames[0].size)
    # mp4 는 길이 접두(avcC), 원본은 시작 코드(Annex B). NAL 내용은 같다
    assert bytes(first)[-16:] == original[-16:]


def test_mp4_는_moov_가_앞에_있다(tmp_path):
    tracks = read_tracks(make_recording(tmp_path))
    chunk = plan_chunks("r", tracks, [Window(1, capture_ms(0), capture_ms(59))])[0]
    out = tmp_path / "fast.mp4"
    mux_chunk(chunk, out)
    head = out.read_bytes()[:4096]
    assert head.find(b"moov") != -1 and head.find(b"moov") < max(head.find(b"mdat"), 4096)


# ---------- EC2·S3 가짜 ----------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeServer:
    """요청을 기록하고 정해 둔 응답을 낸다. put_failures 만큼 PUT 을 503 으로 실패시킨다."""

    def __init__(self, windows: list[dict], open_work: bool = False, put_failures: int = 0, register_error: int | None = None):
        self.windows = windows
        self.open_work = open_work
        self.put_failures = put_failures
        self.register_error = register_error
        self.calls: list[tuple[str, str]] = []
        self.registered: list[dict] = []
        self.progress: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.auth: set[str] = set()
        self.version = "1.0.0"

    def __call__(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        self.calls.append((method, url))
        if url.startswith("https://s3.test/"):
            if self.put_failures:
                self.put_failures -= 1
                raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b""))
            assert request.get_header("Authorization") is None  # 서명 주소에는 토큰을 싣지 않는다
            self.objects[url.split("?")[0].removeprefix("https://s3.test/")] = request.data.read()
            return _Response(b"")
        self.auth.add(request.get_header("Authorization"))
        if "/recordings/windows" in url:
            body = {"schema_version": self.version, "from_ms": 0, "to_ms": 0, "windows": self.windows,
                    "open_work": self.open_work, "retention_days": 14}
            return _Response(json.dumps(body).encode())
        if url.endswith("/api/devices/recordings/progress"):
            self.progress.append(json.loads(request.data))
            return _Response(b'{"status":"ACCEPTED"}')
        if url.endswith("/api/uploads/sign"):
            req = json.loads(request.data)
            assert req["purpose"] == "RECORDING_SEGMENT" and req["content_type"] == "video/mp4"
            key = f"recording-segments/{req['segment_id']}.mp4"
            return _Response(json.dumps({"object_key": key, "method": "PUT", "upload_url": f"https://s3.test/{key}?sig=x",
                                         "headers": {"Content-Type": "video/mp4"}, "expires_at_ms": 0}).encode())
        if url.endswith("/api/devices/recordings/segments"):
            if self.register_error:
                raise urllib.error.HTTPError(url, self.register_error, "no", {},
                                             io.BytesIO(json.dumps({"code": "OUTSIDE_WORK_WINDOW"}).encode()))
            record = json.loads(request.data)
            dup = any(r["segment_id"] == record["segment_id"] for r in self.registered)
            self.registered.append(record)
            return _Response(json.dumps({"segment_id": record["segment_id"], "status": "DUPLICATE" if dup else "CREATED"}).encode())
        raise AssertionError(url)


def _uploader(server: FakeServer, now_ms: int, mode_file: Path | None = None) -> Uploader:
    client = Ec2Client("https://ec2.test", "test-token", opener=server, sleep=lambda s: None)
    return Uploader(client, lambda: now_ms, mode_file=mode_file, parallel=2, sleep=lambda s: None)


NOW = T0 + 3 * 3600 * 1000


def test_작업_구간만_올리고_등록_본문이_계약에_맞으며_다시_돌리면_아무것도_다시_올리지_않는다(tmp_path, contract_validator):
    rec = make_recording(tmp_path, cameras=("cam_A", "cam_B"))
    server = FakeServer([{"work_id": 42, "start_ms": capture_ms(13), "end_ms": capture_ms(47)}], put_failures=1)
    summary = _uploader(server, NOW).run(tmp_path)
    assert summary.uploaded == 2 and summary.failed == 0 and summary.exit_code == 0
    assert server.auth == {"Bearer test-token"}
    for report in server.progress:
        contract_validator("recording_upload_progress", report)
    for record in server.registered:
        contract_validator("recording_segment", record)
        key = record["object_key"]
        assert server.objects[key][4:8] == b"ftyp"
        assert record["size_bytes"] == len(server.objects[key])
        assert record["segment_start_capture_ms"] == capture_ms(10)
        assert record["segment_end_capture_ms"] == capture_ms(48)
        assert record["work_id"] == 42 and record["recording_id"] == rec.name
    # PUT 한 번 503 → 새 서명으로 다시(시도 2번)
    assert sorted(r["upload_attempts"] for r in server.registered) == [1, 2]
    state = json.loads((rec / STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "done" and len(state["segments"]) == 2
    assert not any((rec / ".upload").glob("*.mp4"))  # 임시 mp4 는 지운다
    assert (rec / "cam_A" / "epoch_0000.h264").exists()  # 원본 녹화는 지우지 않는다

    calls = len(server.calls)
    assert _uploader(server, NOW).run(tmp_path).uploaded == 0
    assert len(server.calls) == calls  # 끝난 녹화는 다시 묻지도 않는다


RUN_1 = {"work_id": 42, "run_no": 1, "start_ms": capture_ms(0), "end_ms": capture_ms(15)}
RUN_2 = {"work_id": 42, "run_no": 2, "start_ms": capture_ms(32), "end_ms": capture_ms(47)}


def test_작업_하나의_실행_둘은_구간마다_올리고_진행은_작업_단위로_합쳐_보고한다(tmp_path, contract_validator):
    # S15P31A101-696: recording_windows 1.1.0 은 같은 work_id 를 실행마다 싣는다. 실행 사이(프레임 16~31)는 올리지 않는다
    make_recording(tmp_path)
    server = FakeServer([RUN_1, RUN_2])
    server.version = "1.1.0"
    contract_validator("recording_windows", {"schema_version": "1.1.0", "from_ms": 0, "to_ms": 0,
                                             "windows": [RUN_1, RUN_2], "open_work": False, "retention_days": 14})
    summary = _uploader(server, NOW).run(tmp_path)
    assert summary.uploaded == 2 and summary.failed == 0
    starts = sorted(r["segment_start_capture_ms"] for r in server.registered)
    assert starts == [capture_ms(0), capture_ms(30)]
    assert all(r["work_id"] == 42 for r in server.registered)
    assert {p["work_id"] for p in server.progress} == {42}
    assert all(p["expected_segments"] == 2 for p in server.progress)
    assert server.progress[-1]["sealed"] is True and server.progress[-1]["failed_segments"] == 0


def test_다음_실행이_진행_중이어도_끝난_실행은_올리고_나중에_다음_실행만_더_올린다(tmp_path):
    rec = make_recording(tmp_path)
    server = FakeServer([RUN_1], open_work=True)
    server.version = "1.1.0"
    uploader = _uploader(server, NOW)
    first = uploader.run(tmp_path)
    assert first.uploaded == 1 and rec.name in first.pending_recordings
    assert server.progress[-1]["expected_segments"] == 1
    server.windows, server.open_work = [RUN_1, RUN_2], False
    second = uploader.run(tmp_path)
    assert second.uploaded == 1 and len(server.registered) == 2  # 실행 1 은 다시 올리지 않는다
    assert server.progress[-1]["expected_segments"] == 2 and server.progress[-1]["sealed"] is True
    assert rec.name in second.done_recordings


def test_끊겼다_이어_올리면_올린_조각은_건너뛰고_남은_것만_올린다(tmp_path):
    rec = make_recording(tmp_path, cameras=("cam_A", "cam_B"))
    windows = [{"work_id": 42, "start_ms": capture_ms(0), "end_ms": capture_ms(59)}]
    first = FakeServer(windows)
    _uploader(first, NOW).run(tmp_path)
    state = json.loads((rec / STATE_FILE).read_text(encoding="utf-8"))
    dropped = sorted(state["segments"])[0]
    del state["segments"][dropped]
    state["windows"] = {}
    state["status"] = "pending"
    (rec / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    second = FakeServer(windows)
    summary = _uploader(second, NOW).run(tmp_path)
    assert summary.uploaded == 1 and [r["segment_id"] for r in second.registered] == [dropped]


def test_진행_중_작업이_겹치면_올릴_것만_올리고_녹화는_남겨_둔다(tmp_path):
    rec = make_recording(tmp_path)
    server = FakeServer([], open_work=True)
    summary = _uploader(server, NOW).run(tmp_path)
    assert summary.uploaded == 0 and rec.name in summary.pending_recordings
    assert json.loads((rec / STATE_FILE).read_text(encoding="utf-8"))["status"] == "pending"


def test_관제_중에도_끝난_작업의_닫힌_조각은_올린다(tmp_path):
    make_recording(tmp_path)
    mode = tmp_path / "gateway-mode.txt"
    mode.write_text("\ufeff관제 Pod 1.2.3.4 제어 1 영상 2, 원격 시청 켬, 녹화 C:\\x (11-13 09:15)", encoding="utf-8")
    server = FakeServer([{"work_id": 1, "start_ms": capture_ms(0), "end_ms": capture_ms(59)}])
    summary = _uploader(server, NOW, mode).run(tmp_path)
    assert summary.uploaded == 1 and summary.exit_code == 0
    assert server.progress[-1]["sealed"] is True
    mode.write_text("미리보기(전송 없음), 원격 시청 켬 (11-13 09:15)", encoding="utf-8")
    assert not monitoring_active(mode)
    assert _uploader(server, NOW, mode).run(tmp_path).uploaded == 0


def test_거절된_조각은_rejected_로_적고_다시_올리지_않는다(tmp_path):
    rec = make_recording(tmp_path)
    server = FakeServer([{"work_id": 1, "start_ms": capture_ms(0), "end_ms": capture_ms(59)}], register_error=409)
    summary = _uploader(server, NOW).run(tmp_path)
    assert summary.rejected == 1 and summary.failed == 0
    state = json.loads((rec / STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "done"
    assert next(iter(state["segments"].values()))["code"] == "OUTSIDE_WORK_WINDOW"


def test_보관_기간이_지난_녹화는_묻지도_올리지도_않는다(tmp_path):
    make_recording(tmp_path)
    server = FakeServer([{"work_id": 1, "start_ms": capture_ms(0), "end_ms": capture_ms(59)}])
    summary = _uploader(server, T0 + 15 * 24 * 3600 * 1000).run(tmp_path)
    assert server.calls == [] and summary.done_recordings == ["live-20261113-091500"]


def test_재시도는_네트워크_5xx_만_다시_하고_4xx_는_바로_올린다():
    slept: list[float] = []
    attempts = iter([TransientError("x"), TransientError("y"), "ok"])

    def flaky():
        v = next(attempts)
        if isinstance(v, Exception):
            raise v
        return v

    assert with_retry("시험", flaky, slept.append) == "ok" and slept == [1.0, 2.0]
    with pytest.raises(TransientError):
        with_retry("시험", lambda: (_ for _ in ()).throw(TransientError("z")), slept.append, attempts=3)

    def reject(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, io.BytesIO(b'{"code":"RECORDING_NOT_A_LAPTOP"}'))

    with pytest.raises(RequestRejected) as e:
        Ec2Client("https://ec2.test", "t", opener=reject, sleep=slept.append).windows(0, 1)
    assert e.value.code == "RECORDING_NOT_A_LAPTOP" and "t" not in str(e.value).split(":")[-1]


def test_EC2_주소는_https_만_받고_토큰을_주소에_넣지_않는다():
    assert check_base_url("https://simai.live/") == "https://simai.live"
    assert check_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ("http://simai.live", "https://u:p@simai.live", "https://simai.live?token=x", "ftp://x"):
        with pytest.raises(ValueError):
            check_base_url(bad)
    with pytest.raises(ValueError):
        Ec2Client("https://simai.live", "")


def test_env_로_끄면_업로드_명령이_바로_끝난다(tmp_path, capsys):
    from simai_recorder.cli import main

    env = tmp_path / ".env"
    env.write_text("SIMAI_RECORDING_UPLOAD=off\n", encoding="utf-8")
    assert main(["upload", "--root", str(tmp_path), "--env-file", str(env)]) == 0
    assert "꺼짐" in capsys.readouterr().out
    env.write_text("SIMAI_EC2_BASE_URL=http://simai.live\nSIMAI_DEVICE_TOKEN=x\n", encoding="utf-8")
    assert main(["upload", "--root", str(tmp_path), "--env-file", str(env)]) == 2


def test_시험_영상_시각_기준():
    assert Fraction(1, 1000) * 40 == Fraction(1, 25)


def test_열린_녹화는_닫힌_파일만_읽고_웹_종료를_다음_조회에_반영한다(tmp_path, monkeypatch):
    rec = make_recording(tmp_path)
    (rec / "session.json").write_text(json.dumps({"started_at_ms": T0, "ended_at_ms": None,
        "cameras": [{"camera_id": "cam_A"}]}), encoding="utf-8")
    marker = rec / "cam_A" / "epoch_0000.closed"
    marker.unlink()
    server = FakeServer([], open_work=True)
    uploader = _uploader(server, NOW)
    uploader.run(tmp_path)
    assert server.registered == [] and read_tracks(rec, closed_only=True) == []
    server.windows = [{"work_id": 42, "start_ms": capture_ms(0), "end_ms": capture_ms(47)}]
    server.open_work = False
    uploader.run(tmp_path)
    assert server.progress[-1]["expected_segments"] == 0
    assert server.progress[-1]["sealed"] is False
    marker.touch()
    result = uploader.run(tmp_path)
    assert result.uploaded == 1 and rec.name in result.pending_recordings
    assert server.progress[-1]["sealed"] is True
    assert uploader.run(tmp_path).uploaded == 0


def test_회전한_파일만_공개하고_epoch와_프레임_sequence를_유지한다(tmp_path):
    packets = _packets(make_video(tmp_path / "rotate.h264", frames=FRAMES, gop=GOP))
    rec = tmp_path / "live-rotate"
    writer = SegmentWriter(rec / "cam_A", {"camera_id": "cam_A", "stream_epoch": 3, "nominal_fps": 25}, False)
    writer.rotate_after_ms = 400
    for i, packet in enumerate(packets):
        writer.write({"frame_sequence": i, "keyframe": bool(packet.is_keyframe),
                      "estimated_capture_time_ms": capture_ms(i)}, bytes(packet))
    closed = read_tracks(rec, closed_only=True)
    assert len(closed) == 5
    assert [t.frames[0].sequence for t in closed] == [0, 10, 20, 30, 40]
    assert all(t.stream_epoch == 3 for t in closed)
    assert not writer.paths.closed.exists()
    writer.close()
    tracks = read_tracks(rec, closed_only=True)
    chunks = plan_chunks(rec.name, tracks, [Window(42, capture_ms(0), capture_ms(59))])
    assert len(chunks) == 6 and len({c.segment_id for c in chunks}) == 6
    for chunk in chunks:
        mux_chunk(chunk, tmp_path / f"{chunk.segment_id}.mp4")


def test_파일_재생도_같은_epoch의_회전한_조각을_전부_이어_읽는다(tmp_path):
    from simai_recorder.clock import FakeClock
    from simai_recorder.sources import FileSource

    packets = _packets(make_video(tmp_path / "source.h264", frames=FRAMES, gop=GOP))
    writer = SegmentWriter(tmp_path / "cam_A", {"stream_epoch": 0, "nominal_fps": 25}, False)
    writer.rotate_after_ms = 400
    for i, packet in enumerate(packets):
        writer.write({"frame_sequence": i, "keyframe": bool(packet.is_keyframe),
                      "estimated_capture_time_ms": capture_ms(i)}, bytes(packet))
    writer.close()
    source = FileSource(tmp_path / "cam_A" / "epoch_0000.h264", FakeClock(), realtime=False)
    source.open()
    frames = []
    while (packet := source.read()) is not None:
        frames.append(packet)
    source.release()
    assert len(frames) == FRAMES


def test_여러_녹화의_계획을_먼저_알린_뒤_업로드한다(tmp_path):
    make_recording(tmp_path, name="live-first")
    make_recording(tmp_path, name="live-second", t0_shift=10_000)
    server = FakeServer([{"work_id": 42, "start_ms": T0, "end_ms": T0 + 20_000}])
    result = _uploader(server, NOW).run(tmp_path)
    assert result.uploaded == 2
    first_sign = next(i for i, (_, url) in enumerate(server.calls) if url.endswith('/api/uploads/sign'))
    assert sum(url.endswith('/api/devices/recordings/progress') for _, url in server.calls[:first_sign]) >= 2
    assert {p['recording_id'] for p in server.progress[:2]} == {'live-first', 'live-second'}
    assert all(not p['sealed'] for p in server.progress[:2])


def test_확정된_빈_녹화와_진행중_빈_녹화를_구분한다(tmp_path):
    rec = tmp_path / 'live-empty'
    rec.mkdir()
    session = {'started_at_ms': T0, 'ended_at_ms': None, 'cameras': [{'camera_id': 'cam_A'}]}
    (rec / 'session.json').write_text(json.dumps(session), encoding='utf-8')
    server = FakeServer([{'work_id': 42, 'start_ms': T0, 'end_ms': T0 + 1000}])
    assert _uploader(server, NOW).run(tmp_path).exit_code == 3
    assert server.progress[-1]['expected_segments'] == 0 and not server.progress[-1]['sealed']
    session['ended_at_ms'] = T0 + 2000
    (rec / 'session.json').write_text(json.dumps(session), encoding='utf-8')
    assert _uploader(server, NOW).run(tmp_path).exit_code == 0
    assert server.progress[-1]['expected_segments'] == 0 and server.progress[-1]['sealed']
