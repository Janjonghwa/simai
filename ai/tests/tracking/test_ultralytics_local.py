"""Ultralytics 백엔드 어댑터: 검출 배열 객체와 YOLO26-pose 결과 변환 (ultralytics 없이 numpy 로 확인).

실제 추적기 생성·갱신은 extras detection 이 있어야 돈다(importorskip).
"""

import math

import numpy as np
import pytest

from simai_ai.tracking.local_config import TrackerKind, tracker_config
from simai_ai.tracking.ultralytics_local import DetectionArrays, pose_detections_from_result


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeTensor:
    """torch 텐서처럼 .cpu().numpy() 로 꺼내는 값."""

    def __init__(self, array):
        self._a = np.asarray(array)

    def cpu(self):
        return self

    def numpy(self):
        return self._a


class FakeBoxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = FakeTensor(xyxy), FakeTensor(conf), FakeTensor(cls)
        self._n = len(conf)

    def __len__(self):
        return self._n


def test_detection_arrays_support_tracker_contract():
    d = DetectionArrays(np.array([[0, 0, 10, 20], [5, 5, 9, 9]]), np.array([0.9, 0.2]), np.zeros(2))
    np.testing.assert_allclose(d.xywh, [[5, 10, 10, 20], [7, 7, 4, 4]])
    sub = d[d.conf > 0.5]
    assert len(sub) == 1 and sub.xyxy.shape == (1, 4)
    assert len(d[np.array([False, False])]) == 0
    with pytest.raises(ValueError):
        DetectionArrays(np.zeros((2, 4)), np.zeros(1), np.zeros(2))


def test_pose_result_to_detections():
    kp = np.zeros((3, 17, 3))
    kp[0, :, :] = [10.0, 20.0, 0.8]
    kp[0, 3] = [np.nan, 1.0, 0.1]
    result = Obj(
        boxes=FakeBoxes([[0, 0, 50, 100], [1, 1, 2, 2], [60, 0, 90, 80]], [0.91, 0.5, 0.4], [0, 2, 0]),
        keypoints=Obj(data=FakeTensor(kp)),
    )
    dets = pose_detections_from_result(result)
    assert len(dets) == 2  # cls 2(사람 아님) 는 뺀다
    assert dets[0].bbox == (0.0, 0.0, 50.0, 100.0)
    assert math.isclose(dets[0].score, 0.91, rel_tol=1e-6)
    assert len(dets[0].keypoints_2d) == 17
    assert dets[0].keypoints_2d[3] is None
    assert dets[0].keypoints_2d[0] == pytest.approx((10.0, 20.0, 0.8))
    assert dets[1].bbox == (60.0, 0.0, 90.0, 80.0)


def test_pose_result_edge_cases():
    assert pose_detections_from_result(Obj(boxes=FakeBoxes(np.zeros((0, 4)), [], []), keypoints=None)) == ()
    no_kp = pose_detections_from_result(Obj(boxes=FakeBoxes([[0, 0, 1, 1]], [0.9], [0]), keypoints=None))
    assert no_kp[0].keypoints_2d == ()
    with pytest.raises(ValueError, match="confidence"):
        pose_detections_from_result(
            Obj(boxes=FakeBoxes([[0, 0, 1, 1]], [0.9], [0]), keypoints=Obj(data=FakeTensor(np.zeros((1, 17, 2)))))
        )


@pytest.mark.parametrize("kind", list(TrackerKind))
def test_backend_runs_without_image(kind):
    pytest.importorskip("ultralytics.trackers")
    from simai_ai.tracking.ultralytics_local import UltralyticsTrackBackend

    backend = UltralyticsTrackBackend(tracker_config(kind))
    pairs = []
    for k in range(4):
        pairs = backend.update([(100 + 3 * k, 50, 180 + 3 * k, 400), (600, 50, 680, 400)], [0.9, 0.95])
    assert sorted(i for _, i in pairs) == [0, 1]
    assert len({tid for tid, _ in pairs}) == 2
    tracked, lost = backend.live_track_ids()
    assert {tid for tid, _ in pairs} <= tracked and not lost
    assert backend.update([], []) == []
    assert backend.live_track_ids()[1] == tracked  # 검출이 사라지면 잃음으로 간다


def test_backend_ids_are_per_instance():
    pytest.importorskip("ultralytics.trackers")
    from simai_ai.tracking.ultralytics_local import UltralyticsTrackBackend

    a, b = (UltralyticsTrackBackend(tracker_config("bytetrack")) for _ in range(2))
    for _ in range(3):
        ia = a.update([(0, 0, 50, 100)], [0.9])
        ib = b.update([(0, 0, 50, 100)], [0.9])
    assert [t for t, _ in ia] == [t for t, _ in ib] == [1]


def test_backend_rejects_features_without_reid():
    pytest.importorskip("ultralytics.trackers")
    from simai_ai.tracking.ultralytics_local import UltralyticsTrackBackend

    backend = UltralyticsTrackBackend(tracker_config("botsort"))
    with pytest.raises(ValueError, match="with_reid"):
        backend.update([(0, 0, 50, 100)], [0.9], [[1.0, 0.0]])
