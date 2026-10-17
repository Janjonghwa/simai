"""FrameSource 구현 (명세 5.1 입력 추상화, 20.4): I0 중계 스트림, 시뮬레이터 출력, 녹화 파일이 같은 형식을 낸다."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.io import (
    FakeDecoder,
    I0FrameSource,
    I0Ingest,
    Recording,
    SimObservationFrameSource,
    SkipReason,
    VideoFileFrameSource,
)
from simai_ai.providers import build_input_provenance, provenance_from_providers
from simai_ai.providers.testing import FrameSourceContract
from simai_ai.risk.clock import FixedClock

AI_DIR = Path(__file__).resolve().parents[2]
T0 = 1760342400000


# --------------------------------------------------------------------------- I0


def _i0_source(i0, cameras=("cam1", "cam2"), n=10, source="LIVE"):
    """도착 시각이 붙은 메시지를 feed 가 하나씩 넘기는 I0FrameSource. 추론은 즉시 끝난다고 본다."""
    clock = FixedClock(T0)
    ingest = I0Ingest(cameras, lambda cam: FakeDecoder(delay=1), clock)
    events = []
    for ci, cam in enumerate(cameras):
        events.append((T0, ci, i0.codec(cam=cam)))
        for seq in range(n):
            t = T0 + 40 * seq + 5 * ci
            events.append((t + 30, ci, i0.frame(cam=cam, seq=seq, t=t, keyframe=seq == 0)))
    events.sort(key=lambda e: (e[0], e[1]))
    pending = iter(events)

    def feed():
        event = next(pending, None)
        if event is None:
            return False
        clock.set(max(clock.now_ms(), event[0]))
        ingest.receive(event[2])
        return True

    return I0FrameSource(ingest, feed, source=source)


class TestI0FrameSource(FrameSourceContract):
    @pytest.fixture
    def source(self, i0):
        return _i0_source(i0)


def test_i0_source_flushes_decoder_at_end(i0):
    """입력이 끝나면 디코더(지연 1)에 남은 마지막 그림까지 낸다."""
    frames = list(_i0_source(i0, cameras=("cam1",), n=5).frames())
    assert [f.frame_ref["frame_sequence"] for f in frames] == [0, 1, 2, 3, 4]
    assert all(f.image.frame_sequence == f.frame_ref["frame_sequence"] for f in frames)


def test_i0_tickets_carry_freshness(i0):
    tickets = list(_i0_source(i0, cameras=("cam1",), n=3).tickets())
    assert [t.frame_age_ms for t in tickets[:2]] == [70, 70]  # 디코더 지연 1 프레임(40ms) + 도착 30ms
    assert all(t.queue_wait_ms == 0 for t in tickets[:2])


def test_i0_provenance_live_is_not_upper_bound(i0):
    prov = provenance_from_providers(_i0_source(i0))
    assert prov == {"frames": {"source": "LIVE", "camera_ids": ["cam1", "cam2"]}, "is_upper_bound": False}


# --------------------------------------------------------------------------- 시뮬레이터 출력


@pytest.fixture(scope="module")
def sim_run(tmp_path_factory):
    """`python -m sim.run --scenario basic` 출력 (tests/sim 과 같은 방식으로 실행)."""
    out = tmp_path_factory.mktemp("sim-basic")
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", "basic", "--out", str(out)],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out


class TestSimObservationFrameSource(FrameSourceContract):
    max_frames = 200

    @pytest.fixture
    def source(self, sim_run):
        return SimObservationFrameSource(sim_run)


def test_sim_frames_keep_identity_and_keypoints(sim_run):
    src = SimObservationFrameSource(sim_run)
    assert src.camera_ids == ("cam1", "cam2", "cam3", "cam4")
    first = json.loads((sim_run / "observations" / "cam1.jsonl").read_text(encoding="utf-8").splitlines()[0])
    frame = next(f for f in src.frames() if f.camera_id == "cam1")
    assert frame.frame_ref == {"stream_epoch": first["stream_epoch"], "frame_sequence": first["frame_sequence"]}
    assert frame.estimated_capture_time == first["estimated_capture_time_ms"]
    assert frame.timestamp_source == "SIM" and frame.image is None
    det = frame.detections[0]
    assert det.joint_layout == "COCO17" and len(det.keypoints_2d) == 17
    assert det.local_track_id == first["detections"][0]["local_track_id"]


def test_sim_frames_sorted_across_cameras(sim_run):
    times = [f.estimated_capture_time for f in SimObservationFrameSource(sim_run).frames()]
    assert times == sorted(times)
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in (sim_run / "observations").glob("*.jsonl"))
    assert len(times) == total


def test_sim_provenance_is_upper_bound(sim_run):
    """시뮬레이터 입력으로 얻은 지표는 상한이다 (명세 20.4 규칙 3)."""
    src = SimObservationFrameSource(sim_run)
    manifest = json.loads((sim_run / "manifest.json").read_text(encoding="utf-8"))
    assert src.provenance() == manifest["input_provenance"]["frames"]
    assert build_input_provenance(src.provenance())["is_upper_bound"] is True


def test_sim_camera_subset_and_missing(sim_run):
    assert {f.camera_id for f in SimObservationFrameSource(sim_run, ["cam2"]).frames()} == {"cam2"}
    with pytest.raises(FileNotFoundError):
        SimObservationFrameSource(sim_run, ["cam9"])


# --------------------------------------------------------------------------- 녹화 파일


class ListReader:
    """그림 목록을 차례로 내는 가짜 리더 (OpenCV 없이 시험)."""

    def __init__(self, images, fps=25.0):
        self._images, self._fps, self.closed = list(images), fps, False

    @property
    def fps(self):
        return self._fps

    def read(self):
        return self._images.pop(0) if self._images else None

    def close(self):
        self.closed = True


def _file_source(n=4, fps=25.0, **kw):
    readers = {}

    def open_reader(path):
        readers[path.name] = ListReader([f"{path.stem}-{i}" for i in range(n)], fps)
        return readers[path.name]

    recs = [
        Recording("cam1", Path("cam1.mp4"), T0, file_uri="volume://AP-JP-1/recordings/a/cam1.mp4"),
        Recording("cam2", Path("cam2.mp4"), T0 + 13, file_uri="volume://AP-JP-1/recordings/a/cam2.mp4"),
    ]
    return VideoFileFrameSource(recs, open_reader=open_reader, **kw), readers


class TestVideoFileFrameSource(FrameSourceContract):
    @pytest.fixture
    def source(self):
        return _file_source()[0]


def test_file_frames_times_and_refs():
    src, readers = _file_source(n=3)
    frames = list(src.frames())
    assert [(f.camera_id, f.estimated_capture_time - T0) for f in frames] == [
        ("cam1", 0), ("cam2", 13), ("cam1", 40), ("cam2", 53), ("cam1", 80), ("cam2", 93),
    ]  # fmt: skip
    assert frames[2].frame_ref == {"file_uri": "volume://AP-JP-1/recordings/a/cam1.mp4", "frame_index": 1}
    assert frames[2].image == "cam1-1" and frames[2].timestamp_source == "FILE"
    assert all(r.closed for r in readers.values())


def test_file_fps_override_and_unknown_fps():
    src, _ = _file_source(n=2, fps=0.0)
    with pytest.raises(ValueError):
        list(src.frames())
    recs = [Recording("cam1", Path("cam1.mp4"), T0, fps=20.0)]
    src = VideoFileFrameSource(recs, open_reader=lambda p: ListReader(["a", "b"], 0.0))
    frames = list(src.frames())
    assert [f.estimated_capture_time - T0 for f in frames] == [0, 50]
    assert frames[0].frame_ref["file_uri"].startswith("file:")


def test_file_dataset_provenance():
    src, _ = _file_source(source="DATASET", dataset_id="cmu_panoptic/160422_ultimatum1")
    assert src.provenance() == {
        "source": "DATASET",
        "dataset_id": "cmu_panoptic/160422_ultimatum1",
        "camera_ids": ["cam1", "cam2"],
    }
    assert build_input_provenance(src.provenance())["is_upper_bound"] is False


def test_file_recordings_validation():
    with pytest.raises(ValueError):
        VideoFileFrameSource([])
    with pytest.raises(ValueError):
        VideoFileFrameSource([Recording("cam1", Path("a"), T0), Recording("cam1", Path("b"), T0)])


def test_opencv_reader_roundtrip(tmp_path):
    """OpenCV(io extra)가 있으면 실제 파일을 CPU 로 읽는다. 기본 설치에는 없어 건너뛴다."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    path = tmp_path / "cam1.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (32, 24))
    for i in range(3):
        writer.write(np.full((24, 32, 3), i * 60, dtype=np.uint8))
    writer.release()
    frames = list(VideoFileFrameSource([Recording("cam1", path, T0)]).frames())
    assert [f.estimated_capture_time - T0 for f in frames] == [0, 40, 80]
    assert frames[0].image.shape == (24, 32, 3)


# --------------------------------------------------------------------------- I0 순차 인계 (리뷰 R1)


def _ready_ingest(i0, cameras=("A", "B", "C", "D")):
    """카메라마다 코덱 정보와 키프레임(seq 0)이 T0 에 디코딩돼 대기열에 있는 I0Ingest 와 시계."""
    clock = FixedClock(T0)
    ingest = I0Ingest(cameras, lambda cam: FakeDecoder(), clock)
    for cam in cameras:
        ingest.receive(i0.codec(cam=cam))
        ingest.receive(i0.frame(cam=cam, seq=0, keyframe=True))
    return ingest, clock


def test_i0_source_rechecks_queue_wait_at_handoff(i0):
    """한 장씩 느리게 소비해도, 넘기는 순간의 대기가 상한(150ms)을 넘는 프레임은 넘기지 않는다(G0)."""
    ingest, clock = _ready_ingest(i0)
    handed = []
    for ticket in I0FrameSource(ingest, lambda: False).tickets():
        # 보고한 측정값이 실제로 넘겨받은 시각의 값이어야 한다
        assert ticket.dispatched_at_ms == clock.now_ms()
        assert ticket.queue_wait_ms <= 150
        handed.append(ticket.frame.camera_id)
        clock.advance(100)  # 100ms 추론
    assert handed == ["A", "B"]  # C·D 는 넘기는 순간 200·300ms 를 기다렸으므로 버린다
    stats = ingest.log.stats
    assert [stats[c].inferred for c in "ABCD"] == [1, 1, 0, 0]
    assert [stats[c].skipped_by_policy[SkipReason.QUEUE_WAIT_EXCEEDED] for c in "ABCD"] == [0, 0, 1, 1]


def test_i0_source_hands_newer_frame_that_arrived_during_inference(i0):
    """앞 프레임을 추론하는 동안 더 새 프레임이 오면, 이미 고른 묶음의 옛 프레임이 아니라 새 프레임을 넘긴다."""
    ingest, clock = _ready_ingest(i0, cameras=("A", "B"))
    tickets = I0FrameSource(ingest, lambda: False).tickets()
    first = next(tickets)
    assert first.frame.camera_id == "A"
    clock.advance(40)
    ingest.receive(i0.frame(cam="B", seq=1))
    second = next(tickets)
    assert (second.frame.camera_id, second.frame.frame_ref["frame_sequence"]) == ("B", 1)
    assert second.frame.image.frame_sequence == 1
    assert ingest.log.stats["B"].skipped_by_policy[SkipReason.SUPERSEDED] == 1
