"""녹화 I0 재생(RecordingReplay, S15P31A101-215): gateway/recorder 녹화 형식을 I0 메시지로 다시 내는지.

순서(카메라 사이 수신 시각 순, 카메라 안 세그먼트 순, 코덱 정보 먼저), 세그먼트 사이 연결 끝 표시, 헤더 값 그대로, 손상·누락이면
경로·규칙만 적은 ValueError 로 열지 않는지 본다. 영상 바이트는 임의 값이다(디코딩하지 않는다).
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from recording_session import Segment, codec_doc, header, write_session  # noqa: E402

from simai_ai.io import I0CodecInfo, I0Frame, RecordingReplay, recording_manifest_errors  # noqa: E402

T0 = 1792552246000


def _seg(cam, epoch, n, *, start, step=40, lag=30, received=True, first_seq=0):
    frames = [(header(cam, epoch, first_seq + i, start + i * step, i % 10 == 0,
                      received_ms=(start + i * step + lag) if received else None), bytes([i % 256]) * (5 + i))
              for i in range(n)]
    return Segment(epoch, codec_doc(cam, epoch), frames)


@pytest.fixture
def session(tmp_path):
    # cam_A: 세그먼트 둘(재접속), cam_B: 하나. cam_B 는 20 ms 늦게 시작한다
    return write_session(tmp_path, "rec-test-replay", {
        "cam_A": [_seg("cam_A", 0, 5, start=T0), _seg("cam_A", 1, 3, start=T0 + 1000)],
        "cam_B": [_seg("cam_B", 0, 6, start=T0 + 20)],
    })


def test_session_fixture_passes_the_manifest_contract(session) -> None:
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert recording_manifest_errors(manifest) == []


def test_messages_come_in_receive_time_order_with_codec_first_and_segment_end(session) -> None:
    replay = RecordingReplay(session)
    assert replay.camera_ids == ("cam_A", "cam_B")
    assert replay.start_ms == T0 + 30 and replay.end_ms == T0 + 1000 + 2 * 40 + 30
    out = replay.pop_due(10**15)
    assert replay.finished and replay.delivered == len(out)
    assert [m.release_ms for m in out] == sorted(m.release_ms for m in out)
    a = [m for m in out if m.camera_id == "cam_A"]
    kinds = ["codec" if isinstance(m.message, I0CodecInfo) else "end" if m.message is None else "frame" for m in a]
    # 첫 세그먼트: 코덱 → 프레임 5 → 연결 끝, 둘째(마지막) 세그먼트: 코덱 → 프레임 3 (마지막에는 끝 표시 없음)
    assert kinds == ["codec"] + ["frame"] * 5 + ["end", "codec"] + ["frame"] * 3
    frames = [m.message for m in a if isinstance(m.message, I0Frame)]
    assert [(f.header.stream_epoch, f.header.frame_sequence) for f in frames] == [(0, i) for i in range(5)] + [(1, i) for i in range(3)]
    assert frames[2].payload == bytes([2]) * 7  # .h264[offset:offset+size] 그대로
    assert frames[0].header.estimated_capture_time_ms == T0 and frames[0].header.timestamp_source == "RECEIVE_ESTIMATED"
    codec = a[0].message
    assert codec.sps == bytes.fromhex("6742c01e") and codec.stream_epoch == 0
    assert replay.provenance() == {"source": "FILE", "dataset_id": "recording/rec-test-replay",
                                   "camera_ids": ["cam_A", "cam_B"]}
    replay.close()


def test_pop_due_releases_only_what_the_clock_has_reached(session) -> None:
    replay = RecordingReplay(session, camera_ids=["cam_B"])
    assert replay.pop_due(T0 + 49) == []  # 첫 프레임 수신 T0+20+30
    first = replay.pop_due(T0 + 50)
    assert [type(m.message) for m in first] == [I0CodecInfo, I0Frame]  # 코덱 정보는 첫 프레임과 같은 시각
    assert len(replay.pop_due(T0 + 50 + 40)) == 1
    assert not replay.finished


def test_release_falls_back_to_capture_time_without_received_at(tmp_path) -> None:
    session = write_session(tmp_path, "rec-test-noreceived", {"cam_A": [_seg("cam_A", 0, 3, start=T0, received=False)]})
    replay = RecordingReplay(session)
    assert [m.release_ms for m in replay.pop_due(10**15)] == [T0, T0, T0 + 40, T0 + 80]


def test_dev_test_purpose_is_kept_apart_in_dataset_id(tmp_path) -> None:
    consent = {"purpose": "dev_test", "contains_people": True, "retention_until": "2027-01-31"}
    session = write_session(tmp_path, "rec-test-devtest", {"cam_A": [_seg("cam_A", 0, 2, start=T0)]}, consent=consent)
    assert RecordingReplay(session).provenance()["dataset_id"] == "recording-dev_test/rec-test-devtest"


def _rewrite(session: Path, rel: str, data: bytes, *, fix_manifest: bool) -> None:
    """파일을 바꾼다. fix_manifest 면 manifest 의 크기·sha256 도 맞춘다(파일 내용 자체의 모순만 남긴다)."""
    import hashlib

    (session / rel).write_bytes(data)
    if not fix_manifest:
        return
    path = session / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for cam in manifest["cameras"]:
        for seg in cam["segments"]:
            for key in ("video", "frames", "codec_info"):
                if seg[key]["path"] == rel:
                    seg[key]["bytes"] = len(data)
                    seg[key]["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _frames_lines(session: Path, rel: str) -> list[dict]:
    return [json.loads(line) for line in (session / rel).read_text(encoding="utf-8").splitlines()]


def _dump(lines: list[dict]) -> bytes:
    return "".join(json.dumps(x) + "\n" for x in lines).encode()


@pytest.mark.parametrize("case, expect", [
    ("manifest_not_json", "Expecting"),
    ("manifest_schema", "session_id"),
    ("missing_camera", "manifest 에 없는 카메라"),
    ("video_tampered", "sha256 이 manifest 와 다르다"),
    ("video_truncated", "파일 크기가 manifest 와 다르다"),
    ("video_missing", "파일이 없다"),
    ("sequence_gap", "frame_sequence 가 이어지지 않는다"),
    ("offset_outside", "offset·size 가 영상 파일 밖이다"),
    ("line_count", "frame_count 와 다르다"),
    ("codec_epoch", "codec_info 의 카메라·stream_epoch"),
    ("bad_line", "프레임 줄 2 을 읽지 못했다"),
    ("received_not_int", "프레임 줄 1 의 received_at_ms 가 UTC ms 정수가 아니다"),
    ("received_null", "프레임 줄 3 의 received_at_ms 가 UTC ms 정수가 아니다"),
    ("received_negative", "프레임 줄 2 의 received_at_ms 가 UTC ms 정수가 아니다"),
    ("header_required", "프레임 줄 2 을 읽지 못했다"),
])
def test_damaged_or_incomplete_sessions_are_refused_with_a_clear_error(session, case, expect) -> None:
    """반례: 손상·누락 녹화를 지어내 메우지 않고 열기를 거부한다. 문장에는 경로·규칙만(받은 값 없음)."""
    frames_a0 = "cam_A/epoch_0000.frames.jsonl"
    cameras = None
    if case == "manifest_not_json":
        (session / "manifest.json").write_text("{broken", encoding="utf-8")
    elif case == "manifest_schema":
        manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
        del manifest["session_id"]
        (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "missing_camera":
        cameras = ["cam_A", "cam_C"]
    elif case == "video_tampered":
        data = bytearray((session / "cam_B/epoch_0000.h264").read_bytes())
        data[0] ^= 0xFF
        _rewrite(session, "cam_B/epoch_0000.h264", bytes(data), fix_manifest=False)
    elif case == "video_truncated":
        _rewrite(session, "cam_B/epoch_0000.h264", (session / "cam_B/epoch_0000.h264").read_bytes()[:-1],
                 fix_manifest=False)
    elif case == "video_missing":
        (session / "cam_B/epoch_0000.h264").unlink()
    elif case == "sequence_gap":
        lines = _frames_lines(session, frames_a0)
        lines[3]["header"]["frame_sequence"] = 9
        _rewrite(session, frames_a0, _dump(lines), fix_manifest=True)
    elif case == "offset_outside":
        lines = _frames_lines(session, frames_a0)
        lines[-1]["offset"] += 1000
        _rewrite(session, frames_a0, _dump(lines), fix_manifest=True)
    elif case == "line_count":
        _rewrite(session, frames_a0, _dump(_frames_lines(session, frames_a0)[:-1]), fix_manifest=True)
    elif case == "codec_epoch":
        doc = codec_doc("cam_A", 7)
        _rewrite(session, "cam_A/epoch_0000.codec_info.json", json.dumps(doc).encode(), fix_manifest=True)
    elif case == "bad_line":
        lines = _frames_lines(session, frames_a0)
        del lines[1]["header"]["estimated_capture_time_ms"]
        _rewrite(session, frames_a0, _dump(lines), fix_manifest=True)
    elif case.startswith("received_") or case == "header_required":
        # manifest 해시를 맞춘 헤더 손상: 해시가 아니라 내용 검사로 거절해야 한다(받은 수신 시각을 촬영 시각으로 메우지 않음)
        lines = _frames_lines(session, frames_a0)
        index, value = {"received_not_int": (0, "broken"), "received_null": (2, None),
                        "received_negative": (1, -5), "header_required": (1, ...)}[case]
        if value is ...:
            del lines[index]["header"]["source_pts"]
        else:
            lines[index]["header"]["received_at_ms"] = value
        _rewrite(session, frames_a0, _dump(lines), fix_manifest=True)
    with pytest.raises(ValueError) as e:
        RecordingReplay(session, camera_ids=cameras)
    assert expect in str(e.value)
