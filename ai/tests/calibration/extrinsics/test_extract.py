"""녹화 세션 → keypoint 시퀀스 추출 (-214): 시험 안에서 만든 실제 H.264 세그먼트(사람 영상 없음, 파일은 tmp 에만)로 본다.

- 그림에 새긴 번호(live_video)를 가짜 검출기가 읽어 keypoint u 에 넣는다. 그 값이 줄의 frame_sequence 와 같으면 그림과
  헤더가 맞게 묶였다(G3 결합 오류 0).
- 촬영 시각은 frames.jsonl 헤더 값이다. 공칭 fps 로 다시 만들지 않는다(실제 프레임률이 25 fps 가 아닌 녹화).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "tier0"))

import live_video as lv  # noqa: E402

from simai_ai.calibration.extrinsics.extract import extract_keypoints  # noqa: E402
from simai_ai.calibration.extrinsics.keypoints import (  # noqa: E402
    read_keypoint_file,
    write_keypoint_file,
)
from simai_ai.io import PyAvDecoder  # noqa: E402
from simai_ai.providers.contract import contracts_dir  # noqa: E402

T0 = 1_792_552_246_000
N = 40
BASE = 500  # 그림 번호 = BASE + frame_sequence
PERIOD_MS = 39.7  # 실제 프레임률 25.19 fps (공칭 25)


class IndexDetector:
    """그림 번호를 읽어 keypoint u 로 돌려주는 가짜 검출기. 받은 장 수를 센다."""

    def __init__(self):
        self.seen = 0

    def describe(self):
        return {"name": "fake-index"}

    def detect(self, images):
        self.seen += len(images)
        out = []
        for img in images:
            k = float(lv.read_index(img) - BASE)
            out.append([{"bbox": [0, 0, 1, 1], "score": 0.9, "keypoints_2d": [[k, 0.0, 0.9]] * 17}])
        return out


def _times(cam_offset):
    return [T0 + cam_offset + round(i * PERIOD_MS) for i in range(N)]


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    root = tmp_path_factory.mktemp("rec")
    enc = lv.encode([BASE + i for i in range(N)], gop=10)
    manifest = json.loads((contracts_dir() / "examples" / "recording_manifest.dev_test.json").read_text(encoding="utf-8"))
    manifest = {k: v for k, v in manifest.items() if not k.startswith("_")}
    cams = []
    for cid, offset in (("cam_A", 0), ("cam_B", 13)):
        (root / cid).mkdir()
        times = _times(offset)
        video, lines, pos = b"", [], 0
        for i, (au, key) in enumerate(enc.frames):
            if cid == "cam_B" and i == 20:
                au = au[:20]  # 잘린 키프레임: 디코딩 실패 → 다음 키프레임(30)까지 건너뛴다
            header = lv.frame_meta(cid, 0, i, times[i], key, source="RECEIVE_ESTIMATED", uncertainty=20)
            lines.append(json.dumps({"header": header, "offset": pos, "size": len(au)}))
            video += au
            pos += len(au)
        frames = "\n".join(lines) + "\n"
        codec = json.dumps(lv.codec_meta(cid, 0, enc))
        (root / cid / "epoch_0000.h264").write_bytes(video)
        (root / cid / "epoch_0000.frames.jsonl").write_text(frames, encoding="utf-8")
        (root / cid / "epoch_0000.codec_info.json").write_text(codec, encoding="utf-8")
        cam = json.loads(json.dumps(manifest["cameras"][0]))
        seg = cam["segments"][0]
        cam.update({"camera_id": cid, "frame_count": N, "capture_time_range_ms": [times[0], times[-1]], "reconnects": 0})
        cam["settings"].update({"width": lv.SIZE[0], "height": lv.SIZE[1]})
        cam["events"]["path"] = f"{cid}/events.jsonl"
        seg.update({"frame_count": N, "frame_sequence_range": [0, N - 1], "capture_time_range_ms": [times[0], times[-1]],
                    "received_time_range_ms": [times[0], times[-1]], "keyframe_count": 4})
        seg["video"] = {"path": f"{cid}/epoch_0000.h264", "bytes": len(video), "sha256": _sha(video)}
        seg["frames"] = {"path": f"{cid}/epoch_0000.frames.jsonl", "bytes": len(frames.encode()), "sha256": _sha(frames.encode())}
        seg["codec_info"] = {"path": f"{cid}/epoch_0000.codec_info.json", "bytes": len(codec.encode()), "sha256": _sha(codec.encode())}
        cam["segments"] = [seg]
        cams.append(cam)
    manifest.update({"cameras": cams, "common_capture_window_ms": [T0 + 13, _times(0)[-1]],
                     "started_at_ms": T0 - 100, "ended_at_ms": T0 + 5000})
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _extract(session, **kw):
    det = IndexDetector()
    header, rows = extract_keypoints(session, det, created_at=T0, decoder_factory=PyAvDecoder, **kw)
    return header, list(rows), det


def test_pictures_bind_to_their_own_header_time(session):
    header, rows, _ = _extract(session, every_n=1, start_ms=T0, end_ms=T0 + 10_000)
    times = {"cam_A": _times(0), "cam_B": _times(13)}
    a = [r for r in rows if r["camera_id"] == "cam_A"]
    assert [r["frame_sequence"] for r in a] == list(range(N))
    for r in rows:
        assert r["detections"][0]["keypoints_2d"][0][0] == r["frame_sequence"]  # 그림 번호 = 헤더 번호
        assert r["estimated_capture_time_ms"] == times[r["camera_id"]][r["frame_sequence"]]
    # 공칭 25 fps 로 다시 만든 시각이면 마지막 프레임이 이만큼 어긋난다(반례: 헤더 시각을 써야 한다)
    assert a[-1]["estimated_capture_time_ms"] != T0 + round((N - 1) * 1000 / 25)
    assert header["input_provenance"] == {
        "frames": {"source": "FILE", "dataset_id": "recording-dev_test/rec-20261020-050000z-classroom-lesson",
                   "camera_ids": ["cam_A", "cam_B"]},
        "is_upper_bound": False,
    }


def test_decode_failure_skips_to_next_keyframe(session):
    header, rows, _ = _extract(session, every_n=1, start_ms=T0, end_ms=T0 + 10_000)
    b = [r["frame_sequence"] for r in rows if r["camera_id"] == "cam_B"]
    assert b == [*range(20), *range(30, N)]  # 20(잘림)~29 는 메운 그림 없이 빠진다
    stats = {c["camera_id"]: c for c in header["cameras"]}
    assert stats["cam_B"]["decode_errors"] == 1 and stats["cam_A"]["decode_errors"] == 0


def test_window_starts_from_previous_keyframe_and_every_n(session, tmp_path):
    lo = _times(0)[13]
    hi = _times(0)[27]
    header, rows, det = _extract(session, cameras=["cam_A"], start_ms=lo, end_ms=hi, every_n=3)
    seqs = [r["frame_sequence"] for r in rows]
    # 13 은 키프레임이 아니다: 10 부터 디코딩하되 창 안(13~27)만, 3 장마다 검출한다
    assert seqs == [13, 16, 19, 22, 25]
    assert det.seen == 5 and header["cameras"][0]["frames_in_window"] == 15
    path = tmp_path / "kp.jsonl"
    write_keypoint_file(path, header, rows)
    back_header, back_rows = read_keypoint_file(path)
    assert back_rows == rows and back_header["window_ms"] == [lo, hi]


def test_default_window_is_common_capture_window(session):
    _, rows, _ = _extract(session, every_n=1)
    lo, hi = T0 + 13, _times(0)[-1]
    assert rows and all(lo <= r["estimated_capture_time_ms"] <= hi for r in rows)
    assert min(r["frame_sequence"] for r in rows if r["camera_id"] == "cam_A") == 1  # cam_A 0번(T0)은 창 밖


def test_unknown_camera_is_rejected(session):
    with pytest.raises(ValueError, match="cam_Z"):
        _extract(session, cameras=["cam_Z"])
