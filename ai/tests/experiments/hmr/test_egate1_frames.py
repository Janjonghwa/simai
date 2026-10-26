"""E-GATE-1 입력 준비(egate1_frames, S15P31A101-87): 녹화 → 같은 시각 카메라별 그림.

시험 안에서 만든 H.264(프레임 번호를 화소로 새김, tests/services/tier0/live_video.py)로 합성 녹화를 쓰고, 고른 그림이 그 헤더의
프레임인지(번호를 다시 읽어) 확인한다. 반례: 한 카메라에 프레임 공백이 있으면 그 시각을 다른 프레임으로 채우지 않고 비움,
기준 경계(정확히 tolerance 는 받고 넘으면 비움), 결과 폴더가 있으면 덮어쓰지 않음.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
for p in (AI / "experiments" / "hmr", AI / "tests" / "services" / "tier0", AI / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_video as lv  # noqa: E402
from recording_session import Segment, write_session  # noqa: E402

T0 = 1_792_552_246_000


@pytest.fixture
def F():
    sys.modules.pop("egate1_frames", None)
    return importlib.import_module("egate1_frames")


def _segment(cam, epoch, seqs, start, step=40):
    enc = lv.encode(list(seqs))
    frames = []
    for (au, key), seq in zip(enc.frames, seqs):
        meta = lv.frame_meta(cam, epoch, seq, start + step * seq, key)
        meta["received_at_ms"] = start + step * seq + 30
        frames.append((meta, au))
    return Segment(epoch, lv.codec_meta(cam, epoch, enc), frames)


def _session(tmp_path, gap=False):
    """gap 이면 cam_B 가 18~22 번 동안 끊겼다가 새 세그먼트(재접속, stream_epoch 1)로 이어진다."""
    if gap:
        cam_b = [_segment("cam_B", 0, list(range(18)), T0 + 15), _segment("cam_B", 1, list(range(17)), T0 + 15 + 23 * 40)]
    else:
        cam_b = [_segment("cam_B", 0, list(range(40)), T0 + 15)]
    return write_session(tmp_path, "rec-test-egate1", {"cam_A": [_segment("cam_A", 0, list(range(40)), T0)], "cam_B": cam_b})


def _writer(path, image):
    path.write_bytes(json.dumps({"index": lv.read_index(image)}).encode())


def test_selected_frames_are_the_decoded_pictures_of_their_headers(F, tmp_path):
    session = _session(tmp_path)
    out = tmp_path / "frames"
    assert F.main(["--session", str(session), "--out", str(out), "--step-ms", "400"], writer=_writer) == 0
    index = json.loads((out / F.INDEX_FILE).read_text(encoding="utf-8"))
    assert index["format"] == F.FORMAT and index["frames_provenance"]["source"] == "FILE"
    assert len(index["instants"]) == 4  # 공통 구간 T0+15 ~ T0+1560, 400 ms 간격
    for inst in index["instants"]:
        assert set(inst["frames"]) == {"cam_A", "cam_B"} and inst["missing"] == []
        for cid, f in inst["frames"].items():
            assert abs(f["capture_time_ms"] - inst["t_ms"]) <= 20
            assert json.loads((out / f["file"]).read_bytes())["index"] == f["frame_sequence"]
            assert f["timestamp_source"] == "SIM"


def test_gap_in_one_camera_is_left_empty_not_filled(F, tmp_path):
    """반례: cam_B 가 18~22 번(약 0.2 s)을 잃었다. 그 시각은 비우고, 다른 프레임으로 채우지 않는다."""
    headers = F.read_headers(_session(tmp_path, gap=True))
    instants = F.select_instants(headers, step_ms=400, start_offset_ms=0)
    inst = next(i for i in instants if T0 + 18 * 40 + 15 + 20 < i["t_ms"] < T0 + 23 * 40 + 15 - 20)
    assert "cam_B" in inst["missing"] and "cam_B" not in inst["frames"]
    assert "cam_A" in inst["frames"]
    after = [i for i in instants if i["t_ms"] > T0 + 23 * 40 + 15]
    assert after and all(i["frames"]["cam_B"]["stream_epoch"] == 1 for i in after)


def test_tolerance_boundary(F):
    hs = {"cam_A": [{"stream_epoch": 0, "frame_sequence": 0, "capture_time_ms": 1000}],
          "cam_B": [{"stream_epoch": 0, "frame_sequence": 0, "capture_time_ms": 980},
                    {"stream_epoch": 0, "frame_sequence": 1, "capture_time_ms": 1100}]}
    inst = F.select_instants(hs, step_ms=1000, tolerance_ms=20)
    assert inst[0]["t_ms"] == 1000 and inst[0]["frames"]["cam_B"]["frame_sequence"] == 0
    inst = F.select_instants(hs, step_ms=1000, tolerance_ms=19)
    assert inst[0]["missing"] == ["cam_B"]
    with pytest.raises(ValueError):
        F.select_instants(hs, step_ms=0)


def test_existing_output_is_refused(F, tmp_path):
    session = _session(tmp_path)
    out = tmp_path / "frames"
    out.mkdir()
    assert F.main(["--session", str(session), "--out", str(out)], writer=_writer) == 2
    assert list(out.iterdir()) == []


def test_frames_index_feeds_hmr_job_stub(F, tmp_path):
    """그림 목록이 GPU Job 의 입력 형식(카메라별 파일·촬영 시각·frame_ref)을 갖는다."""
    session = _session(tmp_path)
    out = tmp_path / "frames"
    F.main(["--session", str(session), "--out", str(out), "--step-ms", "800", "--max-instants", "1"], writer=_writer)
    index = json.loads((out / F.INDEX_FILE).read_text(encoding="utf-8"))
    f = index["instants"][0]["frames"]["cam_A"]
    assert {"file", "sha256", "stream_epoch", "frame_sequence", "capture_time_ms", "timestamp_source"} <= set(f)
    assert np.isfinite(index["instants"][0]["t_ms"])
