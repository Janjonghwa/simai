"""FrameSource 계약 시험. 메모리 목록 FrameSource 로 계약 시험 묶음과 위반 검출을 확인한다."""

from collections.abc import Iterator, Sequence

import pytest

from simai_ai.providers import Detection2D, Frame, FrameSource
from simai_ai.providers.contract import check_frame, check_frame_source
from simai_ai.providers.testing import FrameSourceContract

T0 = 1760342400000
COCO17_EMPTY = (None,) * 17


class ListFrameSource(FrameSource):
    def __init__(self, source: str, camera_ids: Sequence[str], frames: Sequence[Frame]) -> None:
        self._source, self._camera_ids, self._frames = source, tuple(camera_ids), list(frames)

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._camera_ids

    def frames(self) -> Iterator[Frame]:
        return iter(self._frames)


def _image_frame(cam, i, **kw):
    fields = {
        "camera_id": cam,
        "estimated_capture_time": T0 + 40 * i,
        "timestamp_source": "FILE",
        "frame_ref": {"file_uri": f"volume://AP-JP-1/recordings/{cam}.mp4", "frame_index": i},
        "timestamp_uncertainty_ms": 0,
        "image": object(),  # 이미지 내용은 구현이 정한다. 계약은 해석하지 않는다.
    }
    return Frame(**(fields | kw))


def _sim_frame(cam, i, **kw):
    det = Detection2D(
        bbox=(100.0, 50.0, 200.0, 400.0),
        joint_layout="COCO17",
        keypoints_2d=((150.0, 60.0, 0.9),) + COCO17_EMPTY[1:],
        local_track_id="sim-0",
    )
    fields = {
        "camera_id": cam,
        "estimated_capture_time": T0 + 40 * i,
        "timestamp_source": "SIM",
        "frame_ref": {"stream_epoch": 0, "frame_sequence": i},
        "detections": (det,),
    }
    return Frame(**(fields | kw))


class TestFileImageFrameSource(FrameSourceContract):
    @pytest.fixture
    def source(self):
        return ListFrameSource(
            "FILE", ["cam1", "cam2"], [_image_frame(c, i) for i in range(3) for c in ("cam1", "cam2")]
        )


class TestSimKeypointFrameSource(FrameSourceContract):
    """시뮬레이터는 2D keypoint 단계부터 바로 넣는다 (명세 5.1)."""

    @pytest.fixture
    def source(self):
        return ListFrameSource("SIM", ["cam1"], [_sim_frame("cam1", i) for i in range(3)])


def test_provenance_default():
    src = ListFrameSource("FILE", ["cam1", "cam2"], [])
    assert src.provenance() == {"source": "FILE", "camera_ids": ["cam1", "cam2"]}


def _has(violations, needle):
    assert any(needle in v for v in violations), violations


@pytest.mark.parametrize(
    ("frame", "needle"),
    [
        pytest.param(_image_frame("cam 1", 0), "스키마", id="bad-camera-id"),
        pytest.param(_image_frame("cam1", 0, estimated_capture_time=1760342400.5), "UTC epoch ms", id="float-seconds"),
        pytest.param(_image_frame("cam1", 0, estimated_capture_time=-1), "UTC epoch ms", id="negative-time"),
        pytest.param(_image_frame("cam1", 0, estimated_capture_time=True), "UTC epoch ms", id="bool-time"),
        pytest.param(_image_frame("cam1", 0, timestamp_source="NTP"), "timestamp_source", id="unknown-ts-source"),
        pytest.param(
            _image_frame("cam1", 0, timestamp_uncertainty_ms=-5), "timestamp_uncertainty_ms", id="negative-uncertainty"
        ),
        pytest.param(_image_frame("cam1", 0, frame_ref={"frame_index": 3}), "frame_ref", id="frame-ref-missing-uri"),
        pytest.param(
            _image_frame("cam1", 0, frame_ref={"stream_epoch": 0, "frame_sequence": -1}),
            "frame_ref",
            id="frame-ref-negative",
        ),
        pytest.param(_image_frame("cam1", 0, image=None), "정확히 하나", id="no-payload"),
        pytest.param(_sim_frame("cam1", 0, image=object()), "정확히 하나", id="both-payloads"),
        pytest.param(
            _sim_frame("cam1", 0, detections=(Detection2D((0.0, 0.0, 1.0, 1.0), "COCO17", ((1.0, 1.0, 0.5),)),)),
            "keypoint 17개",
            id="keypoint-count",
        ),
        pytest.param(
            _sim_frame(
                "cam1",
                0,
                detections=(Detection2D((0.0, 0.0, 1.0, 1.0), "COCO17", ((1.0, 1.0, 1.5),) + COCO17_EMPTY[1:]),),
            ),
            "confidence",
            id="confidence-range",
        ),
        pytest.param(
            _sim_frame("cam1", 0, detections=(Detection2D((5.0, 0.0, 1.0, 1.0), "COCO17", COCO17_EMPTY),)),
            "bbox",
            id="bbox-order",
        ),
    ],
)
def test_frame_violations(frame, needle):
    _has(check_frame(frame), needle)


@pytest.mark.parametrize(
    ("source", "ts_source"),
    [("LIVE", "FILE"), ("LIVE", "SIM"), ("FILE", "SIM"), ("DATASET", "SIM")],
)
def test_contradictory_timestamp_source_rejected(source, ts_source):
    """실제 영상에 시뮬레이터 시각, LIVE 에 파일 시각처럼 의미가 모순되는 조합만 막는다."""
    _has(check_frame(_image_frame("cam1", 0, timestamp_source=ts_source), source), "쓸 수 없다")


@pytest.mark.parametrize(
    ("source", "ts_source"),
    [("DATASET", "RECEIVE_ESTIMATED"), ("DATASET", "CAMERA_STREAM"), ("FILE", "RECEIVE_ESTIMATED"), ("SIM", "FILE")],
)
def test_timestamp_source_combinations_allowed(source, ts_source):
    frame = _image_frame("cam1", 0, timestamp_source=ts_source, timestamp_uncertainty_ms=10.0)
    assert check_frame(frame, source) == []


def test_r3_dataset_keeps_receive_estimated_time():
    """리뷰 R3 재현: 공개 데이터에 기록된 수신 추정 시각을 보존하는 어댑터를 막지 않는다."""
    frame = _image_frame("cam1", 0, timestamp_source="RECEIVE_ESTIMATED", timestamp_uncertainty_ms=10.0)
    assert check_frame_source(ListFrameSource("DATASET", ["cam1"], [frame])) == []


def test_capture_time_must_not_decrease_per_camera():
    frames = [_image_frame("cam1", 1), _image_frame("cam2", 0), _image_frame("cam1", 0)]
    _has(check_frame_source(ListFrameSource("FILE", ["cam1", "cam2"], frames)), "줄었다")


def test_cameras_may_interleave_out_of_order():
    """카메라마다 노출 순간이 달라 카메라 사이 순서는 섞일 수 있다. 카메라별로만 줄지 않으면 된다."""
    frames = [_image_frame("cam1", 1), _image_frame("cam2", 0), _image_frame("cam2", 2)]
    assert check_frame_source(ListFrameSource("FILE", ["cam1", "cam2"], frames)) == []


def test_unknown_camera():
    _has(check_frame_source(ListFrameSource("FILE", ["cam1"], [_image_frame("cam2", 0)])), "camera_ids")


def test_unknown_frame_source():
    _has(check_frame_source(ListFrameSource("RTSP", ["cam1"], [_image_frame("cam1", 0)])), "source")


def test_empty_source():
    _has(check_frame_source(ListFrameSource("FILE", ["cam1"], [])), "없다")


def test_max_frames_limits_reading():
    frames = [_image_frame("cam1", 0), _image_frame("cam1", 1, timestamp_source="NTP")]
    assert check_frame_source(ListFrameSource("FILE", ["cam1"], frames), max_frames=1) == []
