"""시뮬레이터 고정 시나리오(basic, podium, crossing)로 실제 Ultralytics 추적기 세 종류를 돌린다.

- 교차·가림이 있는 구간을 포함해 카메라별 ID·시간·추적 상태가 섞이지 않는지: 네 카메라를 섞어 묶음으로 넣은 결과가
  카메라 하나씩 따로 넣은 결과와 같아야 한다.
- ID 전환 수는 tests/tracking/sim_tracking.py 로 재고 docs/design/tier0-local-tracker.md 에 기록한다. 여기의 허용치는
  회귀를 잡기 위한 것이고 G2 합격 기준이 아니다. SIM 입력의 상한 성능이다(is_upper_bound).

extras detection(ultralytics, lap)이 없으면 건너뛴다: `uv run --extra detection pytest tests/tracking`.
"""

from collections import defaultdict

import pytest

from simai_ai.tracking.local_config import TrackerKind, tracker_config
from simai_ai.tracking.local_tracker import observation_batch_payload

pytest.importorskip("ultralytics.trackers")

SCENARIOS = ("basic", "podium", "crossing")
MODES = ("amodal", "visible")
KINDS = list(TrackerKind)

# 회귀 허용치 (SIM, 상한). amodal 박스는 ID 전환 0, visible 박스(가림 시 박스가 줄어듦)는 기록값 + 여유
MAX_ID_SWITCHES = {"amodal": 0, "visible": 20}


def _by_camera(results):
    out = defaultdict(list)
    for obs, _cd, _labels in results:
        out[obs.camera_id].append(
            (
                obs.frame_sequence,
                obs.estimated_capture_time_ms,
                tuple((t.local_track_id, t.detection.bbox) for t in obs.detections),
            )
        )
    return dict(out)


@pytest.fixture(scope="module")
def frames(sim_tracking, sim_outputs):
    return {(s, m): sim_tracking.load_frames(sim_outputs[s], m) for s in SCENARIOS for m in MODES}


def test_crossing_has_occlusion(frames):
    """crossing 시나리오에 실제로 사람끼리 가림(conf 0 관절, 검출 누락)이 있어야 아래 시험이 의미가 있다."""
    fr = frames[("crossing", "visible")]
    occluded = sum(
        1 for cd, _ in fr for d in cd.detections if any(kp is not None and kp[2] == 0 for kp in d.keypoints_2d)
    )
    assert occluded > 0
    assert sum(len(cd.detections) for cd, _ in fr) < 2 * len(fr)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_cameras_do_not_mix(sim_tracking, frames, scenario, mode, kind):
    fr = frames[(scenario, mode)]
    config = tracker_config(kind)
    tracker, results = sim_tracking.run_tracker(fr, config)
    together = _by_camera(results)
    cameras = sorted(together)
    assert len(cameras) == 4

    # 1) 카메라 하나씩 따로 돌린 결과와 같다 (다른 카메라 입력이 이 카메라의 ID·상태를 바꾸지 않는다)
    for cam in cameras:
        alone_tracker, alone = sim_tracking.run_tracker([f for f in fr if f[0].camera_id == cam], config, [cam])
        assert _by_camera(alone)[cam] == together[cam], cam
        assert alone_tracker.state(cam) == tracker.state(cam)

    # 2) ID 공간: 카메라 접두어, 카메라 간 겹침 없음
    ids = {cam: {tid for _, _, dets in together[cam] for tid, _ in dets} for cam in cameras}
    for cam in cameras:
        assert ids[cam] and all(tid.startswith(f"{cam}-t") for tid in ids[cam])
    assert sum(len(v) for v in ids.values()) == len(set().union(*ids.values()))

    # 3) 시간 기록: 카메라마다 자기 마지막 프레임 시각·프레임 수
    for cam in cameras:
        own = [cd for cd, _ in fr if cd.camera_id == cam]
        state = tracker.state(cam)
        assert state.frames_processed == len(own)
        assert state.last_capture_time_ms == own[-1].estimated_capture_time_ms
        assert state.last_frame_key == own[-1].frame_key
        assert state.rejected_frames == 0 and state.resets == 0
        assert [t for _, t, _ in together[cam]] == sorted(t for _, t, _ in together[cam])

    # 4) 같은 카메라 안에서 정답 사람 둘이 한 트랙 ID 로 섞이지 않는다
    _per_cam, totals = sim_tracking.id_metrics(results)
    assert totals["merged_ids"] == 0
    assert totals["id_switches"] <= MAX_ID_SWITCHES[mode]


@pytest.mark.parametrize("kind", KINDS)
def test_sim_observation_batch_matches_contract(sim_tracking, frames, contract_errors, kind):
    _tracker, results = sim_tracking.run_tracker(frames[("crossing", "visible")], tracker_config(kind))
    obs = [o for o, _, _ in results]
    for start in range(0, len(obs), 40):
        payload = observation_batch_payload(obs[start : start + 40], calibration_version="cal-sim")
        doc = {
            "schema_version": "1.2.0",
            "msg_type": "observation_batch",
            "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000107",
            "sent_at": obs[-1].estimated_capture_time_ms,
            "source_id": "tier0-01",
            "payload": payload,
        }
        assert contract_errors("observation_batch", doc) == []


def test_measurement_script_marks_upper_bound(sim_tracking, sim_outputs):
    r = sim_tracking.evaluate(sim_outputs["crossing"], TrackerKind.TRACKTRACK, "visible")
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["tracker"] == "tracktrack"


@pytest.mark.parametrize("kind", [TrackerKind.BOTSORT, TrackerKind.TRACKTRACK])
def test_reid_path_runs(sim_tracking, sim_outputs, kind):
    """ReID(with_reid, 검출기 특징) 연결 경로가 도는지만 본다. 특징은 정답 사람별 고정 벡터라 성능 지표가 아니다."""
    out = sim_outputs["crossing"]
    labels = {lab for cd, labs in sim_tracking.load_frames(out) for lab in labs}
    features = {lab: [1.0 if i == j else 0.0 for j in range(len(labels))] for i, lab in enumerate(sorted(labels))}
    fr = sim_tracking.load_frames(out, "visible", features)
    _tracker, results = sim_tracking.run_tracker(fr, tracker_config(kind, with_reid=True))
    _per_cam, totals = sim_tracking.id_metrics(results)
    assert totals["tracked"] > 0
    assert totals["merged_ids"] == 0
