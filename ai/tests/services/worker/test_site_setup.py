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
    camera_ids: tuple[str, ...] = ("cam1", "cam2", "cam3", "cam4"),
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
    # cam2 의 실제 높이가 1.59m 로 입력(1.50m)과 +9cm 차이(> ±5cm 허용 오차)가 나도록 설정
    restored = {"cam1": 1.49, "cam2": 1.59, "cam3": 1.48, "cam4": 1.47}
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
    assert hc["cam2"]["within_tolerance"] is False
    assert hc["cam2"]["difference_cm"] > 5.0
    assert hc["cam1"]["within_tolerance"] is True


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
                collection_pct_by_cam={"cam1": 100.0, "cam2": 60.0, "cam3": 100.0, "cam4": 100.0},
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
        calibration_publisher=_publish,
        **kw,
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
