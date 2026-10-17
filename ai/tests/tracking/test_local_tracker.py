"""카메라별 Local Tracker 의 상태 분리·순서·observation_2d 계약 (명세 5.1, 5.2, FR-57).

추적기 구현은 대역(FakeBackend)으로 바꿔 ultralytics 없이 돈다. 실제 추적기 시험은 test_local_tracker_sim.py.
"""

import pytest

from simai_ai.io import I0FrameHeader
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import (
    CameraDetections,
    CameraLocalTracker,
    MultiCameraLocalTracker,
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
    observation_batch_payload,
)

T0 = 1760342400000
KP17 = tuple((100.0 + i, 200.0 + i, 0.9) for i in range(17))


class FakeBackend:
    """박스 x1 을 100 으로 나눈 몫을 '사람'으로 보고 사람마다 추적기 내부 ID 를 붙이는 대역.

    score < 0.5 인 검출에는 트랙을 붙이지 않는다(미연결 검출 흉내). 마지막 프레임에 나온 사람은 추적 중, 그 전에
    나왔던 사람은 잃음으로 본다.
    """

    def __init__(self, config):
        self.config = config
        self.calls = []
        self._ids = {}
        self._last = set()

    def update(self, boxes, scores, features=None):
        self.calls.append((list(boxes), list(scores), features))
        out = []
        for i, (box, score) in enumerate(zip(boxes, scores)):
            if score < 0.5:
                continue
            person = int(box[0] // 100)
            self._ids.setdefault(person, len(self._ids) + 1)
            out.append((self._ids[person], i))
        self._last = {tid for tid, _ in out}
        return out

    def live_track_ids(self):
        return frozenset(self._last), frozenset(set(self._ids.values()) - self._last)


class Factory:
    def __init__(self):
        self.made = []

    def __call__(self, config):
        backend = FakeBackend(config)
        self.made.append(backend)
        return backend


def det(x=100.0, score=0.9, kps=KP17, feat=None):
    return PoseDetection((x, 50.0, x + 80.0, 400.0), score, kps, feat)


def frame(cam="cam1", seq=0, t=None, dets=(), epoch=0, unc=18.0, ts="RECEIVE_ESTIMATED"):
    return CameraDetections(cam, epoch, seq, T0 + 40 * seq if t is None else t, ts, unc, tuple(dets))


@pytest.fixture
def factory():
    return Factory()


@pytest.fixture
def multi(factory):
    return MultiCameraLocalTracker(tracker_config(), backend_factory=factory)


def ids(obs):
    return [t.local_track_id for t in obs.detections]


def test_each_camera_has_its_own_tracker_and_id_space(multi, factory):
    out = multi.update_batch([frame("cam1", 0, dets=[det(100), det(300)]), frame("cam2", 0, dets=[det(300)])])
    assert [ids(o) for o in out] == [["cam1-t1", "cam1-t2"], ["cam2-t1"]]
    assert len(factory.made) == 2
    cam1, cam2 = factory.made
    assert len(cam1.calls) == 1 and len(cam2.calls) == 1
    assert cam2.calls[0][0] == [(300.0, 50.0, 380.0, 400.0)]


def test_one_camera_does_not_change_another(multi):
    multi.update_batch([frame("cam1", 0, dets=[det(100)]), frame("cam2", 0, t=T0 + 7, dets=[det(100)])])
    before = multi.state("cam2")
    for seq in range(1, 30):
        multi.update_batch([frame("cam1", seq, dets=[det(100), det(500)])])
    multi.update_batch([frame("cam1", 100, t=T0 + 10_000, dets=[det(700)])])  # cam1 만 공백 → cam1 만 새 추적기
    assert multi.state("cam2") == before
    assert multi.state("cam1").resets == 1
    assert multi.state("cam2").resets == 0


def test_time_and_frame_records_are_per_camera(multi):
    multi.update_batch([frame("cam1", 5, t=T0 + 200), frame("cam2", 9, t=T0 + 213, epoch=3)])
    s1, s2 = multi.state("cam1"), multi.state("cam2")
    assert (s1.last_capture_time_ms, s1.last_frame_key, s1.frames_processed) == (T0 + 200, (0, 5), 1)
    assert (s2.last_capture_time_ms, s2.last_frame_key, s2.frames_processed) == (T0 + 213, (3, 9), 1)


def test_stale_frames_are_rejected_without_touching_tracker(multi, factory):
    multi.update_batch([frame("cam1", 10, dets=[det()])])
    out = multi.update_batch(
        [
            frame("cam1", 9, dets=[det()]),  # 이전 frame_sequence
            frame("cam1", 10, dets=[det()]),  # 같은 프레임 다시
            frame("cam1", 11, t=T0 + 40 * 10 - 1, dets=[det()]),  # 촬영 시각이 거꾸로
            frame("cam2", 0, dets=[det()]),
        ]
    )
    assert [o.camera_id for o in out] == ["cam2"]
    assert multi.state("cam1").rejected_frames == 3
    assert multi.state("cam2").rejected_frames == 0
    assert len(factory.made[0].calls) == 1


def test_new_epoch_continues_tracking(multi):
    multi.update_batch([frame("cam1", 500, dets=[det()])])
    out = multi.update_batch([frame("cam1", 0, epoch=1, t=T0 + 40 * 501, dets=[det()])])
    assert ids(out[0]) == ["cam1-t1"]
    assert multi.state("cam1").last_frame_key == (1, 0)


def test_gap_resets_tracker_but_never_reuses_ids(factory):
    cam = CameraLocalTracker("cam1", tracker_config(), factory, max_frame_gap_ms=500)
    assert ids(cam.update(frame(seq=0, dets=[det(100), det(300)]))) == ["cam1-t1", "cam1-t2"]
    assert ids(cam.update(frame(seq=1, dets=[det(100)]))) == ["cam1-t1"]
    obs = cam.update(frame(seq=2, t=T0 + 40 + 501, dets=[det(100)]))
    assert ids(obs) == ["cam1-t3"]
    state = cam.state()
    assert (state.resets, state.issued_ids, len(factory.made)) == (1, 3, 2)
    assert state.tracked_ids == ("cam1-t3",)


def test_untracked_detections_are_counted_not_invented(multi):
    obs = multi.update_batch([frame(dets=[det(100), det(300, score=0.3)])])[0]
    assert ids(obs) == ["cam1-t1"]
    assert obs.untracked_count == 1
    assert multi.state("cam1").untracked_detections == 1


def test_output_keeps_detection_values(multi):
    d = det(100, kps=(None, (1.0, 2.0, 0.0)) + KP17[2:])
    obs = multi.update_batch([frame(dets=[d])])[0]
    assert obs.detections[0].detection is d


def test_lost_tracks_and_map_pruning(multi):
    multi.update_batch([frame(seq=0, dets=[det(100), det(300)])])
    multi.update_batch([frame(seq=1, dets=[det(100)])])
    s = multi.state("cam1")
    assert (s.tracked_ids, s.lost_ids) == (("cam1-t1",), ("cam1-t2",))


def test_backend_must_be_one_to_one(factory):
    class Bad(FakeBackend):
        def update(self, boxes, scores, features=None):
            return [(1, 0), (2, 0)]

    cam = CameraLocalTracker("cam1", tracker_config(), Bad)
    with pytest.raises(RuntimeError, match="1:1"):
        cam.update(frame(dets=[det()]))


def test_reid_requires_features(factory):
    cam = CameraLocalTracker("cam1", tracker_config("botsort", with_reid=True), factory)
    with pytest.raises(ValueError, match="reid_feature"):
        cam.update(frame(dets=[det()]))
    cam.update(frame(seq=1, dets=[det(feat=(1.0, 0.0))]))
    assert factory.made[0].calls[-1][2] == [(1.0, 0.0)]
    plain = CameraLocalTracker("cam1", tracker_config(), factory)
    plain.update(frame(dets=[det(feat=(1.0, 0.0))]))
    assert factory.made[-1].calls[-1][2] is None


def test_fixed_camera_list(factory):
    multi = MultiCameraLocalTracker(camera_ids=["cam1", "cam2"], backend_factory=factory)
    assert multi.camera_ids == ("cam1", "cam2")
    with pytest.raises(KeyError):
        multi.update_batch([frame("cam9")])
    with pytest.raises(ValueError):
        MultiCameraLocalTracker(camera_ids=["cam1", "cam1"], backend_factory=factory)


def test_wrong_camera_into_camera_tracker(factory):
    cam = CameraLocalTracker("cam1", tracker_config(), factory)
    with pytest.raises(ValueError):
        cam.update(frame("cam2"))


def test_from_i0_header():
    header = I0FrameHeader("cam3", 2, 77, T0, "RECEIVE_ESTIMATED", keyframe=False, timestamp_uncertainty_ms=21)
    cd = CameraDetections.from_header(header, [det()])
    assert (cd.camera_id, cd.frame_key, cd.estimated_capture_time_ms, cd.timestamp_uncertainty_ms) == (
        "cam3",
        (2, 77),
        T0,
        21,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bbox": (10.0, 10.0, 5.0, 20.0)},
        {"bbox": (0.0, 0.0, 1.0)},
        {"bbox": (0.0, 0.0, float("nan"), 1.0)},
        {"score": 1.2},
        {"keypoints_2d": ((1.0, 2.0),)},
        {"keypoints_2d": ((1.0, 2.0, float("inf")),)},
        {"reid_feature": ()},
    ],
)
def test_pose_detection_validation(kwargs):
    base = {"bbox": (0.0, 0.0, 1.0, 1.0), "score": 0.5, "keypoints_2d": ()}
    with pytest.raises(ValueError):
        PoseDetection(**{**base, **kwargs})


@pytest.mark.parametrize(
    "kwargs",
    [{"cam": "cam 1"}, {"seq": -1}, {"t": 1.5}, {"ts": "NTP"}, {"unc": -1.0}],
)
def test_camera_detections_validation(kwargs):
    with pytest.raises((ValueError, TypeError)):
        frame(**kwargs)


# --------------------------------------------------------------------------- observation_batch 계약


def _envelope(payload):
    return {
        "schema_version": "1.2.0",
        "msg_type": "observation_batch",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000107",
        "sent_at": T0 + 400,
        "source_id": "tier0-01",
        "payload": payload,
    }


def test_observation_batch_validates_against_schema(multi, contract_errors):
    obs = multi.update_batch(
        [
            frame("cam1", 0, dets=[det(100, kps=(None,) + KP17[1:]), det(300)]),
            frame("cam2", 0, t=T0 + 12, dets=[], ts="SIM", unc=0),
        ]
    )
    payload = observation_batch_payload(obs, calibration_version="cal-7")
    assert contract_errors("observation_batch", _envelope(payload)) == []
    d0 = payload["frames"][0]["detections"][0]
    assert d0["local_track_id"] == "cam1-t1"
    assert d0["keypoints_2d"][0] is None
    assert d0["world_id"] is None
    assert payload["frames"][1]["detections"] == []


def test_unknown_uncertainty_is_not_filled_with_zero(multi):
    obs = multi.update_batch([frame(unc=None, dets=[det()])])
    with pytest.raises(ValueError, match="timestamp_uncertainty_ms"):
        observation_batch_payload(obs, calibration_version="cal-7")


def test_joint_count_must_match_layout():
    obs = Observation2DFrame("cam1", 0, 0, T0, "SIM", 0, (TrackedDetection("cam1-t1", det(kps=KP17[:5])),))
    with pytest.raises(ValueError, match="COCO17"):
        observation_batch_payload([obs], calibration_version="cal-7")
    payload = observation_batch_payload([obs], calibration_version="cal-7", joint_layout="MHR_NATIVE")
    assert len(payload["frames"][0]["detections"][0]["keypoints_2d"]) == 5


@pytest.mark.parametrize("kwargs", [{"frames": []}, {"calibration_version": ""}, {"joint_layout": "OPENPOSE"}])
def test_payload_arguments(kwargs):
    obs = Observation2DFrame("cam1", 0, 0, T0, "SIM", 0, ())
    args = {"frames": [obs], "calibration_version": "cal-7", "joint_layout": "COCO17", **kwargs}
    with pytest.raises(ValueError):
        observation_batch_payload(args.pop("frames"), **args)
