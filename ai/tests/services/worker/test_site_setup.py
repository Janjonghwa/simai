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


def _anchor_hook(accepted: bool, reason: str = "ANCHORED") -> Any:
    """World 다시 맞추기(-584) 주입점 대역: 문서에 world_anchor 만 붙인다(채택이면 실제로는 R·t 도 옮겨진다)."""
    def anchor(doc: Any) -> dict[str, Any]:
        out = copy.deepcopy(dict(doc))
        out["world_anchor"] = {
            "accepted": accepted, "reason": reason, "reference_calibration_version": _REF_CAL,
            "method": "CAMERA_CENTER_YAW_XY", "max_center_residual_m": 0.0612 if accepted else 0.4127,
            "max_orientation_change_rad": 0.0703,
        }
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


def test_reuse_scene_rejected_anchor_continues_as_full_setup_in_the_same_job() -> None:
    """World 다시 맞추기가 거부되면(카메라 이동) 같은 작업 안에서 FULL_SETUP 으로 이어 간다: 판정 전 스냅샷은 REUSE_SCENE,
    그 뒤로는 mode=FULL_SETUP + reuse_fallback_reason + world_anchor(accepted=false). 미리보기·굽기를 그대로 한다."""
    called: list[str] = []

    def preview(**kw: Any) -> PreviewDraftResult:
        called.append("preview")
        return PreviewDraftResult("scene-preview-614")

    def bake(**kw: Any) -> str:
        called.append("bake")
        return "scene-614"

    handler, _, published = _room_handler(
        lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td),
        world_anchor=_anchor_hook(False, "CENTER_RESIDUAL_EXCEEDED"), preview_builder=preview, texture_baker=bake,
    )
    ctx = JobContext("job-reuse-fb", "0192a6f0-7c1e-7b3a-9f2d-000000000615", "site_setup", 1, _reuse_input())
    result = handler(ctx)

    assert called == ["preview", "bake"]
    by_stage = {s["status"]: s for s in ctx.site_setup_history}
    assert by_stage["COLLECTING_BACKGROUND"]["mode"] == by_stage["INITIAL_POSE_AND_WORLD"]["mode"] == "REUSE_SCENE"
    for stage in ("JOINT_BA_AND_VERIFY", "PREVIEW_AND_VLM_DRAFT", "READY_FOR_ZONES", "BAKING_TEXTURE"):
        assert by_stage[stage]["mode"] == "FULL_SETUP"
        assert by_stage[stage]["reuse_fallback_reason"] == "CENTER_RESIDUAL_EXCEEDED"
        assert by_stage[stage]["world_anchor"]["accepted"] is False
    assert result["mode"] == "FULL_SETUP" and result["reuse_fallback_reason"] == "CENTER_RESIDUAL_EXCEEDED"
    assert result["preview_scene_version"] == "scene-preview-614" and result["scene_version"] == "scene-614"
    assert result["world_anchor"]["reason"] == "CENTER_RESIDUAL_EXCEEDED"
    assert published[0]["world_anchor"]["accepted"] is False
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


def test_reuse_scene_without_anchor_record_falls_back_with_no_reference() -> None:
    """보정 문서에 world_anchor 가 없으면(비교할 기준 보정 없음·World 다시 맞추기 없음) 같은 World 라고 지어내지 않고
    NO_REFERENCE 로 전체 세팅을 이어 간다."""
    handler, _, _ = _room_handler(lambda td, cams: CleanBackgroundBundle("b", dict.fromkeys(cams, 100.0), tracks_doc=td))
    ctx = JobContext("job-reuse-none", "0192a6f0-7c1e-7b3a-9f2d-000000000616", "site_setup", 1, _reuse_input())
    result = handler(ctx)
    assert result["mode"] == "FULL_SETUP" and result["reuse_fallback_reason"] == "NO_REFERENCE"
    assert result["world_anchor"] == {"accepted": False, "reason": "NO_REFERENCE", "reference_calibration_version": None}
    assert "BAKING_TEXTURE" in [s["status"] for s in ctx.site_setup_history]
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert schema_errors(result, _RESULT) == []


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
    """카메라 확인 실행기 대역: poll 마다 준비한 요약을 하나씩 낸다."""

    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.started: list[tuple[str, str, list[Any]]] = []
        self.stopped = 0
        self.version = ""

    def _summary(self, status: str) -> dict[str, Any]:
        problems = [] if status in ("WAITING", "MEASURING", "PASSED") else [
            {"kind": "CHECK", "id": "camera_check", "reason": "NOT_FINISHED", "message": "카메라 확인을 마치지 못했습니다"}]
        return {"status": status, "calibration_version": self.version, "progress_pct": 100.0 if status == "PASSED" else 10.0,
                "elapsed_s": 1.0, "thresholds": {"min_duration_s": 30.0, "max_duration_s": 120.0, "min_pair_samples": 30,
                                                 "min_pair_pass_rate": 0.75, "max_pair_residual_median_px": 8.0,
                                                 "min_camera_views": 30, "min_loo_samples": 20, "max_loo_median_px": 8.0,
                                                 "min_evaluated_pairs": 3},
                "pairs": {}, "cameras": {}, "problems": problems}

    def start(self, setup_session_id: str, calibration_doc: Any, *, expires_at_ms: int, required_pairs: Any = ()) -> Any:
        self.version = str(calibration_doc["calibration_version"])
        self.started.append((setup_session_id, self.version, list(required_pairs)))
        return self._summary("WAITING")

    def poll(self) -> Any:
        return self._summary(self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])

    def incomplete(self) -> Any:
        return self._summary("INCOMPLETE")

    def stop(self) -> None:
        self.stopped += 1


def test_camera_check_runs_after_calibration_and_result_waits_for_pass() -> None:
    checker = _FakeChecker(["MEASURING", "MEASURING", "PASSED"])
    handler, inp, published = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, camera_check_poll_s=0.01,
    )
    ctx = JobContext("job-cc", "0192a6f0-7c1e-7b3a-9f2d-000000000646", "site_setup", 1, inp, timeout_ms=600_000)
    result = handler(ctx)
    assert checker.started and checker.started[0][1] == result["calibration_version"] == published[0]["calibration_version"]
    assert result["camera_check"]["status"] == "PASSED" and schema_errors(result, _RESULT) == []
    assert result["quality_gate"]["verdict"] in ("PASSED", "NEEDS_CHECK")
    seen = [s["camera_check"]["status"] for s in ctx.site_setup_history if "camera_check" in s]
    assert seen and seen[0] == "WAITING"
    assert all("camera_check" not in s for s in ctx.site_setup_history if s["progress_pct"] < 65)
    for snap in ctx.site_setup_history:
        assert schema_errors(snap, _PROGRESS) == []
    assert checker.stopped >= 1


def test_camera_check_not_finished_by_deadline_is_incomplete() -> None:
    checker = _FakeChecker(["MEASURING"])
    handler, inp, _ = _room_handler(
        lambda td, cams: CleanBackgroundBundle(_BAKE_BUNDLE, dict.fromkeys(cams, 100.0), tracks_doc=td),
        camera_check=checker, camera_check_poll_s=0.01, camera_check_margin_s=10_000.0,
    )
    ctx = JobContext("job-cc2", "0192a6f0-7c1e-7b3a-9f2d-000000000647", "site_setup", 1, inp, timeout_ms=600_000)
    result = handler(ctx)
    assert result["camera_check"]["status"] == "INCOMPLETE" and result["camera_check"]["problems"]
    assert schema_errors(result, _RESULT) == []
    assert checker.stopped >= 1


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


def _ctx(name: str, inp: dict[str, Any], t: _FakeTime, timeout_ms: int = 900_000) -> JobContext:
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
    ctx = _ctx("job-hr4", inp, t, timeout_ms=400_000)
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
