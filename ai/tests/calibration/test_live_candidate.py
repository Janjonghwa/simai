"""운영용 관절 고리 버퍼(`LiveJointBuffer`)와 백그라운드 후보 보정 실행기(`LiveCandidateCalibrationRunner`) 시험 (S15P31A101-345).

- 고리 버퍼의 시간·프레임·표본·메모리 상한과 `reset_camera(camera_id)` 초기화
- 데이터 부족 시 후보 보정을 제안하지 않음(`STATUS_INSUFFICIENT_DATA`)
- 이미 정밀한 활성 보정 대비 개선이 미달이면 후보 보정을 제안하지 않음(`STATUS_NO_MEANINGFUL_IMPROVEMENT`)
- 작은 회전 틀어짐용 빠른 길(`realign_rotation`) 채택 및 큰 틀어짐에서 전체 `session` 보정 폴백
- 별도 프로세스 시간 상한(`STATUS_TIMEOUT`), Tier 0 우선순위 보류(`STATUS_DEFERRED`), 콜백 `propose_calibration(document, report)` 보고 형식 검증
"""

from __future__ import annotations

import copy
from pathlib import Path

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.criteria import CalibrationEvidence, judge_calibration
from simai_ai.calibration.document import build_calibration_doc, content_hash
from simai_ai.calibration.extrinsics.session import FIXED_LENS
from simai_ai.calibration.live_candidate import (
    LIVE_CANDIDATE_REPORT_FORMAT,
    LIVE_CANDIDATE_REPORT_VERSION,
    MODE_FAST_ROTATION,
    MODE_FULL_SESSION,
    STATUS_DEFERRED,
    STATUS_INSUFFICIENT_DATA,
    STATUS_NO_MEANINGFUL_IMPROVEMENT,
    STATUS_PROPOSED,
    STATUS_TIMEOUT,
    LiveBufferConfig,
    LiveCandidateCalibrationRunner,
    LiveCandidateConfig,
    LiveJointBuffer,
)
from simai_ai.gpu.admission import AdmissionGate
from simai_ai.providers.contract import check_calibration
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection

T0 = 1793238900000
K = [[FIXED_LENS[0], 0.0, 1152.0], [0.0, FIXED_LENS[0], 648.0], [0.0, 0.0, 1.0]]
DIST = [*FIXED_LENS[1:], 0.0, 0.0, 0.0]


def _camera(cid: str, center: list[float]) -> dict:
    C = np.asarray(center, dtype=float)
    forward = np.array([4.5, 3.75, 0.9]) - C
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    R = np.stack([right, np.cross(forward, right), forward])
    return {
        "camera_id": cid,
        "image_size": [2304, 1296],
        "K": copy.deepcopy(K),
        "distortion": {"model": "OPENCV_PINHOLE", "coeffs": DIST},
        "R": R.tolist(),
        "t": (-R @ C).tolist(),
    }


def _make_observation_frame(
    camera_id: str,
    seq: int,
    t_ms: int,
    people_uvs: list[tuple[str, np.ndarray]],
    *,
    epoch: int = 0,
) -> Observation2DFrame:
    tracked = []
    for tid, uv in people_uvs:
        kps = tuple((float(p[0]), float(p[1]), 0.99) for p in uv)
        bbox = (
            float(uv[:, 0].min()),
            float(uv[:, 1].min()),
            float(uv[:, 0].max()),
            float(uv[:, 1].max()),
        )
        tracked.append(
            TrackedDetection(
                local_track_id=tid,
                detection=PoseDetection(bbox=bbox, score=0.99, keypoints_2d=kps),
            )
        )
    return Observation2DFrame(
        camera_id=camera_id,
        stream_epoch=epoch,
        frame_sequence=seq,
        estimated_capture_time_ms=t_ms,
        timestamp_source="SIM",
        timestamp_uncertainty_ms=0,
        detections=tuple(tracked),
        untracked_count=0,
    )


@pytest.fixture(scope="module")
def synthetic_rig_and_frames():
    cams = [
        _camera("cam_A", [0.3, 0.3, 2.21]),
        _camera("cam_B", [8.7, 0.3, 2.195]),
        _camera("cam_C", [8.7, 7.2, 2.23]),
        _camera("cam_D", [0.3, 7.2, 2.24]),
    ]
    heights = {"cam_A": 2.21, "cam_B": 2.195, "cam_C": 2.23, "cam_D": 2.24}
    truth_doc = build_calibration_doc(
        calibration_version="cal-truth-001",
        provider_source="SIM",
        provider_version="synthetic-1",
        created_at=T0,
        cameras=cams,
        judgement=judge_calibration("SIM", cams, CalibrationEvidence()),
        fallback_scale_basis="MODEL_ESTIMATE",
        site_id="lobby",
        world_axes_source="WALLS",
    )
    provenance = {"frames": {"source": "SIM", "dataset_id": "live-candidate-synthetic"}, "is_upper_bound": True}
    body = np.array(
        [
            [0, 0, 1.68],
            [-0.03, 0, 1.72],
            [0.03, 0, 1.72],
            [-0.08, 0, 1.7],
            [0.08, 0, 1.7],
            [-0.2, 0, 1.4],
            [0.2, 0, 1.4],
            [-0.3, 0, 1.1],
            [0.3, 0, 1.1],
            [-0.36, 0, 0.95],
            [0.36, 0, 0.95],
            [-0.12, 0, 0.9],
            [0.12, 0, 0.9],
            [-0.12, 0, 0.5],
            [0.12, 0, 0.5],
            [-0.12, 0, 0.08],
            [0.12, 0, 0.08],
        ]
    )
    rng = np.random.default_rng(345)
    frames: list[Observation2DFrame] = []
    for i in range(60):
        t_ms = T0 + i * 300
        for cam in truth_doc["cameras"]:
            people_uvs = []
            for person in range(2):
                a = i * 0.05 + person * np.pi
                X = body + [4.5 + 1.5 * np.cos(a), 3.75 + 1.6 * np.sin(a) + person * 0.6, 0.0]
                uv = cv2.projectPoints(
                    X,
                    np.array(cam["R"]),
                    np.array(cam["t"]),
                    np.array(K),
                    np.array(DIST),
                )[0][:, 0]
                uv += rng.normal(0.0, 0.15, uv.shape)
                people_uvs.append((f"p{person}", uv))
            frames.append(_make_observation_frame(cam["camera_id"], i, t_ms, people_uvs))
    return truth_doc, heights, provenance, frames


def _perturb_camera_rotation(doc: dict, camera_id: str, yaw_deg: float) -> dict:
    perturbed = copy.deepcopy(doc)
    for cam in perturbed["cameras"]:
        if cam["camera_id"] == camera_id:
            R0 = np.asarray(cam["R"], dtype=float)
            center = -R0.T @ np.asarray(cam["t"], dtype=float)
            # 카메라가 실제에서는 truth_doc 방향을 보는데 이전 보정 문서가 -yaw_deg 만큼 잘못 알고 있는 상태
            R_wrong = Rotation.from_euler("y", -yaw_deg, degrees=True).as_matrix() @ R0
            cam["R"] = R_wrong.tolist()
            cam["t"] = (-R_wrong @ center).tolist()
    perturbed["calibration_version"] = f"cal-perturbed-{abs(int(yaw_deg))}deg"
    perturbed["content_hash"] = content_hash(perturbed)
    return perturbed


def test_live_joint_buffer_limits_and_reset_camera(synthetic_rig_and_frames):
    _, _, _, frames = synthetic_rig_and_frames
    cfg = LiveBufferConfig(
        max_duration_ms=3_000,
        max_frames_per_camera=6,
        max_total_samples=24,
        max_bytes=32_768,
        sample_period_ms=300,
        gap_split_ms=1_500,
    )
    buf = LiveJointBuffer(cfg)
    for f in frames[:80]:  # 20개 시각 × 4대 카메라
        assert buf.append_frame(f) is True

    # 같은 epoch 에서 같거나 작은 frame_sequence 는 무시해야 한다.
    assert buf.append_frame(frames[0]) is False

    summary = buf.summary()
    assert summary["total_samples"] <= cfg.max_total_samples
    assert summary["approx_bytes"] <= cfg.max_bytes
    assert all(cnt <= cfg.max_frames_per_camera for cnt in summary["camera_frame_counts"].values())
    span = summary["window_span_ms"]
    assert span is not None and (span[1] - span[0]) <= cfg.max_duration_ms

    # 특정 카메라(cam_D)만 초기화하면 나머지 3대는 그대로 남고 cam_D 만 비워진다.
    removed = buf.reset_camera("cam_D")
    assert removed > 0
    after_reset = buf.summary()
    assert "cam_D" not in after_reset["camera_frame_counts"]
    assert set(after_reset["camera_frame_counts"].keys()) == {"cam_A", "cam_B", "cam_C"}

    tracks = buf.to_session_tracks()
    assert tracks is not None
    assert all(v.camera_id != "cam_D" for _, vs in tracks.ticks for v in vs)


def test_insufficient_data_does_not_propose_candidate(synthetic_rig_and_frames):
    truth_doc, heights, provenance, frames = synthetic_rig_and_frames
    previous = _perturb_camera_rotation(truth_doc, "cam_D", 6.0)
    buf = LiveJointBuffer()
    # 8개 시각(카메라당 8프레임)만 넣으면 최소 데이터 기준(fit 30시각)에 미달한다.
    for f in frames[:32]:
        buf.append_frame(f)

    proposals = []
    runner = LiveCandidateCalibrationRunner(
        buf,
        previous,
        measured_camera_heights=heights,
        input_provenance=provenance,
        propose_calibration=lambda doc, rep: proposals.append((doc, rep)),
        config=LiveCandidateConfig(use_subprocess=False, rounds=2),
    )
    res = runner.run_once(now_ms=T0 + 10_000)
    assert res.status == STATUS_INSUFFICIENT_DATA
    assert res.proposed is False
    assert res.calibration is None
    assert proposals == []
    assert res.report["data_sufficiency"]["usable"] is False
    assert "최소 데이터 기준 미달" in res.report["failure_reason"]


def test_no_meaningful_improvement_when_already_calibrated(synthetic_rig_and_frames):
    truth_doc, heights, provenance, frames = synthetic_rig_and_frames
    buf = LiveJointBuffer()
    for f in frames:
        buf.append_frame(f)

    proposals = []
    runner = LiveCandidateCalibrationRunner(
        buf,
        truth_doc,
        measured_camera_heights=heights,
        input_provenance=provenance,
        propose_calibration=lambda doc, rep: proposals.append((doc, rep)),
        config=LiveCandidateConfig(use_subprocess=False, rounds=2),
    )
    res = runner.run_once(now_ms=T0 + 30_000)
    assert res.status == STATUS_NO_MEANINGFUL_IMPROVEMENT
    assert res.proposed is False
    assert res.calibration is None
    assert proposals == []
    assert res.report["improvement_decision"]["accepted"] is False
    assert "개선 폭 미달" in res.report["failure_reason"]


def test_fast_rotation_path_and_full_session_fallback_with_report_schema(synthetic_rig_and_frames):
    truth_doc, heights, provenance, frames = synthetic_rig_and_frames
    buf = LiveJointBuffer()
    for f in frames:
        buf.append_frame(f)

    # 1) 회전 틀어짐에서 빠른 길(`realign_rotation`)이 동작해 후보를 제안하는지 검증
    perturbed_small = _perturb_camera_rotation(truth_doc, "cam_D", 5.0)
    fast_proposals = []
    runner_fast = LiveCandidateCalibrationRunner(
        buf,
        perturbed_small,
        measured_camera_heights=heights,
        input_provenance=provenance,
        propose_calibration=lambda doc, rep: fast_proposals.append((doc, rep)),
        config=LiveCandidateConfig(use_subprocess=False, try_fast_rotation_first=True, rounds=2),
    )
    res_fast = runner_fast.run_once(now_ms=T0 + 40_000)
    assert res_fast.status == STATUS_PROPOSED
    assert res_fast.proposed is True
    assert res_fast.mode == MODE_FAST_ROTATION
    assert len(fast_proposals) == 1

    # 2) 전체 session 경로(subprocess 포함)와 콜백 보고(report) 스키마 검증
    perturbed_large = _perturb_camera_rotation(truth_doc, "cam_D", 7.0)
    session_proposals = []
    runner_full = LiveCandidateCalibrationRunner(
        buf,
        perturbed_large,
        measured_camera_heights=heights,
        input_provenance=provenance,
        propose_calibration=lambda doc, rep: session_proposals.append((doc, rep)),
        config=LiveCandidateConfig(
            interval_ms=60_000,
            timeout_s=120.0,
            use_subprocess=True,
            try_fast_rotation_first=False,
            rounds=2,
        ),
    )
    res_full = runner_full.run_once(now_ms=T0 + 50_000)
    assert res_full.status == STATUS_PROPOSED, res_full.report.get("failure_reason")
    assert res_full.proposed is True
    assert res_full.mode == MODE_FULL_SESSION
    assert len(session_proposals) == 1

    doc, report = session_proposals[0]
    assert check_calibration(doc) == []
    assert doc["content_hash"] == content_hash(doc)
    assert doc["state"] == "PROVISIONAL"

    # 콜백 보고 dict 스키마 검증 (4단계 명세)
    assert report["format"] == LIVE_CANDIDATE_REPORT_FORMAT
    assert report["format_version"] == LIVE_CANDIDATE_REPORT_VERSION
    assert report["status"] == STATUS_PROPOSED
    assert report["proposed"] is True
    assert report["previous_version"] == perturbed_large["calibration_version"]
    assert report["candidate_version"] == doc["calibration_version"]
    assert report["data_sufficiency"]["usable"] is True
    assert report["data_sufficiency"]["counts"]["fit_ticks"] >= 30
    assert report["data_sufficiency"]["counts"]["holdout_ticks"] >= 20
    assert report["holdout"]["before"]["fusion_4_ratio"] < 0.2
    assert report["holdout"]["after"]["fusion_4_ratio"] > 0.8
    assert report["holdout"]["delta"]["fusion_4_ratio"] > 0.5
    assert report["world_alignment"]["usable"] is True
    assert report["world_alignment"]["anchor_cameras"] == ["cam_A", "cam_B", "cam_C"]
    assert report["improvement_decision"]["accepted"] is True
    assert len(report["improvement_decision"]["triggered_gates"]) >= 1

    # 복구된 cam_D 회전이 정답과 0.3도 이내인지 확인
    d_est = next(c for c in doc["cameras"] if c["camera_id"] == "cam_D")
    d_gt = next(c for c in truth_doc["cameras"] if c["camera_id"] == "cam_D")
    rot_err = float(
        np.degrees(Rotation.from_matrix(np.asarray(d_est["R"]) @ np.asarray(d_gt["R"]).T).magnitude())
    )
    assert rot_err < 0.3

    # interval_ms 안에는 maybe_run 이 실행하지 않아야 한다.
    assert runner_full.maybe_run(now_ms=T0 + 70_000) is None


def test_subprocess_timeout_and_priority_deferral(synthetic_rig_and_frames, tmp_path: Path):
    truth_doc, heights, provenance, frames = synthetic_rig_and_frames
    buf = LiveJointBuffer()
    for f in frames[:20]:
        buf.append_frame(f)

    # 1) AdmissionGate 표시 파일이 없을 때 LIVE 우선 보류(STATUS_DEFERRED)
    gate = AdmissionGate(tmp_path / "missing_tier0.live", wall_s=lambda: 100.0, monotonic=lambda: 100.0)
    runner_deferred = LiveCandidateCalibrationRunner(
        buf,
        truth_doc,
        measured_camera_heights=heights,
        input_provenance=provenance,
        admission_gate=gate,
        config=LiveCandidateConfig(use_subprocess=False),
    )
    res_def = runner_deferred.run_once(now_ms=T0 + 60_000)
    assert res_def.status == STATUS_DEFERRED
    assert res_def.proposed is False

    # 2) 별도 spawn 프로세스가 timeout_s 를 넘기면 즉시 종료하고 STATUS_TIMEOUT 을 돌려준다.
    runner_timeout = LiveCandidateCalibrationRunner(
        buf,
        truth_doc,
        measured_camera_heights=heights,
        input_provenance=provenance,
        config=LiveCandidateConfig(use_subprocess=True, timeout_s=0.5),
    )
    runner_timeout._simulate_sleep_s = 5.0
    res_to = runner_timeout.run_once(now_ms=T0 + 65_000)
    assert res_to.status == STATUS_TIMEOUT
    assert res_to.proposed is False
    assert "시간 상한 초과" in res_to.report["failure_reason"]
