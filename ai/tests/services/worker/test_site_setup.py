"""`site_setup` 워커 파이프라인 로컬 종단 시험 (S15P31A101-465).

GPU·MapAnything 없이 합성 점구름·카메라(가짜 복원기)로 `SiteSetupHandler` 와 `Worker` 전체가
단계 하트비트(단조 증가)·콜백 계약을 지키는지, 카메라 높이 불일치 경고(`height_checks`),
실패 사유(`BACKGROUND_INCOMPLETE`, `FLOOR_FIT_FAILED`, `INSUFFICIENT_FEATURES`, `VALIDATION_REJECTED`,
`LIVE_EXP_BLOCKED`)와 재시도 결정론, 그리고 11차 실측 녹화 캐시 트랙(`tracks.json`) Stage A 연결을 검증한다.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from services.worker.__main__ import build_handlers
from services.worker.client import LeaseClient
from services.worker.handlers import SITE_SETUP_STAGES, JobContext, JobFailed
from services.worker.runner import Outcome, Worker
from services.worker.site_setup import (
    SETUP_BUDGET_CAMERA_CHECK_S,
    SETUP_BUDGET_HEIGHT_REVIEW_S,
    SETUP_BUDGET_MAP_S,
    SETUP_TIME_BUDGET_S,
    BakedScene,
    BakeImageRef,
    BakeImages,
    BakeReclaimed,
    CameraFrameRef,
    CameraPose,
    CleanBackgroundBundle,
    InitialWorldReconstruction,
    ObjectRef,
    PreviewDraftResult,
    PublishedCalibration,
    SiteSetupHandler,
    build_world_from_pointcloud_and_poses,
    world_anchor_summary,
)
from services.worker.transport import Response
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    CAMS,
    PAIRS,
    _derive_new_calibration_version,
    project,
)
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import load_example

CACHED_TRACKS_PATH = Path(
    "C:/Users/SSAFY/simai-local/codex-tmp/449/val01-calib-v2-20261103T153758154194Z/tracks.json"
)
CACHED_INITIAL_CALIB_PATH = Path(
    "C:/Users/SSAFY/simai-local/session11/lobby-bundle-rotfix/calibration.json"
)


class _ScriptedTransport:
    """대여 → 다중 하트비트 → 결과 제출을 기록하고 ACTIVE/ACCEPTED 로 답하는 가짜 전송."""

    def __init__(self, lease_msg: dict[str, Any]) -> None:
        self.lease_msg = lease_msg
        self.leased = False
        self.heartbeats: list[dict[str, Any]] = []
        self.callbacks: list[dict[str, Any]] = []
        self.active_status = load_example("job_lease_status.active.json")
        self.accepted_status = load_example("job_lease_status.accepted.json")

    def post(self, path: str, body: dict[str, Any], timeout_s: float) -> Response:
        if path == "/api/worker/jobs/lease":
            if not self.leased:
                self.leased = True
                return Response(200, self.lease_msg)
            return Response(204, None)
        if path.endswith("/heartbeat"):
            self.heartbeats.append(copy.deepcopy(body))
            return Response(200, self.active_status)
        if path.endswith("/result"):
            self.callbacks.append(copy.deepcopy(body))
            return Response(200, self.accepted_status)
        raise AssertionError(f"Unexpected path: {path}")


def _make_synthetic_room_and_tracks(
    *,
    camera_ids: tuple[str, ...] = ("cam_A", "cam_B", "cam_C", "cam_D"),
    restored_heights_m: dict[str, float] | None = None,
) -> tuple[np.ndarray, dict[str, CameraPose], dict[str, dict[str, Any]], dict[str, Any]]:
    """바닥(Z=0)과 두 수직 벽(X=0, Y=0) 점구름 + 4대 카메라 포즈 + 2-view 배경 특징점 트랙."""
    rng = np.random.default_rng(465)
    # 바닥: X in [0, 6], Y in [0, 6], Z = 0
    floor_xy = rng.uniform([0.2, 0.2], [5.8, 5.8], size=(600, 2))
    floor_pts = np.column_stack([floor_xy, rng.normal(0.0, 0.002, size=600)])
    # 벽 1 (Y=0, 법선 +Y): X in [0.2, 5.8], Z in [0.2, 2.4]
    wall1_xz = rng.uniform([0.2, 0.2], [5.8, 2.4], size=(250, 2))
    wall1_pts = np.column_stack([wall1_xz[:, 0], rng.normal(0.0, 0.002, size=250), wall1_xz[:, 1]])
    # 벽 2 (X=0, 법선 +X): Y in [0.2, 5.8], Z in [0.2, 2.4]
    wall2_yz = rng.uniform([0.2, 0.2], [5.8, 2.4], size=(250, 2))
    wall2_pts = np.column_stack([rng.normal(0.0, 0.002, size=250), wall2_yz[:, 0], wall2_yz[:, 1]])
    points_m = np.vstack([floor_pts, wall1_pts, wall2_pts])

    heights = restored_heights_m or dict.fromkeys(camera_ids, 1.5)
    centers = np.array(
        [
            [0.5, 0.5, heights[camera_ids[0]]],
            [5.5, 0.5, heights[camera_ids[1]]],
            [5.5, 5.5, heights[camera_ids[2]]],
            [0.5, 5.5, heights[camera_ids[3]]],
        ],
        dtype=float,
    )
    target = np.array([3.0, 3.0, 0.9], dtype=float)
    rotations_true = []
    for c in centers:
        forward = target - c
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        down = np.cross(forward, right)
        rotations_true.append(np.vstack([right, down, forward]))
    R_true = np.array(rotations_true, dtype=float)

    # 먼저 기준 포즈로 estimate_world_frame 이 만드는 정규 World 프레임을 구한다.
    base_poses = {
        cid: CameraPose(camera_id=cid, R=R_true[idx], t=-R_true[idx] @ centers[idx])
        for idx, cid in enumerate(camera_ids)
    }
    K_mat = np.array([[1000.0, 0.0, 1152.0], [0.0, 1000.0, 648.0], [0.0, 0.0, 1.0]])
    intrinsics_by_cam = {
        cid: {
            "K": K_mat.tolist(),
            "image_size": (2304, 1296),
            "distortion_model": "OPENCV_PINHOLE",
            "distortion_coeffs": (0.0, 0.0, 0.0, 0.0, 0.0),
        }
        for cid in camera_ids
    }
    canonical = build_world_from_pointcloud_and_poses(
        site_id="site-01",
        camera_ids=camera_ids,
        camera_heights_m=heights,
        height_tolerance_cm=5.0,
        points_m=points_m,
        unscaled_poses=base_poses,
        intrinsics_by_cam=intrinsics_by_cam,
        created_at_ms=1760342400000,
    )
    R_canon = np.array([c["R"] for c in canonical.calibration_doc["cameras"]], dtype=float)
    t_canon = np.array([c["t"] for c in canonical.calibration_doc["cameras"]], dtype=float)
    C_canon = -np.einsum("nji,nj->ni", R_canon, t_canon)
    K_all = np.tile(K_mat, (4, 1, 1))

    # 정규 World 프레임 안에서 2-view 배경 특징점 트랙을 합성한다.
    tracks: list[dict[str, Any]] = []
    pairs_meta: dict[str, Any] = {}
    c_mean = np.mean(C_canon, axis=0)
    for a, b in PAIRS:
        pts_3d = rng.uniform(
            [c_mean[0] - 1.5, c_mean[1] - 1.5, 0.2],
            [c_mean[0] + 1.5, c_mean[1] + 1.5, 1.4],
            size=(32, 3),
        )
        uv_ab = np.stack(
            [project(pts_3d, cam_i, K_all, R_canon, C_canon)[0] for cam_i in (a, b)],
            axis=1,
        )
        for i, uv in enumerate(uv_ab):
            tracks.append(
                {
                    "id": f"{camera_ids[a]}-{camera_ids[b]}:{i}",
                    "pair": [a, b],
                    "uv": uv.tolist(),
                    "nodes": [[a, 1000 * b + i], [b, 1000 * a + i]],
                    "split": "holdout" if i % 4 == 0 else "train",
                }
            )
        pairs_meta[f"{camera_ids[a]}-{camera_ids[b]}"] = {
            "train": 24,
            "holdout": 8,
        }

    # 초기 카메라 회전(0, 1, 2)에 작은 오차를 주어 Stage A 회전 BA 가 수렴·개선하게 만든다.
    R_perturbed = R_true.copy()
    R_perturbed[0] = Rotation.from_euler("y", 1.1, degrees=True).as_matrix() @ R_perturbed[0]
    R_perturbed[1] = Rotation.from_euler("x", 2.0, degrees=True).as_matrix() @ R_perturbed[1]
    R_perturbed[2] = Rotation.from_euler("z", -1.3, degrees=True).as_matrix() @ R_perturbed[2]
    unscaled_poses = {
        cid: CameraPose(camera_id=cid, R=R_perturbed[idx], t=-R_perturbed[idx] @ centers[idx])
        for idx, cid in enumerate(camera_ids)
    }
    return points_m, unscaled_poses, intrinsics_by_cam, {"tracks": tracks, "pairs": pairs_meta}


def test_site_setup_worker_e2e_stage_heartbeats_and_callback_contract() -> None:
    req_msg = load_example("job_request.site_setup.json")
    camera_ids = tuple(req_msg["payload"]["input"]["calibration_target"]["camera_ids"])
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(
        camera_ids=camera_ids
    )
    published_docs: list[dict[str, Any]] = []

    def _collect_bg(*, setup_session_id: str, site_id: str, camera_ids: tuple[str, ...]) -> CleanBackgroundBundle:
        return CleanBackgroundBundle(
            bundle_id="bundle-0123456789abcdef0123456789abcdef",
            collection_pct_by_cam=dict.fromkeys(camera_ids, 100.0),
            tracks_doc=tracks_doc,
        )

    def _init_world(
        *,
        setup_session_id: str,
        site_id: str,
        camera_ids: tuple[str, ...],
        camera_heights_m: dict[str, float],
        height_tolerance_cm: float,
        backgrounds: CleanBackgroundBundle,
        created_at_ms: int,
        progress: Any = None,
    ) -> InitialWorldReconstruction:
        return build_world_from_pointcloud_and_poses(
            site_id=site_id,
            camera_ids=camera_ids,
            camera_heights_m=camera_heights_m,
            height_tolerance_cm=height_tolerance_cm,
            points_m=points_m,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=created_at_ms,
            calibration_version="cal-setup-12-init",
        )

    prefix = "s3://bucket/job-objects/site_setup/job-x/lease-x/"
    preview_ref = ObjectRef(prefix + "preview_scene.glb", "a" * 64, 10)
    scene_ref = ObjectRef(prefix + "scene.glb", "b" * 64, 20)

    def _publish_cal(doc: dict[str, Any]) -> PublishedCalibration:
        published_docs.append(copy.deepcopy(doc))
        version = str(doc["calibration_version"])
        return PublishedCalibration(version, ObjectRef(prefix + f"calibration-{version}.json", "c" * 64, 30))

    handler = SiteSetupHandler(
        background_collector=_collect_bg,
        world_initializer=_init_world,
        calibration_publisher=_publish_cal,
        preview_builder=lambda **kw: PreviewDraftResult(
            preview_scene_version="scene-setup-12-preview",
            zone_drafts_ref="draft-setup-12",
            scene_asset=preview_ref,
        ),
        texture_baker=lambda **kw: BakedScene("scene-setup-12", scene_ref),
        clock_ms=lambda: 1760342410000,
    )

    transport = _ScriptedTransport(req_msg)
    now_s = {"t": 100.0}

    def _mono() -> float:
        now_s["t"] += 0.05
        return now_s["t"]

    client = LeaseClient(
        transport,
        "worker-01",
        clock_ms=lambda: 1760342410000,
        monotonic=_mono,
        sleep=lambda _s: None,
    )
    worker = Worker(
        client,
        {"site_setup": handler},
        clock_ms=lambda: 1760342410000,
        monotonic=_mono,
        sleep=lambda _s: None,
    )

    outcome = worker.run_once()
    assert outcome is Outcome.ACCEPTED

    # 1. 단계 하트비트 단조 증가 및 계약 적합성 확인
    assert len(transport.heartbeats) >= 6
    seen_stages: list[str] = []
    seen_pcts: list[float] = []
    for hb in transport.heartbeats:
        assert schema_errors(hb, "job_heartbeat") == []
        snap = hb["payload"]["site_setup"]
        seen_stages.append(snap["status"])
        seen_pcts.append(float(snap["progress_pct"]))

    expected_stages = [
        "COLLECTING_BACKGROUND",
        "INITIAL_POSE_AND_WORLD",
        "JOINT_BA_AND_VERIFY",
        "PREVIEW_AND_VLM_DRAFT",
        "READY_FOR_ZONES",
        "BAKING_TEXTURE",
    ]
    for stage in expected_stages:
        assert stage in seen_stages
    stage_indices = [SITE_SETUP_STAGES.index(s) for s in seen_stages]
    assert stage_indices == sorted(stage_indices)
    assert seen_pcts == sorted(seen_pcts)

    # 2. 콜백 계약 적합성 및 새 보정 버전·메트릭 확인
    assert len(transport.callbacks) == 1
    cb = transport.callbacks[0]
    assert schema_errors(cb, "job_callback") == []
    assert cb["payload"]["status"] == "SUCCEEDED"
    res = cb["payload"]["result"]
    assert res["status"] == "COMPLETED"
    assert res["progress_pct"] == 100
    assert res["calibration_version"] != "cal-setup-12-init"
    assert res["preview_scene_version"] == "scene-setup-12-preview"
    assert res["scene_version"] == "scene-setup-12"
    assert res["zone_drafts_ref"] == "draft-setup-12"
    assert set(res["height_checks"].keys()) == set(camera_ids)
    assert all(v["within_tolerance"] is True for v in res["height_checks"].values())
    assert "metrics" in res
    assert set(res["metrics"]["stage_seconds"].keys()) == set(expected_stages)
    assert res["metrics"]["joint_ba"]["converged"] is True
    assert len(published_docs) == 1
    assert schema_errors(published_docs[0], "calibration") == []

    # 3. 산출물 객체 참조(artifacts, site_setup 1.2.0): 보정 등록 단계부터 싣고, 미리보기·정식 GLB 를 차례로 더한다
    by_stage = {hb["payload"]["site_setup"]["status"]: hb["payload"]["site_setup"] for hb in transport.heartbeats}
    cal_ref = {"uri": prefix + f"calibration-{res['calibration_version']}.json", "sha256": "c" * 64, "size_bytes": 30}
    assert "artifacts" not in by_stage["INITIAL_POSE_AND_WORLD"]
    assert by_stage["JOINT_BA_AND_VERIFY"]["artifacts"] == {"calibration_document": cal_ref}
    assert by_stage["READY_FOR_ZONES"]["artifacts"] == {"calibration_document": cal_ref, "preview_scene": preview_ref.to_json()}
    assert res["artifacts"] == {"calibration_document": cal_ref, "preview_scene": preview_ref.to_json(),
                                "scene": scene_ref.to_json()}


def test_site_setup_height_mismatch_marks_camera_out_of_tolerance() -> None:
    req_msg = load_example("job_request.site_setup.json")
    camera_ids = tuple(req_msg["payload"]["input"]["calibration_target"]["camera_ids"])
    # cam_B 의 실제 높이가 1.59m 로 입력(1.50m)과 +9cm 차이(> ±5cm 허용 오차)가 나도록 설정
    restored = {"cam_A": 1.49, "cam_B": 1.59, "cam_C": 1.48, "cam_D": 1.47}
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(
        camera_ids=camera_ids,
        restored_heights_m=restored,
    )

    handler = SiteSetupHandler(
        background_collector=lambda **kw: CleanBackgroundBundle(
            bundle_id="bundle-height-mismatch",
            collection_pct_by_cam=dict.fromkeys(camera_ids, 100.0),
            tracks_doc=tracks_doc,
        ),
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"],
            camera_ids=kw["camera_ids"],
            camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"],
            points_m=points_m,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=kw["created_at_ms"],
        ),
        clock_ms=lambda: 1760342410000,
        # 높이 표시(±5 cm) 시험이다. 품질 게이트의 높이 상한(기본 10 cm, -646)은 아래 따로 시험한다
        quality_gate={"height_max_diff_cm": 20.0},
        height_review_wait_s=0,  # 높이 빠른 확인(-690)을 기다리지 않는다(아래 따로 시험)
    )

    ctx = JobContext(
        job_id="job-height-check",
        lease_id="0192a6f0-7c1e-7b3a-9f2d-000000000311",
        job_type="site_setup",
        attempt=1,
        input=req_msg["payload"]["input"],
    )
    result = handler(ctx)
    assert schema_errors(result, {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"}) == []
    hc = result["height_checks"]
    assert hc["cam_B"]["within_tolerance"] is False
    assert hc["cam_B"]["difference_cm"] > 5.0
    assert hc["cam_A"]["within_tolerance"] is True


def test_site_setup_failures_and_deterministic_retry() -> None:
    req_msg = load_example("job_request.site_setup.json")
    inp = req_msg["payload"]["input"]
    camera_ids = tuple(inp["calibration_target"]["camera_ids"])
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(
        camera_ids=camera_ids
    )

    # (1) BACKGROUND_INCOMPLETE -> retryable=True
    state = {"ready": False}

    def _flaky_bg(**kw: Any) -> CleanBackgroundBundle:
        if not state["ready"]:
            return CleanBackgroundBundle(
                bundle_id="bundle-incomplete",
                collection_pct_by_cam={"cam_A": 100.0, "cam_B": 60.0, "cam_C": 100.0, "cam_D": 100.0},
                tracks_doc=tracks_doc,
            )
        return CleanBackgroundBundle(
            bundle_id="bundle-ready",
            collection_pct_by_cam=dict.fromkeys(camera_ids, 100.0),
            tracks_doc=tracks_doc,
        )

    handler = SiteSetupHandler(
        background_collector=_flaky_bg,
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"],
            camera_ids=kw["camera_ids"],
            camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"],
            points_m=points_m,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=1760342410000,
        ),
        clock_ms=lambda: 1760342410000,
    )

    ctx1 = JobContext("job-1", "0192a6f0-7c1e-7b3a-9f2d-000000000311", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc_bg:
        handler(ctx1)
    assert exc_bg.value.code == "BACKGROUND_INCOMPLETE"
    assert exc_bg.value.retryable is True

    # 재시도 시 성공하고, 같은 입력으로 두 번 돌리면 동일한 버전·결과가 나온다(결정론)
    state["ready"] = True
    ctx2 = JobContext("job-1", "0192a6f0-7c1e-7b3a-9f2d-000000000312", "site_setup", 2, inp)
    ctx3 = JobContext("job-1", "0192a6f0-7c1e-7b3a-9f2d-000000000313", "site_setup", 2, inp)
    res_a = dict(handler(ctx2))
    res_b = dict(handler(ctx3))
    res_a.pop("metrics", None)
    res_b.pop("metrics", None)
    assert res_a == res_b

    # (2) FLOOR_FIT_FAILED -> retryable=False (수직 점들만 있어 수평 바닥이 없는 경우)
    bad_points = np.column_stack(
        [
            np.zeros(400),
            np.linspace(0.0, 5.0, 400),
            np.linspace(0.0, 3.0, 400),
        ]
    )
    handler_floor_fail = SiteSetupHandler(
        background_collector=_flaky_bg,
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"],
            camera_ids=kw["camera_ids"],
            camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"],
            points_m=bad_points,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=1760342410000,
        ),
    )
    with pytest.raises(JobFailed) as exc_floor:
        handler_floor_fail(
            JobContext("job-2", "0192a6f0-7c1e-7b3a-9f2d-000000000314", "site_setup", 1, inp)
        )
    assert exc_floor.value.code == "FLOOR_FIT_FAILED"
    assert exc_floor.value.retryable is False

    # (3) INSUFFICIENT_FEATURES -> retryable=False
    handler_no_feat = SiteSetupHandler(
        background_collector=lambda **kw: CleanBackgroundBundle(
            bundle_id="bundle-empty-tracks",
            collection_pct_by_cam=dict.fromkeys(camera_ids, 100.0),
            tracks_doc={"tracks": [], "pairs": {}},
        ),
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"],
            camera_ids=kw["camera_ids"],
            camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"],
            points_m=points_m,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=1760342410000,
        ),
    )
    with pytest.raises(JobFailed) as exc_feat:
        handler_no_feat(
            JobContext("job-3", "0192a6f0-7c1e-7b3a-9f2d-000000000315", "site_setup", 1, inp)
        )
    assert exc_feat.value.code == "INSUFFICIENT_FEATURES"
    assert exc_feat.value.retryable is False

    # (4) LIVE_EXP_BLOCKED -> LIVE 중 고화질 텍스처 굽기 금지 게이트
    handler_live_blocked = SiteSetupHandler(
        background_collector=_flaky_bg,
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"],
            camera_ids=kw["camera_ids"],
            camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"],
            points_m=points_m,
            unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam,
            created_at_ms=1760342410000,
        ),
        live_gate=lambda: True,
    )
    ctx_live = JobContext("job-4", "0192a6f0-7c1e-7b3a-9f2d-000000000316", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc_live:
        handler_live_blocked(ctx_live)
    assert exc_live.value.code == "LIVE_EXP_BLOCKED"
    assert exc_live.value.retryable is True
    # READY_FOR_ZONES 까지는 정상 보고되었고 BAKING_TEXTURE 직전에 차단되었음을 확인
    assert [s["status"] for s in ctx_live.site_setup_history][-1] == "READY_FOR_ZONES"


def test_job_context_rejects_non_monotonic_site_setup_progress() -> None:
    ctx = JobContext("job-mono", "0192a6f0-7c1e-7b3a-9f2d-000000000311", "site_setup", 1, {})
    ctx.report_site_setup(
        {
            "setup_session_id": "0192a6f0-7c1e-7b3a-9f2d-000000000452",
            "status": "INITIAL_POSE_AND_WORLD",
            "progress_pct": 40,
        }
    )
    with pytest.raises(ValueError, match="되돌아갈 수 없다"):
        ctx.report_site_setup(
            {
                "setup_session_id": "0192a6f0-7c1e-7b3a-9f2d-000000000452",
                "status": "COLLECTING_BACKGROUND",
                "progress_pct": 50,
            }
        )
    with pytest.raises(ValueError, match="감소할 수 없다"):
        ctx.report_site_setup(
            {
                "setup_session_id": "0192a6f0-7c1e-7b3a-9f2d-000000000452",
                "status": "JOINT_BA_AND_VERIFY",
                "progress_pct": 35,
            }
        )


def test_build_handlers_registers_site_setup_when_provided() -> None:
    dummy = lambda _ctx: {}
    handlers = build_handlers(None, site_setup=dummy)
    assert handlers.get("site_setup") is dummy


@pytest.mark.skipif(
    not (CACHED_TRACKS_PATH.exists() and CACHED_INITIAL_CALIB_PATH.exists()),
    reason="Real VAL-01 cached tracks or session11 calibration not present on this host",
)
def test_site_setup_worker_with_real_val01_cached_tracks() -> None:
    import json

    cal_doc = json.loads(CACHED_INITIAL_CALIB_PATH.read_text(encoding="utf-8"))
    tracks_doc = json.loads(CACHED_TRACKS_PATH.read_text(encoding="utf-8"))
    heights_m = {"cam_A": 2.21, "cam_B": 2.195, "cam_C": 2.23, "cam_D": 2.24}
    restored = {c["camera_id"]: float(c["camera_height_m"]) for c in cal_doc["cameras"]}

    req_msg = copy.deepcopy(load_example("job_request.site_setup.json"))
    req_msg["payload"]["input"]["site_id"] = "lobby"
    req_msg["payload"]["input"]["camera_heights_m"] = heights_m
    req_msg["payload"]["input"]["calibration_target"]["camera_ids"] = list(CAMS)

    handler = SiteSetupHandler(
        background_collector=lambda **kw: CleanBackgroundBundle(
            bundle_id="bundle-session11-lobby",
            collection_pct_by_cam=dict.fromkeys(CAMS, 100.0),
            tracks_doc=tracks_doc,
        ),
        world_initializer=lambda **kw: InitialWorldReconstruction(
            calibration_doc=copy.deepcopy(cal_doc),
            restored_heights_m=restored,
            height_checks={},
        ),
        clock_ms=lambda: 1793687375000,
        height_review_wait_s=0,
    )

    transport = _ScriptedTransport(req_msg)
    client = LeaseClient(
        transport,
        "worker-01",
        clock_ms=lambda: 1793687375000,
        monotonic=lambda: 10.0,
        sleep=lambda _s: None,
    )
    worker = Worker(
        client,
        {"site_setup": handler},
        clock_ms=lambda: 1793687375000,
        monotonic=lambda: 10.0,
        sleep=lambda _s: None,
    )
    assert worker.run_once() is Outcome.ACCEPTED
    assert len(transport.callbacks) == 1
    cb = transport.callbacks[0]
    assert schema_errors(cb, "job_callback") == []
    res = cb["payload"]["result"]
    assert res["calibration_version"] != cal_doc["calibration_version"]
    assert res["metrics"]["joint_ba"]["converged"] is True
    assert res["metrics"]["joint_ba"]["stage_seconds"] < 10.0
    assert res["metrics"]["joint_ba"]["valid_points"] == 318


# ---------------------------------------------------------------------------- S15P31A101-473
def _room_handler(collector: Any, **kw: Any) -> tuple[SiteSetupHandler, dict[str, Any], list[dict[str, Any]]]:
    """합성 방으로 끝까지 도는 처리기. 올린 보정 문서를 모은다."""
    inp = load_example("job_request.site_setup.json")["payload"]["input"]
    camera_ids = tuple(inp["calibration_target"]["camera_ids"])
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(camera_ids=camera_ids)
    published: list[dict[str, Any]] = []

    def _publish(doc: dict[str, Any]) -> str:
        published.append(copy.deepcopy(doc))
        return str(doc["calibration_version"])

    handler = SiteSetupHandler(
        background_collector=lambda **c: collector(tracks_doc, camera_ids),
        world_initializer=lambda **w: build_world_from_pointcloud_and_poses(
            site_id=w["site_id"], camera_ids=w["camera_ids"], camera_heights_m=w["camera_heights_m"],
            height_tolerance_cm=w["height_tolerance_cm"], points_m=points_m, unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam, created_at_ms=w["created_at_ms"],
        ),
        **{"calibration_publisher": _publish, **kw},
    )
    return handler, inp, published


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, event: Any, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_site_setup_subprogress_is_real_monotonic_unique_and_rate_limited() -> None:
    t = _FakeTime()
    handler = SiteSetupHandler(background_collector=lambda **kw: None, world_initializer=lambda **kw: None,
                               monotonic=t.monotonic)
    inp = load_example("job_request.site_setup.json")["payload"]["input"]
    ctx = JobContext("job-progress", "lease-progress", "site_setup", 1, inp)
    base = {"setup_session_id": inp["setup_session_id"], "status": "INITIAL_POSE_AND_WORLD", "progress_pct": 15}
    handler._emit_progress(ctx, base)
    report = handler._subprogress(ctx, base, 15, 39)
    report(0.25)  # 같은 초의 하위 보고는 보내지 않는다.
    assert len(ctx.site_setup_history) == 1
    t.now = 1
    report(0.25)
    t.now = 2
    report(0.25)
    report(0.1)  # 중복·역순 콜백은 버린다.
    report(0.5)
    t.now = 2.5
    report(0.75)
    t.now = 3
    report(0.75)
    t.now = 1000  # 시간만으로 진행률을 올리지 않는다.
    assert [s["progress_pct"] for s in ctx.site_setup_history] == [15, 21, 27, 33]
    handler._emit_progress(ctx, ctx.site_setup_history[-1])
    assert len(ctx.site_setup_history) == 4


def test_site_setup_background_progress_follows_slowest_camera() -> None:
    inp = load_example("job_request.site_setup.json")["payload"]["input"]
    cams = tuple(inp["calibration_target"]["camera_ids"])
    bundles = iter(CleanBackgroundBundle("b", {**dict.fromkeys(cams, 100), cams[-1]: slow}, tracks_doc={})
                   for slow in (20, 50, 100))
    t = _FakeTime()
    handler = SiteSetupHandler(background_collector=lambda **kw: next(bundles), world_initializer=lambda **kw: None,
                               background_wait_s=10, monotonic=t.monotonic, sleep=t.sleep)
    ctx = JobContext("job-bg", "lease-bg", "site_setup", 1, inp)
    handler._wait_for_backgrounds(ctx, inp["setup_session_id"], inp["site_id"], cams)
    assert [s["progress_pct"] for s in ctx.site_setup_history] == [2.8, 7]
    assert ctx.site_setup_history[0]["background_collection_pct"] == {**dict.fromkeys(cams, 100), cams[-1]: 20}


def test_site_setup_subprogress_reaches_each_stage_and_preserves_milestones() -> None:
    t = _FakeTime()
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100), tracks_doc=td),
        monotonic=t.monotonic,
    )
    original = handler._init_world

    def initialize(**kw: Any) -> InitialWorldReconstruction:
        for fraction in (0.2, 0.4, 0.6, 0.8, 1):
            t.now += 1
            kw["progress"](fraction)
        return original(**kw)

    def preview(**kw: Any) -> PreviewDraftResult:
        t.now += 1
        kw["progress"](0.5)
        t.now += 1
        kw["progress"](1)
        return PreviewDraftResult("scene-preview-test")

    def bake(**kw: Any) -> str:
        for fraction in (0.25, 0.75, 1):
            t.now += 1
            kw["progress"](fraction)
        return "scene-baked-test"

    handler._init_world, handler._build_preview, handler._bake_texture = initialize, preview, bake
    # 최적화의 실제 경계 콜백도 시간 간격 제한을 통과하게 시계를 주입한다.
    def tick() -> float:
        t.now += 1
        return t.now
    handler._monotonic = tick
    ctx = JobContext("job-progress", "lease-progress", "site_setup", 1, inp)
    result = handler(ctx)
    pcts = [s["progress_pct"] for s in ctx.site_setup_history]
    assert pcts == sorted(pcts)
    assert {15, 40, 65, 80, 85, 95} <= set(pcts)
    for lo, hi in ((15, 40), (40, 65), (65, 80), (85, 95)):
        assert any(lo < p < hi for p in pcts)
    assert result["progress_pct"] == 100
    assert all(schema_errors(s, _PROGRESS) == [] for s in ctx.site_setup_history)


def test_background_wait_reports_collection_and_continues_when_bundle_completes() -> None:
    """묶음이 완성될 때까지 기다리며 카메라별 수집률을 COLLECTING_BACKGROUND 로 보내고, 완성되면 그대로 진행한다."""
    calls = {"n": 0}

    def collector(tracks_doc: Any, cams: tuple[str, ...]) -> CleanBackgroundBundle:
        calls["n"] += 1
        if calls["n"] == 1:
            return CleanBackgroundBundle("", {c: 25.0 for c in cams})
        if calls["n"] == 2:  # 활성 보정이 바뀌어 수집을 새로 시작하면 수집률은 내려갈 수 있다(진행률은 단조)
            return CleanBackgroundBundle("", dict.fromkeys(cams, 10.0))
        if calls["n"] == 3:
            return CleanBackgroundBundle("", dict.fromkeys(cams, 99.0))
        return CleanBackgroundBundle("bundle-x", dict.fromkeys(cams, 100.0), tracks_doc=tracks_doc,
                                     observed_until_ms=1760342400123)

    t = _FakeTime()
    handler, inp, _ = _room_handler(collector, background_wait_s=240.0, background_poll_s=2.0,
                                    monotonic=t.monotonic, sleep=t.sleep, clock_ms=lambda: 1760342410000)
    ctx = JobContext("job-wait", "0192a6f0-7c1e-7b3a-9f2d-000000000321", "site_setup", 1, inp)
    result = handler(ctx)
    assert result["status"] == "COMPLETED" and t.sleeps == [2.0, 2.0, 2.0]
    collecting = [s for s in ctx.site_setup_history if s["status"] == "COLLECTING_BACKGROUND"]
    assert [s["progress_pct"] for s in collecting] == [3.5, 3.5, 13.9, 15]
    assert [set(s["background_collection_pct"].values()) for s in collecting] == [{25.0}, {10.0}, {99.0}, {100.0}]
    for snap in collecting:
        assert schema_errors(snap, {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/progress"}) == []


def test_background_wait_times_out_retryably_and_honours_cancel() -> None:
    t = _FakeTime()
    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("", dict.fromkeys(cams, 50.0)),
                                    background_wait_s=10.0, background_poll_s=4.0, monotonic=t.monotonic, sleep=t.sleep)
    ctx = JobContext("job-wait", "0192a6f0-7c1e-7b3a-9f2d-000000000322", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "BACKGROUND_INCOMPLETE" and exc.value.retryable is True
    assert sum(t.sleeps) == pytest.approx(10.0)  # 마지막 대기는 남은 시간만큼
    assert ctx.site_setup_history[-1]["status"] == "COLLECTING_BACKGROUND"

    def cancel_on_sleep(event: Any, seconds: float) -> None:
        cancelled_ctx.cancelled.set()

    handler2, _, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("", dict.fromkeys(cams, 50.0)),
                                   background_wait_s=240.0, sleep=cancel_on_sleep)
    cancelled_ctx = JobContext("job-wait", "0192a6f0-7c1e-7b3a-9f2d-000000000323", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc2:
        handler2(cancelled_ctx)
    assert exc2.value.code == "CANCELLED"


def _capture(cams: tuple[str, ...], counts: dict[str, int], states: dict[str, str] | None = None) -> dict[str, Any]:
    return {"since_ms": 1760342400000, "required_samples": 12, "sample_interval_ms": 1_000, "cameras": {
        c: {"state": (states or {}).get(c, "READY"), "sample_count": counts[c], "person_visible": False} for c in cams}}


def test_background_camera_not_ready_never_enters_stage_a_and_names_the_camera() -> None:
    """묶음이 있어도 촬영 상태에 INSUFFICIENT_SAMPLES 카메라가 남으면 Stage A 로 가지 않고 시한 뒤 그 카메라로 멈춘다(-711)."""
    t = _FakeTime()

    def collector(td: Any, cams: tuple[str, ...]) -> CleanBackgroundBundle:
        counts = {**dict.fromkeys(cams, 48), cams[-1]: 11}
        return CleanBackgroundBundle("bundle-x", dict.fromkeys(cams, 100.0), tracks_doc=td,
                                     capture=_capture(cams, counts, {cams[-1]: "INSUFFICIENT_SAMPLES"}))

    handler, inp, published = _room_handler(collector, background_wait_s=10.0, background_poll_s=4.0,
                                            monotonic=t.monotonic, sleep=t.sleep)
    ctx = JobContext("job-bg-thin", "0192a6f0-7c1e-7b3a-9f2d-000000000324", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    cam_d = inp["calibration_target"]["camera_ids"][-1]
    assert exc.value.code == "BACKGROUND_INCOMPLETE" and exc.value.retryable is True
    assert f"'{cam_d}': 'INSUFFICIENT_SAMPLES 11/12'" in exc.value.message
    assert {s["status"] for s in ctx.site_setup_history} == {"COLLECTING_BACKGROUND"} and published == []
    assert all(schema_errors(s, _PROGRESS) == [] for s in ctx.site_setup_history)


def test_nonfinite_stage_a_triangulation_fails_as_validation_rejected_not_handler_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """16차: Stage A 의 비유한 삼각측량이 HANDLER_ERROR 로 처리기를 죽였다. 이제 VALIDATION_REJECTED 와 원인 후보(-711)."""
    import simai_ai.calibration.extrinsics.joint_ba_v2 as jba

    original = jba.triangulate

    def nan_for_b_c(uv: Any, pair: Any, *a: Any, **kw: Any) -> np.ndarray:
        xyz = original(uv, pair, *a, **kw)
        return np.full_like(xyz, np.nan) if tuple(pair) == (1, 2) else xyz

    monkeypatch.setattr(jba, "triangulate", nan_for_b_c)

    def collector(td: Any, cams: tuple[str, ...]) -> CleanBackgroundBundle:
        counts = {**dict.fromkeys(cams, 48), cams[-1]: 12}  # 최소 12장에 겨우 닿은 카메라
        return CleanBackgroundBundle("bundle-x", dict.fromkeys(cams, 100.0), tracks_doc=td,
                                     capture=_capture(cams, counts))

    handler, inp, published = _room_handler(collector)
    cams = inp["calibration_target"]["camera_ids"]
    ctx = JobContext("job-nonfinite", "0192a6f0-7c1e-7b3a-9f2d-000000000325", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "VALIDATION_REJECTED" and exc.value.retryable is False
    assert f"nonfinite_pairs=['{cams[1]}-{cams[2]}']" in exc.value.message  # 화면이 이 쌍을 뽑아 보인다(setupFailures.ts)
    assert f"배경 표본이 적은 카메라: {cams[-1]} READY 12/12" in exc.value.message
    assert len(exc.value.message) <= 480 and published == []


def test_bake_runs_while_tier0_marker_is_live_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """세팅은 진행 중 작업이 없을 때만 시작하므로 Tier 0 표시 파일(live_notice)이 있어도 굽는다(재시도 → 서버 400 방지)."""
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        live_notice=lambda: True,
    )
    ctx = JobContext("job-live", "0192a6f0-7c1e-7b3a-9f2d-000000000324", "site_setup", 1, inp)
    with caplog.at_level("WARNING", logger="services.worker"):
        result = handler(ctx)
    assert result["status"] == "COMPLETED" and "BAKING_TEXTURE" in [s["status"] for s in ctx.site_setup_history]
    assert "Tier 0 가 떠 있지만 세팅 굽기를 진행" in caplog.text


_RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"}


def test_work_started_before_bake_skips_bake_and_completes_on_preview() -> None:
    """굽기 전에 같은 현장 작업이 시작됐으면(EC2 live_work_active) 정식 Scene 없이 COMPLETED(-597, 명세 3.2 5항).

    운영 굽기 도구(ProcessStages)는 신호가 이미 켜져 있으면 자식을 띄우지 않고 BakeReclaimed 를 던진다(stages 시험). 여기서는 그 동작을 흉내 낸다.
    """
    ctx_box: list[JobContext] = []

    def _bake(**kw: Any) -> BakedScene:
        assert ctx_box[0].live_work.is_set()
        raise BakeReclaimed("bake")

    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        texture_baker=_bake,
    )
    ctx = JobContext("job-reclaim-0", "0192a6f0-7c1e-7b3a-9f2d-000000000325", "site_setup", 1, inp)
    ctx_box.append(ctx)
    ctx.live_work.set()
    result = handler(ctx)
    assert result["status"] == "COMPLETED" and result["scene_version"] is None
    assert result["bake_skipped"] == "LIVE_WORK_STARTED" and "scene" not in result.get("artifacts", {})
    assert schema_errors(result, _RESULT_SCHEMA) == []


def test_work_started_during_bake_reclaims_and_completes_on_preview() -> None:
    """굽는 중 작업 시작: 굽기 도구가 자식을 끝내고 BakeReclaimed 를 던지면 실패가 아니라 정식 Scene 없는 COMPLETED 다."""
    ctx_box: list[JobContext] = []

    def _bake(**kw: Any) -> BakedScene:
        ctx_box[0].live_work.set()  # 굽는 사이 하트비트가 신호를 받았다
        raise BakeReclaimed("bake")

    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        texture_baker=_bake,
    )
    ctx = JobContext("job-reclaim-1", "0192a6f0-7c1e-7b3a-9f2d-000000000326", "site_setup", 1, inp)
    ctx_box.append(ctx)
    result = handler(ctx)
    assert result["scene_version"] is None and result["bake_skipped"] == "LIVE_WORK_STARTED"
    assert "BAKING_TEXTURE" in [s["status"] for s in ctx.site_setup_history]
    assert result["metrics"]["stage_seconds"]["BAKING_TEXTURE"] >= 0
    assert schema_errors(result, _RESULT_SCHEMA) == []


def test_lease_cancel_during_bake_is_still_a_failure_not_a_reclaim() -> None:
    """대여 취소·시간 초과는 회수가 아니다: 작업 시작 신호가 함께 켜져 있어도 굽기 도구의 CANCELLED 실패는 그대로 올라간다.

    운영 굽기 도구는 취소가 켜져 있으면 BakeReclaimed 가 아니라 JobFailed(CANCELLED)를 던진다(stages 시험).
    """
    ctx_box: list[JobContext] = []

    def _bake(**kw: Any) -> BakedScene:
        ctx_box[0].live_work.set()
        ctx_box[0].cancelled.set()
        raise JobFailed("CANCELLED", "대여 취소 또는 시간 초과로 중단됨", retryable=True)

    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        texture_baker=_bake,
    )
    ctx = JobContext("job-reclaim-2", "0192a6f0-7c1e-7b3a-9f2d-000000000327", "site_setup", 1, inp)
    ctx_box.append(ctx)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "CANCELLED"


def test_retry_with_same_bundle_publishes_identical_calibration_document() -> None:
    """재시도(새 대여·다른 시계)여도 같은 묶음이면 같은 버전·같은 문서 바이트 → 서버가 같은 sha256 으로 받는다."""
    import json

    clock = iter(range(1760342410000, 1760342410000 + 10_000_000, 7919))
    handler, inp, published = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td, observed_until_ms=1760342399000),
        clock_ms=lambda: next(clock),
    )
    for lease in ("0192a6f0-7c1e-7b3a-9f2d-000000000325", "0192a6f0-7c1e-7b3a-9f2d-000000000326"):
        handler(JobContext("job-retry", lease, "site_setup", 1, inp))
    first, second = (json.dumps(d, sort_keys=True) for d in published)
    assert first == second and published[0]["created_at"] == 1760342399000


def test_stage_a_version_changes_when_content_changes() -> None:
    """같은 버전이면 같은 내용: 회전이 같아도 내용(content_hash)이 다르면 다른 버전이 된다."""
    R = np.stack([np.eye(3)] * 2)
    base = _derive_new_calibration_version("cal-x", R, ["a", "b"], "sha-1")
    assert base == _derive_new_calibration_version("cal-x", R, ["a", "b"], "sha-1")
    assert base != _derive_new_calibration_version("cal-x", R, ["a", "b"], "sha-2")
    assert base.startswith("cal-x-jba2-")


# ---------------------------------------------------------------------------- S15P31A101-479
def _frame_publisher(prefix: str = "s3://bucket/job-objects/site_setup/job-f/lease-f/") -> Any:
    def publish(*, setup_session_id: str, camera_ids: tuple[str, ...], backgrounds: CleanBackgroundBundle) -> list[CameraFrameRef]:
        return [CameraFrameRef(cid, ObjectRef(f"{prefix}frame-{cid}.jpg", f"{idx + 1:x}" * 64, 1000 + idx))
                for idx, cid in enumerate(camera_ids)]
    return publish


def test_camera_frames_ride_the_preview_snapshot_so_the_server_can_queue_the_draft() -> None:
    """프레임은 미리보기 스냅샷(서버가 구역 초안 작업을 만드는 그 스냅샷)부터 artifacts.camera_frames 로 실리고 결과까지 이어진다."""
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        frame_publisher=_frame_publisher(),
        preview_builder=lambda **kw: PreviewDraftResult("scene-p", scene_asset=ObjectRef("s3://bucket/job-objects/site_setup/job-f/lease-f/p.glb", "a" * 64, 5)),
    )
    ctx = JobContext("job-f", "0192a6f0-7c1e-7b3a-9f2d-000000000479", "site_setup", 1, inp)
    result = handler(ctx)
    by_stage = {s["status"]: s for s in ctx.site_setup_history}
    camera_ids = inp["calibration_target"]["camera_ids"]
    assert "camera_frames" not in by_stage["JOINT_BA_AND_VERIFY"].get("artifacts", {})
    frames = by_stage["PREVIEW_AND_VLM_DRAFT"]["artifacts"]["camera_frames"]
    assert [f["camera_id"] for f in frames] == camera_ids
    assert all(f["face_policy"] == "BLURRED" and f["uri"].endswith(f"frame-{f['camera_id']}.jpg") for f in frames)
    assert by_stage["READY_FOR_ZONES"]["artifacts"]["camera_frames"] == frames
    assert result["artifacts"]["camera_frames"] == frames and "preview_scene" in result["artifacts"]
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/progress"}) == []
    assert schema_errors(result, {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"}) == []


def test_without_frame_publisher_artifacts_have_no_camera_frames() -> None:
    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td))
    result = handler(JobContext("job-f", "0192a6f0-7c1e-7b3a-9f2d-000000000480", "site_setup", 1, inp))
    assert "artifacts" not in result


def test_frame_upload_failure_does_not_fail_setup_but_cancel_still_does(caplog: pytest.LogCaptureFixture) -> None:
    """프레임은 AI 의견용 보조 산출물: 올리지 못하면 프레임 없이 끝낸다(경고). 취소는 그대로 취소한다."""
    def failing(**kw: Any) -> list[CameraFrameRef]:
        raise JobFailed("SETUP_OBJECT_FAILED", "5xx", retryable=True)

    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                    frame_publisher=failing)
    ctx = JobContext("job-f", "0192a6f0-7c1e-7b3a-9f2d-000000000481", "site_setup", 1, inp)
    with caplog.at_level("WARNING", logger="services.worker"):
        result = handler(ctx)
    assert result["status"] == "COMPLETED" and "artifacts" not in result
    assert "프레임 없이 진행" in caplog.text

    def cancel_then_fail(**kw: Any) -> list[CameraFrameRef]:
        ctx2.cancelled.set()
        raise JobFailed("SETUP_OBJECT_FAILED", "5xx", retryable=True)

    handler2, _, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                   frame_publisher=cancel_then_fail)
    ctx2 = JobContext("job-f", "0192a6f0-7c1e-7b3a-9f2d-000000000482", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler2(ctx2)
    assert exc.value.code == "CANCELLED"


def test_frame_refs_outside_the_session_cameras_are_dropped_and_capped() -> None:
    def odd(*, setup_session_id: str, camera_ids: tuple[str, ...], backgrounds: CleanBackgroundBundle) -> list[CameraFrameRef]:
        ref = ObjectRef("s3://bucket/job-objects/site_setup/j/l/f.jpg", "a" * 64, 1)
        return [CameraFrameRef("camX", ref), CameraFrameRef(camera_ids[0], ref), CameraFrameRef(camera_ids[0], ref)]

    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                    frame_publisher=odd)
    result = handler(JobContext("job-f", "0192a6f0-7c1e-7b3a-9f2d-000000000483", "site_setup", 1, inp))
    assert [f["camera_id"] for f in result["artifacts"]["camera_frames"]] == [inp["calibration_target"]["camera_ids"][0]]


# ---------------------------------------------------------------------------- S15P31A101-584
def test_world_anchor_hook_feeds_the_same_document_to_publish_preview_and_bake() -> None:
    """Stage A 뒤 World 다시 맞추기를 한 번만 하고, 등록·미리보기·굽기가 같은(옮긴) 문서를 받는다."""
    seen: dict[str, Any] = {}

    def anchor(doc: Any) -> dict[str, Any]:
        out = copy.deepcopy(dict(doc))
        out["world_anchor"] = {"accepted": False, "reason": "INSUFFICIENT_CAMERAS"}
        seen["anchor_calls"] = seen.get("anchor_calls", 0) + 1
        return out

    def preview(**kw: Any) -> PreviewDraftResult:
        seen["preview"] = kw["calibration_doc"]
        return PreviewDraftResult("scene-preview-x")

    def bake(**kw: Any) -> str:
        seen["bake"] = kw["calibration_doc"]
        return "scene-x"

    handler, inp, published = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=anchor, preview_builder=preview, texture_baker=bake,
    )
    result = handler(JobContext("job-anchor", "0192a6f0-7c1e-7b3a-9f2d-000000000584", "site_setup", 1, inp))
    assert result["status"] == "COMPLETED" and seen["anchor_calls"] == 1
    assert published[0]["world_anchor"]["reason"] == "INSUFFICIENT_CAMERAS"
    assert seen["preview"]["world_anchor"] == seen["bake"]["world_anchor"] == published[0]["world_anchor"]


# ---------------------------------------------------------------------------- S15P31A101-593
_BAKE_BUNDLE = "bundle-3f6c2b1a9d8e4f70a1b2c3d4e5f60718"
_PROGRESS = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/progress"}
_RESULT = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"}


def _bake_publisher(bundle_id: str = _BAKE_BUNDLE, extra: tuple[str, ...] = ()) -> Any:
    def publish(*, setup_session_id: str, camera_ids: tuple[str, ...], backgrounds: CleanBackgroundBundle) -> BakeImages:
        refs = [BakeImageRef(cid, ObjectRef(f"s3://bucket/job-objects/site_setup/j/l/bake-{cid}.jpg", f"{idx + 1:x}" * 64, 9000 + idx))
                for idx, cid in enumerate((*extra, *camera_ids, camera_ids[0]))]
        return BakeImages(bundle_id, tuple(refs))
    return publish


def test_bake_images_ride_the_preview_snapshot_before_the_preview_goes_live() -> None:
    """굽기 입력 사진은 미리보기 스냅샷(서버가 미리보기를 현재 Scene 으로 올리기 전)부터 실려 결과까지 간다.
    세션 밖·중복 카메라는 버리고 기본 얼굴 정책은 OBSERVED."""
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        bake_image_publisher=_bake_publisher(extra=("camX",)),
    )
    ctx = JobContext("job-b", "0192a6f0-7c1e-7b3a-9f2d-000000000593", "site_setup", 1, inp)
    result = handler(ctx)
    by_stage = {s["status"]: s for s in ctx.site_setup_history}
    camera_ids = inp["calibration_target"]["camera_ids"]
    assert "bake_images" not in by_stage["JOINT_BA_AND_VERIFY"].get("artifacts", {})
    bake = by_stage["PREVIEW_AND_VLM_DRAFT"]["artifacts"]["bake_images"]
    assert by_stage["READY_FOR_ZONES"]["artifacts"]["bake_images"] == by_stage["BAKING_TEXTURE"]["artifacts"]["bake_images"] == bake
    assert bake["bundle_id"] == _BAKE_BUNDLE
    assert [i["camera_id"] for i in bake["images"]] == camera_ids
    assert all(i["face_policy"] == "OBSERVED" for i in bake["images"])
    assert result["artifacts"]["bake_images"] == bake
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_bake_image_failure_or_unknown_bundle_never_fails_setup(caplog: pytest.LogCaptureFixture) -> None:
    """VDT 사진은 장식 층: 저장소 오류·예상 밖 예외·형식 모를 묶음 ID 는 경고만 남기고 사진 없이 끝낸다. 취소는 그대로 취소한다."""
    def collector(td: Any, cams: tuple[str, ...]) -> CleanBackgroundBundle:
        return CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td)

    def store_down(**kw: Any) -> BakeImages:
        raise JobFailed("SETUP_OBJECT_FAILED", "5xx", retryable=True)

    def boom(**kw: Any) -> BakeImages:
        raise RuntimeError("예상 밖")

    for publisher in (store_down, boom, _bake_publisher(bundle_id="bundle-latest")):
        handler, inp, _ = _room_handler(collector, bake_image_publisher=publisher)
        with caplog.at_level("WARNING", logger="services.worker"):
            result = handler(JobContext("job-b", "0192a6f0-7c1e-7b3a-9f2d-000000000594", "site_setup", 1, inp))
        assert result["status"] == "COMPLETED" and "artifacts" not in result
    assert "VDT 사진 없이" in caplog.text and "묶음 ID 형식을 몰라" in caplog.text

    def cancel_then_fail(**kw: Any) -> BakeImages:
        ctx2.cancelled.set()
        raise JobFailed("SETUP_OBJECT_FAILED", "5xx", retryable=True)

    handler2, inp2, _ = _room_handler(collector, bake_image_publisher=cancel_then_fail)
    ctx2 = JobContext("job-b", "0192a6f0-7c1e-7b3a-9f2d-000000000595", "site_setup", 1, inp2)
    with pytest.raises(JobFailed) as exc:
        handler2(ctx2)
    assert exc.value.code == "CANCELLED"


def test_full_setup_result_carries_supplement_summary_from_baker() -> None:
    """S15P31A101-605: 굽기 주입점이 돌려준 보조 촬영 요약이 COMPLETED 결과 supplement 로 실린다(계약 통과)."""
    from simai_ai.scene import supplement_capture as sc

    summary = sc.summary(status="FALLBACK", reason=sc.R_TOO_FEW_REGISTERED,
                         media=[sc.MediaInput("sup-01", "VIDEO", None)], frames_selected=40, frames_registered=3)
    handler, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        texture_baker=lambda **kw: BakedScene("scene-sup", None, summary),
    )
    inp = load_example("job_request.site_setup_supplement.json")["payload"]["input"]
    result = handler(JobContext("job-sup", "0192a6f0-7c1e-7b3a-9f2d-000000000605", "site_setup", 1, inp))
    assert result["supplement"] == summary and result["scene_version"] == "scene-sup"
    assert "mode" not in result


# ---------------------------------------------------------------------------- S15P31A101-606
def _floor_coverage(calibration_version: str) -> dict[str, Any]:
    return {"calibration_version": calibration_version, "cell_m": 0.3, "origin_xy": [0.0, 0.0], "cols": 2, "rows": 2,
            "cells": ".102", "ankle_height_m": 0.1, "head_height_m": 1.7, "floor_source": "MAPANYTHING_FLOOR_POINTS",
            "is_upper_bound": True}


def test_floor_coverage_rides_the_preview_snapshot_with_its_calibration_version() -> None:
    """바닥 감시 범위는 등록한 보정 버전으로 센 것만 미리보기 스냅샷부터 결과까지 실린다."""
    def preview(**kw: Any) -> PreviewDraftResult:
        return PreviewDraftResult("scene-p", floor_coverage=_floor_coverage(kw["calibration_doc"]["calibration_version"]))

    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                    preview_builder=preview)
    ctx = JobContext("job-c", "0192a6f0-7c1e-7b3a-9f2d-000000000606", "site_setup", 1, inp)
    result = handler(ctx)
    by_stage = {s["status"]: s for s in ctx.site_setup_history}
    assert "floor_coverage" not in by_stage["JOINT_BA_AND_VERIFY"].get("artifacts", {})
    coverage = by_stage["PREVIEW_AND_VLM_DRAFT"]["artifacts"]["floor_coverage"]
    assert coverage["calibration_version"] == result["calibration_version"]
    assert by_stage["BAKING_TEXTURE"]["artifacts"]["floor_coverage"] == result["artifacts"]["floor_coverage"] == coverage
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_floor_coverage_of_another_calibration_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    handler, inp, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                    preview_builder=lambda **kw: PreviewDraftResult("scene-p", floor_coverage=_floor_coverage("cal-old")))
    with caplog.at_level("WARNING", logger="services.worker"):
        result = handler(JobContext("job-c", "0192a6f0-7c1e-7b3a-9f2d-000000000607", "site_setup", 1, inp))
    assert result["status"] == "COMPLETED" and "artifacts" not in result
    assert "보정 버전이 등록한 보정과 달라" in caplog.text


# ---------------------------------------------------------------------------- S15P31A101-614
_REF_CAL = "cal-lobby-init-079f00cd-jba2-55083185"


def _anchor_hook(accepted: bool, reason: str = "ANCHORED", per_camera: bool = False) -> Any:
    """World 다시 맞추기(-584) 주입점 대역: 문서에 world_anchor 만 붙인다(채택이면 실제로는 R·t 도 옮겨진다).
    per_camera 면 한도와 카메라별 배치 잔차를 함께 싣는다(cam_B 만 한도 0.15 m 초과)."""
    def anchor(doc: Any) -> dict[str, Any]:
        out = copy.deepcopy(dict(doc))
        out["world_anchor"] = {
            "accepted": accepted, "reason": reason, "reference_calibration_version": _REF_CAL,
            "method": "CAMERA_CENTER_YAW_XY", "max_center_residual_m": 0.0612 if accepted else 0.4127,
            "max_orientation_change_rad": 0.0703,
        }
        if per_camera:
            out["world_anchor"]["limits"] = {"max_center_residual_m": 0.15, "max_scale_deviation": 0.03,
                                             "max_orientation_change_rad": 0.1745, "min_cameras": 3}
            out["world_anchor"]["center_residuals_m"] = {"cam_A": 0.02, "cam_B": 0.4127, "cam_C": 0.03, "cam_D": 0.01}
        return out
    return anchor


def _doc_publisher(published: list[dict[str, Any]]) -> Any:
    def publish(doc: Any) -> PublishedCalibration:
        published.append(copy.deepcopy(dict(doc)))
        version = str(doc["calibration_version"])
        return PublishedCalibration(version, ObjectRef.of(f"s3://bucket/job-objects/site_setup/j/l/calibration-{version}.json",
                                                          version.encode()))
    return publish


def _reuse_input() -> dict[str, Any]:
    """계약 예시 job_request.site_setup_reuse 의 mode 를 합성 방 입력(job_request.site_setup)에 얹는다."""
    mode = load_example("job_request.site_setup_reuse.json")["payload"]["input"]["mode"]
    assert mode == "REUSE_SCENE"
    return {**load_example("job_request.site_setup.json")["payload"]["input"], "mode": mode}


def _must_not_run(name: str) -> Any:
    def run(**kw: Any) -> Any:
        raise AssertionError(f"REUSE_SCENE 채택이면 {name} 를 부르지 않는다")
    return run


def test_reuse_scene_accepted_skips_preview_and_bake_and_completes_with_calibration_only() -> None:
    """REUSE_SCENE + World 다시 맞추기 채택: 배경·보정은 그대로 하고 JOINT_BA → READY_FOR_ZONES → COMPLETED.
    미리보기·프레임·굽기 사진·굽기는 부르지 않고, 보정 문서 artifacts·높이 확인·world_anchor 를 싣는다."""
    published: list[dict[str, Any]] = []
    handler, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(True), calibration_publisher=_doc_publisher(published),
        preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
        frame_publisher=_must_not_run("카메라 프레임"), bake_image_publisher=_must_not_run("굽기 입력 사진"),
    )
    ctx = JobContext("job-reuse", "0192a6f0-7c1e-7b3a-9f2d-000000000614", "site_setup", 1, _reuse_input())
    result = handler(ctx)

    stages = list(dict.fromkeys(s["status"] for s in ctx.site_setup_history))
    assert stages == ["COLLECTING_BACKGROUND", "INITIAL_POSE_AND_WORLD", "JOINT_BA_AND_VERIFY", "READY_FOR_ZONES"]
    assert {15, 40, 65, 85} <= {s["progress_pct"] for s in ctx.site_setup_history}
    assert all(s["mode"] == "REUSE_SCENE" for s in ctx.site_setup_history)
    assert result["mode"] == "REUSE_SCENE" and result["status"] == "COMPLETED" and result["progress_pct"] == 100
    assert "preview_scene_version" not in result and "scene_version" not in result and "reuse_fallback_reason" not in result
    assert result["calibration_version"] == published[0]["calibration_version"]
    assert set(result["artifacts"]) == {"calibration_document"}
    assert result["world_anchor"] == {"accepted": True, "reason": "ANCHORED", "reference_calibration_version": _REF_CAL,
                                      "max_center_residual_m": 0.0612, "max_orientation_change_rad": 0.0703}
    assert set(result["height_checks"]) == set(_reuse_input()["calibration_target"]["camera_ids"])
    assert set(result["metrics"]["stage_seconds"]) == {"COLLECTING_BACKGROUND", "INITIAL_POSE_AND_WORLD",
                                                        "JOINT_BA_AND_VERIFY", "READY_FOR_ZONES"}
    ready = ctx.site_setup_history[-1]
    assert ready["world_anchor"] == result["world_anchor"] and ready["artifacts"] == result["artifacts"]
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_reuse_scene_rejected_anchor_stops_with_changed_cameras_instead_of_new_map() -> None:
    """World 다시 맞추기가 거부되면(카메라 이동) 새 맵으로 저절로 넘어가지 않는다(S15P31A101-689): REUSE_SCENE 그대로 거부 요약
    (한도를 넘은 카메라 changed_cameras)을 실은 스냅샷을 보내고, 새 보정을 등록하지 않은 채 REUSE_WORLD_REJECTED 로 실패한다."""
    published: list[dict[str, Any]] = []
    handler, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(False, "CENTER_RESIDUAL_EXCEEDED", per_camera=True),
        calibration_publisher=_doc_publisher(published),
        preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
        frame_publisher=_must_not_run("카메라 프레임"), bake_image_publisher=_must_not_run("굽기 입력 사진"),
    )
    ctx = JobContext("job-reuse-rj", "0192a6f0-7c1e-7b3a-9f2d-000000000615", "site_setup", 1, _reuse_input())
    with pytest.raises(JobFailed) as exc:
        handler(ctx)

    assert exc.value.code == "REUSE_WORLD_REJECTED" and exc.value.retryable is False
    assert "cam_B" in str(exc.value) and "새 맵" in str(exc.value)
    assert published == []  # Tier 0 는 지금 보정을 그대로 쓴다
    assert all(s["mode"] == "REUSE_SCENE" for s in ctx.site_setup_history)
    assert all("reuse_fallback_reason" not in s for s in ctx.site_setup_history)
    last = ctx.site_setup_history[-1]
    assert last["status"] == "JOINT_BA_AND_VERIFY" and 61 < last["progress_pct"] < 65
    assert last["world_anchor"]["accepted"] is False and last["world_anchor"]["changed_cameras"] == ["cam_B"]
    assert "artifacts" not in last and "calibration_version" not in last
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []


def test_reuse_scene_without_anchor_record_stops_with_no_reference() -> None:
    """보정 문서에 world_anchor 가 없으면(비교할 기준 보정 없음) 같은 World 라고 지어내지 않고, 새 맵으로 넘어가지도 않는다:
    NO_REFERENCE 거부 요약(달라진 카메라 없음)을 싣고 실패한다."""
    handler, _, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
                                  texture_baker=_must_not_run("굽기"))
    ctx = JobContext("job-reuse-none", "0192a6f0-7c1e-7b3a-9f2d-000000000616", "site_setup", 1, _reuse_input())
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "REUSE_WORLD_REJECTED" and "비교할 수 없어" in str(exc.value)
    assert ctx.site_setup_history[-1]["world_anchor"] == {
        "accepted": False, "reason": "NO_REFERENCE", "reference_calibration_version": None, "changed_cameras": []}
    assert "BAKING_TEXTURE" not in [s["status"] for s in ctx.site_setup_history]
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []


def test_rejected_anchor_summary_lists_only_cameras_over_the_limits() -> None:
    """changed_cameras 는 카메라별 배치 잔차·방향 변화가 문서 한도를 넘은 카메라만, ID 오름차순. 한도가 없으면 빈 목록(지어내지 않음)."""
    rec = {"accepted": False, "reason": "ORIENTATION_CHANGE_EXCEEDED", "reference_calibration_version": _REF_CAL,
           "limits": {"max_center_residual_m": 0.15, "max_scale_deviation": 0.03, "max_orientation_change_rad": 0.17,
                      "min_cameras": 3},
           "center_residuals_m": {"cam_D": 0.2, "cam_A": 0.01},
           "orientation_changes_rad": {"cam_C": 0.5, "cam_A": 0.02, "cam_D": 0.3}}
    assert world_anchor_summary({"world_anchor": rec})["changed_cameras"] == ["cam_C", "cam_D"]
    no_limits = {k: v for k, v in rec.items() if k != "limits"}
    assert world_anchor_summary({"world_anchor": no_limits})["changed_cameras"] == []
    accepted = {**rec, "accepted": True, "reason": "ANCHORED"}
    assert "changed_cameras" not in world_anchor_summary({"world_anchor": accepted})


def test_full_setup_reports_world_anchor_summary_without_mode() -> None:
    """FULL_SETUP 도 World 다시 맞추기 결과를 요약해 싣는다(세팅 완료 sameWorld 근거). mode 는 지금처럼 싣지 않는다."""
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(True),
    )
    ctx = JobContext("job-full-anchor", "0192a6f0-7c1e-7b3a-9f2d-000000000617", "site_setup", 1, inp)
    result = handler(ctx)
    assert "mode" not in result and "reuse_fallback_reason" not in result
    assert result["world_anchor"]["accepted"] is True and result["scene_version"]
    assert "world_anchor" not in {k for s in ctx.site_setup_history[:2] for k in s}
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


# --------------------------------------------------------------------------- S15P31A101-646 보정 품질 게이트·카메라 확인


def test_quality_gate_height_mismatch_fails_with_problem_camera_snapshot() -> None:
    """줄자 높이와 복원 높이 차가 품질 게이트 상한(기본 10 cm)을 넘으면 보정을 등록하지 않고 VALIDATION_REJECTED 로 끝낸다.
    끝내기 전 스냅샷에 문제 카메라·사유를 실은 quality_gate(FAILED)를 보낸다(서버가 세션 조회에 싣는다)."""
    req_msg = load_example("job_request.site_setup.json")
    camera_ids = tuple(req_msg["payload"]["input"]["calibration_target"]["camera_ids"])
    restored = {"cam_A": 1.49, "cam_B": 1.59, "cam_C": 1.48, "cam_D": 1.47}
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(
        camera_ids=camera_ids, restored_heights_m=restored)
    published: list[Any] = []
    handler = SiteSetupHandler(
        background_collector=lambda **kw: CleanBackgroundBundle("bundle-qg", dict.fromkeys(camera_ids, 100.0),
                                                                tracks_doc=tracks_doc),
        world_initializer=lambda **kw: build_world_from_pointcloud_and_poses(
            site_id=kw["site_id"], camera_ids=kw["camera_ids"], camera_heights_m=kw["camera_heights_m"],
            height_tolerance_cm=kw["height_tolerance_cm"], points_m=points_m, unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam, created_at_ms=kw["created_at_ms"]),
        calibration_publisher=lambda doc: published.append(doc) or str(doc["calibration_version"]),
        clock_ms=lambda: 1760342410000,
        height_review_wait_s=0,  # 확인 없이 진행한 높이(TIMED_OUT)는 품질 게이트 높이 상한을 그대로 본다(-690)
    )
    ctx = JobContext("job-qg", "0192a6f0-7c1e-7b3a-9f2d-000000000646", "site_setup", 1, req_msg["payload"]["input"])
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "VALIDATION_REJECTED" and "cam_B" in exc.value.message
    assert published == []  # 기준 미달 보정은 등록하지 않는다
    last = ctx.site_setup_history[-1]
    assert schema_errors(last, _PROGRESS) == []
    assert last["status"] == "JOINT_BA_AND_VERIFY" and "calibration_version" not in last
    gate = last["quality_gate"]
    assert gate["verdict"] == "FAILED"
    assert gate["cameras"]["cam_B"]["verdict"] == "FAIL" and gate["cameras"]["cam_B"]["reason"] == "HEIGHT_MISMATCH"
    assert any(p["kind"] == "CAMERA" and p["id"] == "cam_B" and p["reason"] == "HEIGHT_MISMATCH" for p in gate["problems"])


def test_quality_gate_unit_rules() -> None:
    from simai_ai.calibration.extrinsics.joint_ba_v2 import evaluate_quality_gate

    def pair(tracks: int, median: float | None, p90: float | None) -> dict[str, Any]:
        return {"holdout": tracks, "camera_ids": ["a", "b"], "after_holdout": {"median": median, "p90": p90}}

    ok_heights = {"a": {"difference_cm": 4.3}, "b": {"difference_cm": -2.5}}
    gate = evaluate_quality_gate({"a-b": pair(20, 0.45, 0.85)}, ok_heights)
    assert gate["verdict"] == "PASSED" and gate["problems"] == []
    gate = evaluate_quality_gate({"a-b": pair(2, 4.1, 4.8)}, ok_heights)
    # 근거가 모자라면 통과가 아니라 확인 필요
    assert gate["verdict"] == "NEEDS_CHECK" and gate["pairs"]["a-b"]["reason"] == "FEW_HOLDOUT"
    gate = evaluate_quality_gate({"a-b": pair(20, 3.5, 4.0)}, ok_heights)
    assert gate["verdict"] == "FAILED" and gate["pairs"]["a-b"]["reason"] == "HOLDOUT_ERROR_HIGH"
    gate = evaluate_quality_gate({"a-b": pair(20, 1.0, 6.5)}, ok_heights)
    assert gate["pairs"]["a-b"]["verdict"] == "FAIL"  # p90 상한
    gate = evaluate_quality_gate({"a-b": pair(20, None, None)}, ok_heights)
    assert gate["pairs"]["a-b"]["reason"] == "HOLDOUT_INVALID"
    gate = evaluate_quality_gate({"a-b": pair(20, 0.5, 0.9)}, {"a": {"difference_cm": 10.4}, "b": {"difference_cm": 0}})
    assert gate["verdict"] == "FAILED" and gate["cameras"]["a"]["reason"] == "HEIGHT_MISMATCH"
    gate = evaluate_quality_gate({"a-b": pair(20, 0.5, 0.9)}, {"a": {"difference_cm": 10.4}}, {"height_max_diff_cm": 12})
    assert gate["verdict"] == "PASSED" and gate["thresholds"]["height_max_diff_cm"] == 12.0


class _FakeChecker:
    """카메라 확인 실행기 대역: 시도마다 poll 이 준비한 상태를 하나씩 낸다(마지막 값은 되풀이). 호출 순서를 events 에 남긴다."""

    def __init__(self, attempts: list[list[str]], *, events: list[str] | None = None, lines_read: int = 10,
                 start_error: Exception | None = None) -> None:
        self.attempts = [list(a) for a in attempts]
        self.statuses: list[str] = []
        self.started: list[tuple[str, str, list[Any], str | None]] = []
        self.blocking: list[list[Any]] = []
        self.stopped = 0
        self.incompletes = 0
        self.version = ""
        self.lines_read = lines_read
        self.start_error = start_error
        self.events = events if events is not None else []

    def _summary(self, status: str) -> dict[str, Any]:
        problems = [] if status in ("WAITING", "MEASURING", "PASSED") else [
            {"kind": "CAMERA", "id": "cam_D", "camera_ids": ["cam_D"],
             "reason": "FEW_SAMPLES" if status == "INCOMPLETE" else "REPROJECTION_HIGH",
             "message": "cam_D 에서 사람이 충분히 보이지 않았습니다"}]
        return {"status": status, "calibration_version": self.version, "progress_pct": 100.0 if status == "PASSED" else 10.0,
                "elapsed_s": 1.0, "thresholds": {"min_duration_s": 30.0, "max_duration_s": 120.0, "min_pair_samples": 30,
                                                 "min_pair_pass_rate": 0.75, "max_pair_residual_median_px": 8.0,
                                                 "min_camera_views": 30, "min_loo_samples": 20, "max_loo_median_px": 12.0,
                                                 "min_evaluated_pairs": 3},
                "pairs": {}, "cameras": {"cam_D": {"observed_views": 3, "loo_samples": 0, "loo_median_px": None,
                                                   "loo_p95_px": None, "verdict": "NOT_OBSERVED", "person_visible": True}},
                "problems": problems}

    def idle(self, calibration_doc: Any, *, required_pairs: Any = (), blocking_pairs: Any = ()) -> Any:
        self.version = str(calibration_doc["calibration_version"])
        return self._summary("WAITING")

    def start(self, setup_session_id: str, calibration_doc: Any, *, expires_at_ms: int, required_pairs: Any = (),
              blocking_pairs: Any = (), attempt_id: str | None = None) -> Any:
        if self.start_error is not None:
            raise self.start_error
        self.version = str(calibration_doc["calibration_version"])
        self.started.append((setup_session_id, self.version, list(required_pairs), attempt_id))
        self.blocking.append(sorted(blocking_pairs))
        self.statuses = self.attempts.pop(0) if self.attempts else ["MEASURING"]
        self.events.append(f"start:{attempt_id}")
        return self._summary("WAITING")

    def poll(self) -> Any:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status == "PASSED":
            self.events.append("passed")
        return self._summary(status)

    def incomplete(self) -> Any:
        self.incompletes += 1
        return self._summary("INCOMPLETE")

    def stop(self) -> None:
        self.stopped += 1


def _recording(name: str, events: list[str], fn: Any) -> Any:
    def run(**kw: Any) -> Any:
        events.append(name)
        return fn(**kw)
    return run


def _cc_room(checker: _FakeChecker, events: list[str], t: _FakeTime, **kw: Any) -> tuple[SiteSetupHandler, dict[str, Any]]:
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, camera_check_poll_s=1.0, monotonic=t.monotonic, sleep=t.sleep,
        preview_builder=_recording("preview", events, SiteSetupHandler._default_preview_builder),
        texture_baker=_recording("bake", events, SiteSetupHandler._default_texture_baker),
        **kw,
    )
    return handler, inp


def _press(ctx_box: dict[str, JobContext], t: _FakeTime, plan: dict[float, int]) -> Any:
    """가짜 잠: 시각이 plan 의 키를 지나면 그 번호로 관리자 「걷기 시작」·「확인만 다시」를 누른다(하트비트 응답 대역)."""
    def sleep(event: Any, seconds: float) -> None:
        t.sleeps.append(seconds)
        t.now += seconds
        for at, attempt in sorted(plan.items()):
            if t.now >= at:
                ctx_box["ctx"].note_camera_check_start(attempt)
    return sleep


def _checks(ctx: JobContext) -> list[dict[str, Any]]:
    return [s["camera_check"] for s in ctx.site_setup_history if "camera_check" in s]


def test_camera_check_waits_for_walk_start_and_passes_before_any_map_step() -> None:
    """보정 등록(65%) 뒤 AWAITING_START + user_wait 로 「걷기 시작」을 기다리고, 시도 1 이 통과해야 미리보기·굽기를 부른다."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["MEASURING", "PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {5.0: 1})
    ctx = box["ctx"] = JobContext("job-cc", "0192a6f0-7c1e-7b3a-9f2d-000000000646", "site_setup", 1, inp,
                                  timeout_ms=1_800_000)
    result = handler(ctx)

    assert events == ["start:0192a6f0-1", "passed", "preview", "bake"]
    checks = _checks(ctx)
    assert checks[0]["status"] == "AWAITING_START" and "attempt" not in checks[0]
    # 실행 상한 1,800 s(-688)면 맵 몫(600 s)을 남기고도 카메라 확인 몫(600 s)을 그대로 쓴다
    assert checks[0]["user_wait"]["wait_s"] == pytest.approx(SETUP_BUDGET_CAMERA_CHECK_S, abs=1.0)
    started = next(c for c in checks if c.get("attempt"))
    assert started["attempt"]["no"] == 1 and started["attempt"]["id"] == "0192a6f0-1" and "user_wait" not in started
    assert started["attempt"]["calibration_version"] == result["calibration_version"]
    first_map = next(i for i, s in enumerate(ctx.site_setup_history) if s["status"] == "PREVIEW_AND_VLM_DRAFT")
    assert all(s["camera_check"]["status"] == "PASSED" for s in ctx.site_setup_history[first_map:])
    assert all(s["status"] == "JOINT_BA_AND_VERIFY" for s in ctx.site_setup_history if "camera_check" in s
               and s["camera_check"]["status"] != "PASSED")
    assert result["camera_check"]["status"] == "PASSED" and result["camera_check"]["attempt"]["no"] == 1
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []
    assert checker.stopped >= 1
    # 새 맵 세팅은 막는 쌍을 넘기지 않아 대각을 포함한 모든 쌍이 판정에 든다(-760. -759 의 참고 쌍은 REUSE_SCENE 만)
    assert checker.blocking == [[]]


def test_camera_check_retry_keeps_the_calibration_and_ignores_a_late_old_request() -> None:
    """시도 1 이 기하 실패(FAILED)면 user_wait 를 다시 싣고 「확인만 다시」를 기다린다. 늦게 온 같은 번호(1)는 무시하고 2 에서 같은
    보정으로 다시 잰다. 실패한 시도 동안 맵은 굽지 않는다."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["MEASURING", "FAILED"], ["PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1, 30.0: 1, 60.0: 2})
    ctx = box["ctx"] = JobContext("job-cc-retry", "0192a6f0-7c1e-7b3a-9f2d-000000000647", "site_setup", 1, inp,
                                  timeout_ms=900_000)
    result = handler(ctx)

    assert events == ["start:0192a6f0-1", "start:0192a6f0-2", "passed", "preview", "bake"]
    assert len(checker.started) == 2 and checker.started[0][1] == checker.started[1][1] == result["calibration_version"]
    failed = next(c for c in _checks(ctx) if c["status"] == "FAILED")
    assert failed["attempt"]["no"] == 1 and failed["user_wait"]["wait_s"] > 0
    assert failed["problems"][0]["reason"] == "REPROJECTION_HIGH"
    assert result["camera_check"]["attempt"]["no"] == 2 and "user_wait" not in result["camera_check"]


def test_camera_check_attempt_cap_closes_as_incomplete_with_sample_shortage_then_retry_passes() -> None:
    """시도 상한까지 판정을 못 하면 그 시도는 INCOMPLETE(표본 부족)로 닫고 다시 확인을 기다린다."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["MEASURING"], ["PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t, camera_check_attempt_s=20.0)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1, 40.0: 2})
    ctx = box["ctx"] = JobContext("job-cc-cap", "0192a6f0-7c1e-7b3a-9f2d-000000000648", "site_setup", 1, inp)
    handler(ctx)

    assert checker.incompletes == 1
    incomplete = next(c for c in _checks(ctx) if c["status"] == "INCOMPLETE")
    assert incomplete["problems"][0]["reason"] == "FEW_SAMPLES" and incomplete["user_wait"]
    assert events[-2:] == ["preview", "bake"]


def test_camera_check_replay_skip_records_skipped_replay_and_goes_to_the_map() -> None:
    """녹화 재생 시험(-736): EC2 가 하트비트 응답으로 건너뛰기를 알리고 있으면 걷기를 열지 않고 SKIPPED_REPLAY(관리자 대기 없음)를 남긴 뒤
    미리보기·굽기로 간다."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t)
    ctx = JobContext("job-cc-replay", "0192a6f0-7c1e-7b3a-9f2d-000000000736", "site_setup", 1, inp, timeout_ms=1_800_000)
    ctx.note_camera_check_skip("REPLAY_TEST")
    result = handler(ctx)

    assert events == ["preview", "bake"] and checker.started == []
    checks = _checks(ctx)
    assert checks and all(c["status"] == "SKIPPED_REPLAY" for c in checks)
    assert "user_wait" not in checks[0] and "attempt" not in checks[0] and checks[0]["problems"] == []
    assert checks[0]["calibration_version"] == result["calibration_version"]
    assert result["camera_check"]["status"] == "SKIPPED_REPLAY"
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_camera_check_replay_skip_arriving_while_waiting_for_walk_start_skips() -> None:
    """걷기 시작을 기다리는 동안 건너뛰기가 오면(스위치를 켜고 녹화 파일로 바꾼 뒤) 그때 SKIPPED_REPLAY 로 맵 단계로 간다(-736)."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t)
    box: dict[str, JobContext] = {}

    def sleep(event: Any, seconds: float) -> None:
        t.sleeps.append(seconds)
        t.now += seconds
        if t.now >= 5.0:
            box["ctx"].note_camera_check_skip("REPLAY_TEST")
    handler._sleep = sleep
    ctx = box["ctx"] = JobContext("job-cc-replay2", "0192a6f0-7c1e-7b3a-9f2d-000000000737", "site_setup", 1, inp,
                                  timeout_ms=1_800_000)
    result = handler(ctx)

    checks = _checks(ctx)
    assert checks[0]["status"] == "AWAITING_START"
    assert checks[-1]["status"] == "SKIPPED_REPLAY" and "user_wait" not in checks[-1]
    assert events == ["preview", "bake"] and result["camera_check"]["status"] == "SKIPPED_REPLAY"


def test_camera_check_without_replay_skip_still_waits_for_the_walk() -> None:
    """건너뛰기가 없으면(스위치 끔·실제 카메라) 지금처럼 걷기 시작을 기다린다(-736 의 기본 동작)."""
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]])
    handler, inp = _cc_room(checker, [], t)
    ctx = JobContext("job-cc-noskip", "0192a6f0-7c1e-7b3a-9f2d-000000000738", "site_setup", 1, inp, timeout_ms=1_800_000)
    ctx.note_camera_check_skip(None)
    with pytest.raises(JobFailed) as e:
        handler(ctx)
    assert e.value.code == "CAMERA_CHECK_NOT_PASSED"
    assert all(c["status"] != "SKIPPED_REPLAY" for c in _checks(ctx))


def test_camera_check_without_walk_start_fails_with_zero_bakes() -> None:
    """대기 마감까지 「걷기 시작」이 없으면 CAMERA_CHECK_NOT_PASSED 로 끝나고 미리보기·굽기를 한 번도 부르지 않는다."""
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]])
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, camera_check_wait_s=60.0, monotonic=t.monotonic, sleep=t.sleep,
        preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
        frame_publisher=_must_not_run("카메라 프레임"), bake_image_publisher=_must_not_run("굽기 입력 사진"),
    )
    ctx = JobContext("job-cc-idle", "0192a6f0-7c1e-7b3a-9f2d-000000000649", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "CAMERA_CHECK_NOT_PASSED" and not exc.value.retryable
    assert checker.started == []
    last = _checks(ctx)[-1]
    assert last["status"] == "INCOMPLETE" and last["problems"][0]["reason"] == "NOT_STARTED" and "user_wait" not in last
    assert schema_errors(ctx.site_setup_history[-1], _PROGRESS) == []
    assert max(s["progress_pct"] for s in ctx.site_setup_history) == 65


@pytest.mark.parametrize("case", ["no_views", "start_error"])
def test_camera_check_channel_failure_fails_with_zero_bakes(case: str) -> None:
    """확인 채널을 열지 못하면(시작 오류, Tier 0 view 가 한 줄도 오지 않음) CAMERA_CHECK_UNAVAILABLE 이고 맵을 굽지 않는다."""
    t = _FakeTime()
    checker = _FakeChecker([["WAITING"]], lines_read=0,
                           start_error=OSError("camera-check 폴더 쓰기 실패") if case == "start_error" else None)
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, monotonic=t.monotonic,
        preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
    )
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {0.5: 1})
    ctx = box["ctx"] = JobContext("job-cc-chan", "0192a6f0-7c1e-7b3a-9f2d-000000000650", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "CAMERA_CHECK_UNAVAILABLE" and not exc.value.retryable
    assert checker.stopped >= 1


def test_camera_check_budget_leaves_the_map_share_of_the_execution_limit() -> None:
    """실행 상한이 예산 합보다 짧으면(옛 EC2 등) 대기는 실행 상한에서 맵 몫(NEW 600 s, -688)을 뺀 만큼으로 줄인다. 그만큼도 안 남으면
    확인을 열지 않고 맵 없이 실패한다(맵 몫은 사람 대기에 내주지 않는다)."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]], events=events)
    handler, inp = _cc_room(checker, events, t)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1})
    ctx = box["ctx"] = JobContext("job-cc-budget", "0192a6f0-7c1e-7b3a-9f2d-000000000651", "site_setup", 1, inp,
                                  timeout_ms=780_000)
    handler(ctx)
    wait = _checks(ctx)[0]["user_wait"]["wait_s"]
    assert 150.0 < wait <= 180.0  # 780 − 600 − 앞 단계 시간

    checker = _FakeChecker([["PASSED"]])
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
    )
    ctx = JobContext("job-cc-short", "0192a6f0-7c1e-7b3a-9f2d-000000000652", "site_setup", 1, inp, timeout_ms=450_000)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "CAMERA_CHECK_NOT_PASSED" and checker.started == []


def test_reuse_scene_checks_cameras_before_ready_for_zones() -> None:
    """REUSE_SCENE 채택도 카메라 확인을 통과해야 READY_FOR_ZONES·결과로 간다(맵 몫 대신 결과 제출 몫만 남긴다)."""
    events: list[str] = []
    t = _FakeTime()
    checker = _FakeChecker([["PASSED"]], events=events)
    handler, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(True), calibration_publisher=_doc_publisher([]), camera_check=checker,
        monotonic=t.monotonic, preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
    )
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1})
    ctx = box["ctx"] = JobContext("job-cc-reuse", "0192a6f0-7c1e-7b3a-9f2d-000000000653", "site_setup", 1, _reuse_input(),
                                  timeout_ms=600_000)
    result = handler(ctx)
    assert _checks(ctx)[0]["user_wait"]["wait_s"] == pytest.approx(600.0 - 30.0, abs=30.0) or \
        _checks(ctx)[0]["user_wait"]["wait_s"] == pytest.approx(600.0, abs=1.0)
    ready = next(i for i, s in enumerate(ctx.site_setup_history) if s["status"] == "READY_FOR_ZONES")
    passed = next(i for i, s in enumerate(ctx.site_setup_history) if s.get("camera_check", {}).get("status") == "PASSED")
    assert passed < ready and result["camera_check"]["status"] == "PASSED"
    assert schema_errors(result, _RESULT) == []
    # REUSE_SCENE 은 보정이 맞춘 쌍(품질 게이트의 쌍)만 막는 쌍으로 넘긴다. 그 밖의 쌍(대각)은 참고로만 잰다(-759 그대로)
    gate_pairs = sorted(tuple(p["camera_ids"]) for p in result["quality_gate"]["pairs"].values())
    assert gate_pairs and checker.blocking == [gate_pairs]


# ---------------------------------------------------------------------------- S15P31A101-630
def test_zone_height_changes_propagate_from_preview_to_baked_and_fallback_to_unknown() -> None:
    """이전 구역·이전 맵이 주어지면 미리보기·완료 스냅샷에 zone_height_changes 가 실리고,
    world_anchor 가 거부되거나 REUSE_SCENE 이면 UNKNOWN 사유가 실린다."""
    preview_zhc = {
        "previous_scene_version": "scene-prev-1",
        "compared_scene_version": "scene-preview-x",
        "cell_m": 0.1,
        "height_diff_threshold_m": 0.15,
        "changed_area_ratio_threshold": 0.1,
        "min_coverage_ratio": 0.3,
        "zones": [
            {
                "zone_id": "zone-1",
                "status": "CHANGE_SUSPECTED",
                "reason": None,
                "changed_area_ratio": 0.25,
                "max_height_diff_m": 0.42,
                "max_diff_xy": [0.45, 0.55],
                "total_cells": 100,
                "compared_cells": 80,
                "changed_cells": 20,
            }
        ],
    }
    baked_zhc = {
        **preview_zhc,
        "compared_scene_version": "scene-baked-x",
        "zones": [{**preview_zhc["zones"][0], "changed_area_ratio": 0.22}],
    }
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        preview_builder=lambda **kw: PreviewDraftResult("scene-preview-x", zone_height_changes=preview_zhc),
        texture_baker=lambda **kw: BakedScene("scene-baked-x", zone_height_changes=baked_zhc),
    )
    inp = {
        **inp,
        "previous_scene": {
            "scene_version": "scene-prev-1",
            "calibration_version": "cal-prev-1",
            "uri": "s3://bucket/scenes/prev.glb",
        },
        "previous_zones": [
            {"zone_id": "zone-1", "polygon_xy": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]}
        ],
    }
    ctx = JobContext("job-zhc", "0192a6f0-7c1e-7b3a-9f2d-000000000630", "site_setup", 1, inp)
    result = handler(ctx)
    by_stage = {s["status"]: s for s in ctx.site_setup_history}
    assert by_stage["READY_FOR_ZONES"]["zone_height_changes"] == preview_zhc
    assert result["zone_height_changes"] == baked_zhc
    assert schema_errors(result, _RESULT) == []

    # world_anchor 가 채택되지 않은 경우 UNKNOWN(WORLD_NOT_ANCHORED) 폴백
    handler_unanchored, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=lambda doc: {
            **doc,
            "world_anchor": {"accepted": False, "reason": "CENTER_RESIDUAL_EXCEEDED", "reference_calibration_version": "cal-prev-1"},
        },
    )
    ctx2 = JobContext("job-zhc2", "0192a6f0-7c1e-7b3a-9f2d-000000000631", "site_setup", 1, inp)
    res2 = handler_unanchored(ctx2)
    assert res2["zone_height_changes"]["zones"][0]["status"] == "UNKNOWN"
    assert res2["zone_height_changes"]["zones"][0]["reason"] == "WORLD_NOT_ANCHORED"
    assert schema_errors(res2, _RESULT) == []


# ---------------------------------------------------------------------------- S15P31A101-690
_HEIGHT_REVIEW = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/height_review"}
_ONE_LOW = {"cam_A": 1.5, "cam_B": 1.5, "cam_C": 1.38, "cam_D": 1.5}


def _height_handler(
    restored: dict[str, float], **kw: Any,
) -> tuple[SiteSetupHandler, dict[str, Any], list[dict[str, Any]], _FakeTime]:
    """합성 방(카메라 실제 높이 restored)과 입력 높이 1.5 m(계약 예시)로 끝까지 도는 처리기. world_initializer 부른 높이를 모은다."""
    inp = copy.deepcopy(load_example("job_request.site_setup.json")["payload"]["input"])
    camera_ids = tuple(inp["calibration_target"]["camera_ids"])
    points_m, unscaled_poses, intrinsics_by_cam, tracks_doc = _make_synthetic_room_and_tracks(
        camera_ids=camera_ids, restored_heights_m=restored)
    init_calls: list[dict[str, Any]] = []

    def init(**w: Any) -> InitialWorldReconstruction:
        init_calls.append(dict(w["camera_heights_m"]))
        return build_world_from_pointcloud_and_poses(
            site_id=w["site_id"], camera_ids=w["camera_ids"], camera_heights_m=w["camera_heights_m"],
            height_tolerance_cm=w["height_tolerance_cm"], points_m=points_m, unscaled_poses=unscaled_poses,
            intrinsics_by_cam=intrinsics_by_cam, created_at_ms=w["created_at_ms"])

    t = _FakeTime()
    handler = SiteSetupHandler(
        background_collector=lambda **c: CleanBackgroundBundle("bundle-height-review", dict.fromkeys(camera_ids, 100.0),
                                                               tracks_doc=tracks_doc),
        world_initializer=init, clock_ms=lambda: 1760342410000, monotonic=t.monotonic, sleep=t.sleep, **kw)
    return handler, inp, init_calls, t


def _decide_when_asked(t: _FakeTime, ctx: JobContext, decision: dict[str, Any]) -> Any:
    """확인을 기다리는 동안 두 번째 sleep 뒤 하트비트 응답처럼 결정을 넣는 sleep."""
    def sleep(event: Any, seconds: float) -> None:
        t.sleep(event, seconds)
        waiting = (ctx.site_setup or {}).get("height_review", {}).get("status") == "AWAITING_CONFIRMATION"
        if waiting and len(t.sleeps) >= 2:
            ctx.note_height_decision(decision)

    return sleep


def _reviews(ctx: JobContext) -> list[dict[str, Any]]:
    """스냅샷마다 실린 height_review(다음 단계 스냅샷이 이어 실은 같은 값은 한 번만)."""
    out: list[dict[str, Any]] = []
    for h in ctx.site_setup_history:
        if "height_review" in h and (not out or out[-1] != h["height_review"]):
            out.append(h["height_review"])
    return out


def _ctx(name: str, inp: dict[str, Any], t: _FakeTime, timeout_ms: int = 1_800_000) -> JobContext:
    return JobContext(name, "0192a6f0-7c1e-7b3a-9f2d-000000000690", "site_setup", 1, inp, timeout_ms=timeout_ms,
                      monotonic=t.monotonic)


def test_height_review_one_wrong_camera_waits_then_corrected_value_reuses_the_model() -> None:
    """한 대만 잘못 넣으면 그 카메라만 LOW 로 확인을 기다린다(height_checks 없이). 고친 값이 오면 MapAnything(초기화)을 다시 부르지
    않고 같은 점구름으로 다시 맞춰 CORRECTED 로 이어 가고, 그 뒤 높이 검산·결과는 고친 높이 기준이다."""
    handler, inp, init_calls, t = _height_handler(_ONE_LOW)
    ctx = _ctx("job-hr", inp, t)
    corrected = {**inp["camera_heights_m"], "cam_C": 1.38}
    handler._sleep = _decide_when_asked(t, ctx, {"decision": "CORRECTED", "camera_heights_m": corrected})
    result = handler(ctx)

    awaiting, final = _reviews(ctx)[0], _reviews(ctx)[-1]
    assert schema_errors(awaiting, _HEIGHT_REVIEW) == [] and schema_errors(final, _HEIGHT_REVIEW) == []
    assert awaiting["status"] == "AWAITING_CONFIRMATION" and awaiting["wait_s"] == 180
    assert {c: v["verdict"] for c, v in awaiting["cameras"].items()} == {
        "cam_A": "OK", "cam_B": "OK", "cam_C": "LOW", "cam_D": "OK"}
    assert awaiting["cameras"]["cam_C"]["difference_cm"] == pytest.approx(-12.0, abs=0.5)
    waiting_snap = next(h for h in ctx.site_setup_history
                        if h.get("height_review", {}).get("status") == "AWAITING_CONFIRMATION")
    assert schema_errors(waiting_snap, _PROGRESS) == []
    assert "height_checks" not in waiting_snap  # 서버 세션 높이가 바뀌기 전 입력 높이로 검산을 싣지 않는다
    assert final["status"] == "CORRECTED" and final["camera_heights_m"] == corrected
    assert all(v["verdict"] == "OK" for v in final["cameras"].values())
    assert init_calls == [inp["camera_heights_m"]]  # 초기 복원은 한 번(다시 맞추기는 같은 점구름·포즈)
    assert {c: v["input_height_m"] for c, v in result["height_checks"].items()} == corrected
    assert result["quality_gate"]["verdict"] in ("PASSED", "NEEDS_CHECK")


def test_height_review_confirmed_measurement_is_not_rejected_again_by_the_quality_gate() -> None:
    """「실측이 맞음」(같은 높이)이면 그 높이로 진행하고, 품질 게이트는 높이 차(12 cm > 10 cm)로 다시 거절하지 않는다(기록만)."""
    handler, inp, init_calls, t = _height_handler(_ONE_LOW)
    ctx = _ctx("job-hr2", inp, t)
    handler._sleep = _decide_when_asked(t, ctx, {"decision": "CONFIRMED", "camera_heights_m": dict(inp["camera_heights_m"])})
    result = handler(ctx)
    assert _reviews(ctx)[-1]["status"] == "CONFIRMED"
    assert result["height_checks"]["cam_C"]["difference_cm"] < -10.0
    gate = result["quality_gate"]
    assert gate["cameras"]["cam_C"]["verdict"] == "PASS" and gate["cameras"]["cam_C"]["height_diff_cm"] < -10.0
    assert not any(p["kind"] == "CAMERA" for p in gate["problems"])
    assert len(init_calls) == 1


def test_height_review_timeout_continues_with_input_and_keeps_the_gate() -> None:
    """대기 상한까지 답이 없으면 입력 높이 그대로 진행하고(TIMED_OUT 기록), 품질 게이트 높이 상한은 그대로 본다."""
    handler, inp, _, t = _height_handler(_ONE_LOW, height_review_wait_s=5.0)
    ctx = _ctx("job-hr3", inp, t)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "VALIDATION_REJECTED" and "cam_C" in exc.value.message
    statuses = [r["status"] for r in _reviews(ctx)]
    assert statuses[0] == "AWAITING_CONFIRMATION" and statuses[-1] == "TIMED_OUT"
    assert sum(t.sleeps) == pytest.approx(5.0)


def test_height_review_wait_is_capped_by_the_remaining_budget_and_cancel_still_stops() -> None:
    """실행 상한까지 뒤 단계 몫(reserve)을 남기도록 대기를 줄인다. 남는 시간이 없으면 기다리지 않는다. 취소는 기다리는 중에도 멈춘다."""
    loose_gate = {"height_max_diff_cm": 50.0}
    handler, inp, _, t = _height_handler(_ONE_LOW, quality_gate=loose_gate)
    ctx = _ctx("job-hr4", inp, t, timeout_ms=1_300_000)  # 뒤 단계 몫 1,260 s = Stage A 60 + 카메라 확인 600 + 맵 600(-688)
    handler(ctx)
    review = _reviews(ctx)[0]
    assert review["status"] == "AWAITING_CONFIRMATION" and review["wait_s"] == pytest.approx(40.0, abs=0.5)

    handler, inp, _, t = _height_handler(_ONE_LOW, quality_gate=loose_gate)
    ctx = _ctx("job-hr5", inp, t, timeout_ms=300_000)
    handler(ctx)
    assert [r["status"] for r in _reviews(ctx)] == ["TIMED_OUT"] and t.sleeps == []

    handler, inp, _, t = _height_handler(_ONE_LOW)
    ctx = _ctx("job-hr6", inp, t)

    def cancel(event: Any, seconds: float) -> None:
        ctx.cancelled.set()

    handler._sleep = cancel
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "CANCELLED"


def test_human_waits_never_eat_the_map_share_of_the_execution_limit() -> None:
    """S15P31A101-688: 실행 상한(1,800 s)은 사람 대기 몫과 기계 몫의 합이다. 높이 확인을 끝까지(180 s) 기다리고 카메라 확인 걷기 시작을
    대기 마감 직전에 눌러도, 통과한 뒤 맵(미리보기·굽기)에 맵 몫(600 s) 이상이 남는다(통과한 확인을 굽기 시간 초과로 버리지 않는다)."""
    assert SETUP_TIME_BUDGET_S == pytest.approx(1800.0)
    events: list[str] = []
    checker = _FakeChecker([["PASSED"]], events=events)
    box: dict[str, JobContext] = {}
    left_at_bake: list[float] = []

    def bake(**kw: Any) -> Any:
        left_at_bake.append(box["ctx"].remaining_s())
        return SiteSetupHandler._default_texture_baker(**kw)

    handler, inp, _, t = _height_handler(_ONE_LOW, quality_gate={"height_max_diff_cm": 50.0}, camera_check=checker,
                                         camera_check_poll_s=1.0, texture_baker=bake)
    ctx = box["ctx"] = _ctx("job-budget", inp, t, timeout_ms=1_800_000)
    # 높이 결정은 오지 않는다(180 s 를 다 기다림). 카메라 확인 단계는 그 뒤 600 s 이고, 마감 20 s 전에 「걷기 시작」
    stage_end = SETUP_BUDGET_HEIGHT_REVIEW_S + SETUP_BUDGET_CAMERA_CHECK_S
    handler._sleep = _press(box, t, {stage_end - 20.0: 1})
    result = handler(ctx)

    assert _reviews(ctx)[0]["wait_s"] == pytest.approx(SETUP_BUDGET_HEIGHT_REVIEW_S)
    assert _reviews(ctx)[-1]["status"] == "TIMED_OUT"
    assert _checks(ctx)[0]["user_wait"]["wait_s"] == pytest.approx(SETUP_BUDGET_CAMERA_CHECK_S, abs=1.0)
    assert result["camera_check"]["status"] == "PASSED"
    assert len(left_at_bake) == 1 and left_at_bake[0] >= SETUP_BUDGET_MAP_S
    assert t.now <= stage_end


def test_height_review_same_multiplier_error_passes_and_known_decision_skips_the_wait() -> None:
    """4대를 같은 배수로 틀리면 축척이 함께 바뀌어 잡지 못한다(PASSED, 기다리지 않음). 재대여처럼 결정이 이미 와 있으면 기다리지 않고 쓴다."""
    handler, inp, _, t = _height_handler(dict.fromkeys(("cam_A", "cam_B", "cam_C", "cam_D"), 1.65))
    ctx = _ctx("job-hr7", inp, t)
    handler(ctx)
    assert [r["status"] for r in _reviews(ctx)] == ["PASSED"] and t.sleeps == []

    handler, inp, init_calls, t = _height_handler(_ONE_LOW)
    ctx = _ctx("job-hr8", inp, t)
    ctx.note_height_decision({"decision": "CORRECTED", "camera_heights_m": {**inp["camera_heights_m"], "cam_C": 1.38}})
    result = handler(ctx)
    assert [r["status"] for r in _reviews(ctx)] == ["CORRECTED"] and t.sleeps == []
    assert result["height_checks"]["cam_C"]["input_height_m"] == 1.38 and len(init_calls) == 1


def test_height_review_unit_rules() -> None:
    from simai_ai.calibration.extrinsics.joint_ba_v2 import compute_height_review, evaluate_quality_gate

    cams = compute_height_review({"a": 2.45, "b": 2.44, "c": 2.48, "d": 2.48},
                                 {"a": 2.39, "b": 2.45, "c": 2.66, "d": 2.47})
    # 15차 cam_B 5.5 cm(2.4%)는 실측이 맞았던 잡음이라 기준(3%) 안, 18 cm(7%)는 HIGH
    assert {k: v["verdict"] for k, v in cams.items()} == {"a": "OK", "b": "OK", "c": "HIGH", "d": "OK"}
    assert cams["a"]["difference_cm"] == -6.0 and cams["a"]["relative_diff"] == pytest.approx(-0.02449, abs=1e-5)
    assert compute_height_review({"a": 2.5}, {"a": 2.4})["a"]["verdict"] == "LOW"
    with pytest.raises(ValueError):
        compute_height_review({"a": 2.5}, {"a": 2.4}, max_relative_diff=0)
    pair = {"a-b": {"holdout": 20, "camera_ids": ["a", "b"], "after_holdout": {"median": 0.5, "p90": 0.9}}}
    heights = {"a": {"difference_cm": -12.0}, "b": {"difference_cm": None}}
    gate = evaluate_quality_gate(pair, heights, heights_settled=True)
    assert gate["cameras"]["a"] == {"height_diff_cm": -12.0, "verdict": "PASS", "reason": None}
    assert gate["cameras"]["b"]["reason"] == "HEIGHT_UNKNOWN" and gate["verdict"] == "FAILED"  # 기하 실패는 그대로


# ---------------------------------------------------------------------------- S15P31A101-760 걷기 다듬기
class _RefiningChecker(_FakeChecker):
    """기하 실패한 시도의 걷기 다듬기(refine_walk)를 대신하는 실행기 대역. adopt 면 새 버전 문서·Holdout PASSED 요약을 낸다."""

    def __init__(self, attempts: list[list[str]], *, adopt: bool, events: list[str] | None = None,
                 recheck: str = "FAILED") -> None:
        super().__init__(attempts, events=events)
        self.adopt = adopt
        self.recheck = recheck
        self.refined: list[dict[str, Any]] = []
        self.adopted: list[Any] = []
        self.rechecked: list[dict[str, Any]] = []

    def refine_walk(self, calibration_doc: Any, *, camera_heights_m: Any, required_pairs: Any = (),
                    blocking_pairs: Any = ()) -> Any:
        from simai_ai.calibration.document import content_hash
        from simai_ai.calibration.monitor import BASELINE_SOURCE_SETUP_CAMERA_CHECK, BaselineRecord
        from simai_ai.calibration.walk_refinement import WalkRefinementResult

        self.events.append("refine")
        self.refined.append({"version": calibration_doc["calibration_version"], "blocking": sorted(blocking_pairs),
                             "heights": dict(camera_heights_m)})
        record = {"base_calibration_version": calibration_doc["calibration_version"], "adopted": self.adopt}
        if not self.adopt:
            return WalkRefinementResult(False, ("HOLDOUT_PAIR_FAILED",), None, None, None, {**record, "reasons": ["X"]})
        doc = copy.deepcopy(dict(calibration_doc))
        for cam in doc["cameras"]:
            cam["camera_height_m"] = round(float(cam["camera_height_m"]) + 0.012, 4)
        doc["content_hash"] = content_hash(doc)
        version = f"{calibration_doc['calibration_version']}-walk-0123abcd"
        doc["calibration_version"] = version
        summary = self._summary("PASSED")
        summary["calibration_version"] = version
        summary["baseline"] = {"calibration_version": version, "values_px": {"cam_D": 3.0},
                               "source": BASELINE_SOURCE_SETUP_CAMERA_CHECK}
        baseline = BaselineRecord(version, {"cam_D": 3.0}, ("cam_D",), {"cam_D": BASELINE_SOURCE_SETUP_CAMERA_CHECK})
        return WalkRefinementResult(True, (), doc, summary, baseline, {**record, "new_calibration_version": version})

    def adopt_walk(self, result: Any) -> None:
        self.adopted.append(result.calibration_doc["calibration_version"])

    def recheck_walk(self, calibration_doc: Any, *, required_pairs: Any = (), blocking_pairs: Any = ()) -> Any:
        """다듬기 거절 뒤 -759 규칙 다시 판정 대역: 정한 상태(PASSED·FAILED)의 요약을 낸다."""
        self.events.append(f"recheck:{self.recheck}")
        self.rechecked.append({"version": calibration_doc["calibration_version"], "blocking": sorted(blocking_pairs)})
        self.version = str(calibration_doc["calibration_version"])
        return self._summary(self.recheck)


def _walk_room(checker: _FakeChecker, events: list[str], t: _FakeTime, published: list[dict[str, Any]],
               seen: dict[str, list[str]], **kw: Any) -> tuple[SiteSetupHandler, dict[str, Any]]:
    """보정 문서를 모으는 발행기와, 미리보기·굽기가 받은 보정 버전을 남기는 처리기."""
    def preview(**w: Any) -> PreviewDraftResult:
        events.append("preview")
        seen["preview"].append(w["calibration_doc"]["calibration_version"])
        return PreviewDraftResult("scene-p", floor_coverage=_floor_coverage(w["calibration_doc"]["calibration_version"]))

    def bake(**w: Any) -> str:
        events.append("bake")
        seen["bake"].append(w["calibration_doc"]["calibration_version"])
        return "scene-b"

    return _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, camera_check_poll_s=1.0, monotonic=t.monotonic, sleep=t.sleep,
        calibration_publisher=_doc_publisher(published), preview_builder=preview, texture_baker=bake, **kw,
    )[:2]


def test_geometric_failure_adopts_walk_refinement_and_every_later_step_uses_the_new_version() -> None:
    """새 맵 세팅의 시도 1 이 기하 실패(REPROJECTION_HIGH)면 그 걷기로 한 번 다듬는다. 채택되면 새 보정 문서를 같은 발행 주입점으로
    한 번 더 올리고(덮어쓰지 않음), 카메라 확인은 그 버전의 Holdout 판정(PASSED·기준선)으로 끝나며, 높이 확인·보정 문서 artifacts·
    미리보기·굽기·바닥 감시 범위·결과가 모두 새 버전을 가리킨다. 「확인만 다시」를 기다리지 않는다."""
    events: list[str] = []
    t = _FakeTime()
    published: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = {"preview": [], "bake": []}
    checker = _RefiningChecker([["MEASURING", "FAILED"]], adopt=True, events=events)
    handler, inp = _walk_room(checker, events, t, published, seen)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1})
    ctx = box["ctx"] = JobContext("job-walk", "0192a6f0-7c1e-7b3a-9f2d-000000000760", "site_setup", 1, inp,
                                  timeout_ms=1_800_000)
    result = handler(ctx)

    assert events == ["start:0192a6f0-1", "refine", "preview", "bake"]
    assert len(published) == 2 and len(checker.started) == 1
    stage_a, refined = published
    assert checker.refined[0]["version"] == stage_a["calibration_version"] and checker.refined[0]["blocking"] == []
    assert checker.refined[0]["heights"] == inp["camera_heights_m"]
    new = refined["calibration_version"]
    assert new == f"{stage_a['calibration_version']}-walk-0123abcd" and checker.adopted == [new]
    # 계보·세팅 출처는 발행 주입점이 세팅 시작 때 활성 보정으로 넣는다(Stage A 버전을 계보로 싣지 않는다)
    assert "previous_calibration_version" not in refined
    assert result["calibration_version"] == new and seen == {"preview": [new], "bake": [new]}
    assert new in result["artifacts"]["calibration_document"]["uri"]
    assert result["artifacts"]["floor_coverage"]["calibration_version"] == new
    cc = result["camera_check"]
    assert cc["status"] == "PASSED" and cc["calibration_version"] == new and cc["baseline"]["calibration_version"] == new
    assert cc["attempt"]["no"] == 1 and cc["attempt"]["calibration_version"] == new and "user_wait" not in cc
    restored = {c["camera_id"]: c["camera_height_m"] for c in refined["cameras"]}
    assert {cid: v["restored_height_m"] for cid, v in result["height_checks"].items()} == pytest.approx(restored)
    passed = [s for s in ctx.site_setup_history if s.get("camera_check", {}).get("status") == "PASSED"]
    assert passed and all(s["calibration_version"] == new for s in passed)
    assert all(s["calibration_version"] == new for s in ctx.site_setup_history
               if s["status"] in ("PREVIEW_AND_VLM_DRAFT", "READY_FOR_ZONES", "BAKING_TEXTURE"))
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_rejected_walk_refinement_publishes_nothing_and_keeps_the_retry_flow() -> None:
    """다듬기를 채택하지 않으면 새 문서를 내지 않고 지금처럼 「확인만 다시」를 기다린다. 시도 2 가 지금 보정으로 통과하면 그 보정 그대로다."""
    events: list[str] = []
    t = _FakeTime()
    published: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = {"preview": [], "bake": []}
    checker = _RefiningChecker([["MEASURING", "FAILED"], ["PASSED"]], adopt=False, events=events)
    handler, inp = _walk_room(checker, events, t, published, seen)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1, 60.0: 2})
    ctx = box["ctx"] = JobContext("job-walk-no", "0192a6f0-7c1e-7b3a-9f2d-000000000761", "site_setup", 1, inp,
                                  timeout_ms=1_800_000)
    result = handler(ctx)

    assert events == ["start:0192a6f0-1", "refine", "recheck:FAILED", "start:0192a6f0-2", "passed", "preview", "bake"]
    assert len(published) == 1 and checker.adopted == []
    # 다듬기 거절 뒤 같은 시도를 -759 규칙(품질 게이트 옆 쌍만 막음)으로 다시 쟀고, 그것도 실패라 「확인만 다시」를 기다렸다
    gate_pairs = sorted(tuple(p["camera_ids"]) for p in result["quality_gate"]["pairs"].values())
    assert checker.rechecked == [{"version": published[0]["calibration_version"], "blocking": gate_pairs}]
    version = published[0]["calibration_version"]
    assert result["calibration_version"] == version and seen == {"preview": [version], "bake": [version]}
    failed = next(c for c in _checks(ctx) if c["status"] == "FAILED")
    assert failed["user_wait"]["wait_s"] > 0 and failed["calibration_version"] == version


def test_rejected_walk_refinement_falls_back_to_the_ring_pair_check_and_passes_with_stage_a() -> None:
    """다듬기가 거절돼도 같은 시도를 -759 규칙(옆 쌍만 막고 대각은 참고)으로 다시 재서 통과하면 Stage A 보정 그대로 통과로 끝낸다
    (시연 회귀 방지). 새 보정은 발행하지 않고 「확인만 다시」도 기다리지 않는다."""
    events: list[str] = []
    t = _FakeTime()
    published: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = {"preview": [], "bake": []}
    checker = _RefiningChecker([["MEASURING", "FAILED"]], adopt=False, recheck="PASSED", events=events)
    handler, inp = _walk_room(checker, events, t, published, seen)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1})
    ctx = box["ctx"] = JobContext("job-walk-759", "0192a6f0-7c1e-7b3a-9f2d-000000000765", "site_setup", 1, inp,
                                  timeout_ms=1_800_000)
    result = handler(ctx)

    assert events == ["start:0192a6f0-1", "refine", "recheck:PASSED", "preview", "bake"]
    assert len(published) == 1 and checker.adopted == [] and len(checker.started) == 1
    version = published[0]["calibration_version"]
    gate_pairs = sorted(tuple(p["camera_ids"]) for p in result["quality_gate"]["pairs"].values())
    assert checker.rechecked == [{"version": version, "blocking": gate_pairs}]
    assert result["calibration_version"] == version and seen == {"preview": [version], "bake": [version]}
    cc = result["camera_check"]
    assert cc["status"] == "PASSED" and cc["calibration_version"] == version
    assert cc["attempt"]["no"] == 1 and cc["attempt"]["calibration_version"] == version and "user_wait" not in cc
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_walk_refinement_runs_only_for_new_model_geometric_failures() -> None:
    """Stage A 가 바로 통과하면 다듬지 않는다. 표본 부족(INCOMPLETE)도 다듬지 않는다(다시 걷기가 답이다)."""
    events: list[str] = []
    t = _FakeTime()
    published: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = {"preview": [], "bake": []}
    checker = _RefiningChecker([["MEASURING", "PASSED"]], adopt=True, events=events)
    handler, inp = _walk_room(checker, events, t, published, seen)
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1})
    ctx = box["ctx"] = JobContext("job-walk-pass", "0192a6f0-7c1e-7b3a-9f2d-000000000762", "site_setup", 1, inp)
    handler(ctx)
    assert "refine" not in events and len(published) == 1

    events.clear()
    t = _FakeTime()
    checker = _RefiningChecker([["MEASURING"], ["PASSED"]], adopt=True, events=events)
    handler, inp = _walk_room(checker, events, t, [], {"preview": [], "bake": []}, camera_check_attempt_s=20.0)
    box = {}
    handler._sleep = _press(box, t, {1.0: 1, 40.0: 2})
    ctx = box["ctx"] = JobContext("job-walk-short", "0192a6f0-7c1e-7b3a-9f2d-000000000763", "site_setup", 1, inp)
    handler(ctx)
    assert checker.incompletes == 1 and "refine" not in events


def test_reuse_scene_keeps_the_old_camera_check_without_walk_refinement() -> None:
    """REUSE_SCENE 은 지금 동작 그대로다: 품질 게이트의 쌍만 막고, 기하 실패해도 다듬지 않고 「확인만 다시」를 기다린다."""
    events: list[str] = []
    t = _FakeTime()
    published: list[dict[str, Any]] = []
    checker = _RefiningChecker([["FAILED"], ["PASSED"]], adopt=True, events=events)
    handler, _, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(True), calibration_publisher=_doc_publisher(published), camera_check=checker,
        monotonic=t.monotonic, preview_builder=_must_not_run("미리보기"), texture_baker=_must_not_run("굽기"),
    )
    box: dict[str, JobContext] = {}
    handler._sleep = _press(box, t, {1.0: 1, 60.0: 2})
    ctx = box["ctx"] = JobContext("job-walk-reuse", "0192a6f0-7c1e-7b3a-9f2d-000000000764", "site_setup", 1,
                                  _reuse_input(), timeout_ms=1_800_000)
    result = handler(ctx)
    assert "refine" not in events and len(published) == 1 and checker.adopted == []
    assert result["calibration_version"] == published[0]["calibration_version"]
    gate_pairs = sorted(tuple(p["camera_ids"]) for p in result["quality_gate"]["pairs"].values())
    assert checker.blocking == [gate_pairs, gate_pairs]


# --------------------------------------------------------------------------- S15P31A101-764 Stage A 자동 재시도


def _bundle(td: Any, cams: tuple[str, ...]) -> CleanBackgroundBundle:
    return CleanBackgroundBundle("bundle-764", dict.fromkeys(cams, 100.0), tracks_doc=td)


def _scripted_stage_a(monkeypatch: pytest.MonkeyPatch, failures: dict[int, Any]) -> list[str | None]:
    """run_stage_a_rotation_ba 의 n 번째 호출(1부터)을 failures[n](StageAError 를 만드는 함수)로 바꾸고 나머지는 진짜로 푼다.

    돌려주는 목록에 호출마다 받은 기준 카메라(자동이면 None)를 적는다."""
    import simai_ai.calibration.extrinsics.joint_ba_v2 as jba

    original = jba.run_stage_a_rotation_ba
    anchors: list[str | None] = []

    def fake(initial: Any, tracks: Any, **kw: Any) -> Any:
        anchors.append(kw.get("anchor_camera_id"))
        make = failures.get(len(anchors))
        if make is None:
            return original(initial, tracks, **kw)
        anchor = kw.get("anchor_camera_id") or jba.stage_a_anchor_order(initial, tracks)[0]
        raise make(anchor)

    monkeypatch.setattr(jba, "run_stage_a_rotation_ba", fake)
    return anchors


def _worsened(pair: str) -> Any:
    from simai_ai.calibration.extrinsics.joint_ba_v2 import StageAError

    def make(anchor: str) -> StageAError:
        return StageAError(
            "VALIDATION_REJECTED",
            f"Stage A rotation BA rejected by validation gate: converged=True, blockers=[], worsened_pairs=['{pair}']",
            anchor_retryable=True, details={"anchor_camera_id": anchor, "converged": True, "worsened_pairs": [pair]})
    return make


def test_stage_a_auto_retry_adopts_the_second_attempt_after_a_rejection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """11-16 로비: 거절마다 관리자가 처음부터 다시 눌렀다. 이제 같은 트랙으로 기준 카메라만 바꿔 다시 풀고 통과한 시도를 채택한다."""
    anchors = _scripted_stage_a(monkeypatch, {1: _worsened("cam_D-cam_A"), 3: _worsened("cam_B-cam_C")})
    handler, inp, published = _room_handler(_bundle)
    ctx = JobContext("job-764-ok", "0192a6f0-7c1e-7b3a-9f2d-000000000764", "site_setup", 1, inp)
    with caplog.at_level("INFO", logger="services.worker"):
        result = handler(ctx)
    assert len(anchors) == 3 and anchors[0] is None and None not in anchors[1:] and len(set(anchors[1:])) == 2
    assert result["scene_version"] and len(published) == 1
    joint = next(s for s in ctx.site_setup_history if s.get("calibration_version"))["metrics"]["joint_ba"]
    assert joint["anchor_camera_id"] == anchors[1]  # 두 번째 시도(통과)의 기준 카메라
    assert result["quality_gate"]["verdict"] in ("PASSED", "NEEDS_CHECK")
    assert "Stage A 시도 1/3" in caplog.text and "Stage A 시도 3/3" in caplog.text and "시도 2(기준" in caplog.text
    assert all(schema_errors(s, _PROGRESS) == [] for s in ctx.site_setup_history)
    assert schema_errors(result, _RESULT) == []


def test_stage_a_auto_retry_all_rejected_keeps_the_failure_code_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    anchors = _scripted_stage_a(monkeypatch, {n: _worsened("cam_D-cam_A") for n in (1, 2, 3)})
    handler, inp, published = _room_handler(_bundle)
    ctx = JobContext("job-764-fail", "0192a6f0-7c1e-7b3a-9f2d-000000000765", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert len(anchors) == 3 and published == []
    assert exc.value.code == "VALIDATION_REJECTED" and exc.value.retryable is False
    assert "worsened_pairs=['cam_D-cam_A']" in exc.value.message  # 화면이 이 쌍을 뽑아 보인다(setupFailures.ts)
    assert exc.value.message.endswith("(같은 배경으로 자동 재시도 2회도 실패)") and len(exc.value.message) <= 480


def test_stage_a_auto_retry_all_gate_failures_report_the_best_attempt_snapshot() -> None:
    handler, inp, published = _room_handler(_bundle, quality_gate={"pair_holdout_p90_max_px": 1e-9})
    ctx = JobContext("job-764-gate", "0192a6f0-7c1e-7b3a-9f2d-000000000766", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "VALIDATION_REJECTED" and exc.value.message.startswith("보정 품질 기준 미달:")
    assert exc.value.message.endswith("(같은 배경으로 자동 재시도 2회도 실패)") and published == []
    last = ctx.site_setup_history[-1]
    assert last["quality_gate"]["verdict"] == "FAILED" and "calibration_version" not in last
    assert schema_errors(last, _PROGRESS) == []


def test_stage_a_auto_retry_stops_when_the_later_stages_need_the_remaining_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """실행 상한까지 남은 시간이 맵 몫(600 s)보다 적으면 재시도하지 않는다(뒤 단계 몫을 깎지 않는다)."""
    import time as _time

    anchors = _scripted_stage_a(monkeypatch, {n: _worsened("cam_D-cam_A") for n in (1, 2, 3)})
    handler, inp, _ = _room_handler(_bundle)
    ctx = JobContext("job-764-time", "0192a6f0-7c1e-7b3a-9f2d-000000000767", "site_setup", 1, inp,
                     deadline=_time.monotonic() + 599.0)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert anchors == [None]
    assert exc.value.code == "VALIDATION_REJECTED" and "자동 재시도" not in exc.value.message


def test_stage_a_auto_retry_does_not_retry_insufficient_features(monkeypatch: pytest.MonkeyPatch) -> None:
    from simai_ai.calibration.extrinsics.joint_ba_v2 import StageAError

    anchors = _scripted_stage_a(monkeypatch, {
        1: lambda anchor: StageAError("INSUFFICIENT_FEATURES", "Insufficient total training tracks: 3")})
    handler, inp, _ = _room_handler(_bundle)
    ctx = JobContext("job-764-feat", "0192a6f0-7c1e-7b3a-9f2d-000000000768", "site_setup", 1, inp)
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert anchors == [None]
    assert exc.value.code == "INSUFFICIENT_FEATURES" and exc.value.message == "Insufficient total training tracks: 3"
