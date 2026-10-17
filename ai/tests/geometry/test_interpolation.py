"""카메라 간 시간 보간 시험 (명세 3.9 촬영 시각 계약, 12.7 역순·지연 도착)."""

import pytest

from simai_ai.geometry.interpolation import (
    DEFAULT_INTERPOLATION_WINDOW_MS,
    DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS,
    DropReason,
    InterpolationConfig,
    KeypointBuffer,
    KeypointSample,
    interpolate_camera,
    interpolate_views,
)

T0 = 1_760_342_400_000


def sample(dt_ms, keypoints, camera_id="cam1", uncertainty=4.0, source="CAMERA_STREAM", seq=0):
    return KeypointSample(
        camera_id=camera_id,
        estimated_capture_time=T0 + dt_ms,
        timestamp_source=source,
        timestamp_uncertainty_ms=uncertainty,
        keypoints=tuple(keypoints),
        frame_ref={"stream_epoch": 0, "frame_sequence": seq},
    )


def test_initial_values_match_spec():
    assert DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS == 25.0
    assert DEFAULT_INTERPOLATION_WINDOW_MS == 200


def test_linear_interpolation_between_bracketing_frames():
    a = sample(0, [(100.0, 200.0, 0.9), (10.0, 10.0, 0.5)], seq=0)
    b = sample(40, [(140.0, 180.0, 0.7), (30.0, 50.0, 0.8)], seq=1)
    view, drops = interpolate_camera([b, a], T0 + 10)  # 순서가 뒤섞여 들어와도 정렬해 쓴다
    assert drops == []
    assert view is not None
    assert view.t == T0 + 10
    assert view.keypoints[0] == pytest.approx((110.0, 195.0, 0.7))
    assert view.keypoints[1] == pytest.approx((15.0, 20.0, 0.5))
    assert view.source_times == (T0, T0 + 40)
    assert view.timestamp_uncertainty_ms == 4.0
    assert view.timestamp_source == "CAMERA_STREAM"


def test_exact_time_uses_single_frame_without_needing_next():
    a = sample(0, [(1.0, 2.0, 0.9)])
    view, drops = interpolate_camera([a], T0)
    assert drops == []
    assert view.keypoints == ((1.0, 2.0, 0.9),)
    assert view.source_times == (T0,)


def test_joint_missing_on_either_side_is_not_invented():
    a = sample(0, [(1.0, 2.0, 0.9), None, (5.0, 5.0, 0.9)])
    b = sample(40, [None, (3.0, 3.0, 0.9), (7.0, 7.0, 0.9)])
    view, _ = interpolate_camera([a, b], T0 + 20)
    assert view.keypoints[0] is None
    assert view.keypoints[1] is None
    assert view.keypoints[2] == pytest.approx((6.0, 6.0, 0.9))


def test_no_extrapolation_after_last_frame():
    a = sample(0, [(1.0, 2.0, 0.9)])
    view, drops = interpolate_camera([a], T0 + 10)
    assert view is None
    assert [d.reason for d in drops] == [DropReason.NO_FRAME_AFTER]


def test_no_frame_before():
    b = sample(40, [(1.0, 2.0, 0.9)])
    view, drops = interpolate_camera([b], T0 + 10)
    assert view is None
    assert [d.reason for d in drops] == [DropReason.NO_FRAME_BEFORE]
    assert drops[0].estimated_capture_time is None


@pytest.mark.parametrize("uncertainty", [25.5, 400.0, None, float("nan"), -1.0])
def test_uncertain_timestamp_excludes_camera(uncertainty):
    a = sample(0, [(1.0, 2.0, 0.9)], uncertainty=uncertainty, source="RECEIVE_ESTIMATED")
    b = sample(40, [(1.0, 2.0, 0.9)], uncertainty=4.0)
    view, drops = interpolate_camera([a, b], T0 + 20)
    assert view is None
    assert [(d.reason, d.estimated_capture_time) for d in drops] == [(DropReason.TIMESTAMP_UNCERTAIN, T0)]
    assert drops[0].frame_ref == {"stream_epoch": 0, "frame_sequence": 0}


def test_uncertainty_at_threshold_is_kept():
    a = sample(0, [(1.0, 2.0, 0.9)], uncertainty=25.0)
    b = sample(40, [(1.0, 2.0, 0.9)], uncertainty=25.0)
    view, drops = interpolate_camera([a, b], T0 + 20)
    assert drops == []
    assert view.timestamp_uncertainty_ms == 25.0


def test_frame_outside_window_is_dropped_with_reason():
    a = sample(0, [(1.0, 2.0, 0.9)])
    b = sample(300, [(9.0, 9.0, 0.9)])  # 사이 프레임이 사라졌다
    view, drops = interpolate_camera([a, b], T0 + 250)
    assert view is None
    assert [(d.reason, d.estimated_capture_time) for d in drops] == [(DropReason.OUTSIDE_WINDOW, T0)]
    # 창 안이면 쓴다
    view, drops = interpolate_camera([a, b], T0 + 150, InterpolationConfig(window_ms=300))
    assert drops == []
    assert view.keypoints[0] == pytest.approx((5.0, 5.5, 0.9))


@pytest.mark.parametrize(("before", "after", "kept"), [(-200, 200, True), (-201, 40, False), (-40, 201, False)])
def test_window_boundary_is_distance_from_t(before, after, kept):
    """보간 창은 t 에서 각 프레임까지의 거리다: 200 ms 는 쓰고 201 ms 는 버린다."""
    a = sample(before, [(1.0, 2.0, 0.9)])
    b = sample(after, [(1.0, 2.0, 0.9)])
    view, drops = interpolate_camera([a, b], T0)
    assert (view is not None) == kept
    assert [d.reason for d in drops] == ([] if kept else [DropReason.OUTSIDE_WINDOW])


def test_mixed_cameras_rejected():
    with pytest.raises(ValueError):
        interpolate_camera([sample(0, [None]), sample(40, [None], camera_id="cam2")], T0 + 10)
    with pytest.raises(TypeError):
        interpolate_camera([sample(0, [None])], float(T0))


def test_interpolate_views_excludes_only_bad_cameras():
    good = [sample(0, [(1.0, 1.0, 0.9)]), sample(40, [(3.0, 3.0, 0.9)])]
    bad = [
        sample(5, [(1.0, 1.0, 0.9)], camera_id="cam2", uncertainty=30.0),
        sample(45, [(1.0, 1.0, 0.9)], camera_id="cam2", uncertainty=30.0),
    ]
    views, drops = interpolate_views({"cam1": good, "cam2": bad}, T0 + 20)
    assert set(views) == {"cam1"}
    assert {(d.camera_id, d.reason) for d in drops} == {("cam2", DropReason.TIMESTAMP_UNCERTAIN)}
    with pytest.raises(ValueError):
        interpolate_views({"cam3": good}, T0 + 20)


def test_buffer_sorts_late_frames_and_drops_beyond_window():
    buf = KeypointBuffer("cam1")
    assert buf.add(sample(80, [(8.0, 8.0, 0.9)])) is None
    assert buf.add(sample(40, [(4.0, 4.0, 0.9)])) is None  # 역순 도착: 창 안이면 정렬해 쓴다
    assert [s.estimated_capture_time - T0 for s in buf.samples] == [40, 80]
    late = buf.add(sample(-200, [(0.0, 0.0, 0.9)]))  # 최신(80)보다 280 ms 늦게 도착
    assert late is not None and late.reason == DropReason.LATE_ARRIVAL
    assert late.estimated_capture_time == T0 - 200
    assert len(buf.samples) == 2
    view, _ = buf.interpolate(T0 + 60)
    assert view.keypoints[0] == pytest.approx((6.0, 6.0, 0.9))
    buf.add(sample(120, [(12.0, 12.0, 0.9)]))
    buf.prune(T0 + 100)
    assert [s.estimated_capture_time - T0 for s in buf.samples] == [80, 120]
    with pytest.raises(ValueError):
        buf.add(sample(160, [None], camera_id="cam2"))
