"""실측 카메라 높이 + 현장 정물(라바콘·개구부 안내판) 공동 축척 보정 시험 (S15P31A101-462).

검증 항목:
1. 실측 정물 카탈로그(`measured_objects.json`)에 `traffic_cone`과 `opening_sign`이 등록되고 `pending`에서 라바콘이 제거되었는지 확인.
2. 합성 4카메라 현장에 3D 라바콘(높이 67.5 cm, 정사각 밑판 한 변 37 cm, 꼭대기 지름 4.5 cm, 흰 반사띠 2줄 + 밑판 흰 스티커)을 렌더링하고,
   축척 오차와 바닥 Z 오프셋 및 카메라 높이 노이즈를 주입했을 때:
   - 2D 꼭대기·밑판 모서리 검출 및 다중 시점 삼각측량이 정확히 복원되는지
   - 카메라 높이와 라바콘 높이를 한 최소제곱식에서 동시에 푸는 공동 추정(`scale_source="JOINT"`)이
     높이 단독(`HEIGHT_ONLY`)과 정물 단독(`PROP_ONLY`)보다 오차를 줄이고 참값 축척·바닥 오프셋을 복원하는지
   - Stage A(`run_stage_a_rotation_ba`) 결과 보정 문서가 `calibration.schema.json`을 통과하고 `scale_source`, `scale_estimation`,
     `object_cross_checks`를 기록하는지
3. 카메라 높이와 정물 크기가 허용치(5%) 넘게 어긋날 때:
   - 평균내지 않고 경고를 발생시키며(`disagreement_warning=True`), 어느 카메라 높이 또는 어느 정물이 이상치인지(`outliers`) 표시하고,
     적용 축척은 높이 단독(`scale_source="HEIGHT_ONLY"`, `applied_scale == height_only_scale`)으로 유지하면서 공동 추정치를 참고로 기록하는지
4. 정물이 없는 장면(또는 1대 카메라에만 보이는 경우)에서 기존 높이 단독 동작과 완전히 동일한지
5. 12차 오프라인 데이터(있을 때)에서 4대 카메라 라바콘 삼각측량 높이가 실측 67.5 cm와 1 cm 이내로 일치하는지
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

cv2 = pytest.importorskip("cv2")

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.extrinsics.joint_ba_v2 import (
    CAMS,
    PAIRS,
    project,
    run_stage_a_rotation_ba,
)
from simai_ai.calibration.world_frame import measured_objects as mo
from simai_ai.calibration.world_frame.scale_props import (
    OPENING_SIGN_BOTTOM_DIAGONAL_M,
    OPENING_SIGN_EDGE_BAND_M,
    OPENING_SIGN_HEIGHT_M,
    OPENING_SIGN_SIDE_EDGE_M,
    OPENING_SIGN_WIDTH_M,
    TRAFFIC_CONE_BASE_SIDE_M,
    TRAFFIC_CONE_HEIGHT_M,
    TRAFFIC_CONE_TOP_DIAMETER_M,
    detect_and_triangulate_cone_from_images,
    detect_traffic_cone_endpoints,
    estimate_joint_scale_and_floor,
)
from simai_ai.providers.contract import schema_errors

SESSION12_CALIB_PATH = Path(
    "C:/Users/SSAFY/simai-local/session12/results/lobby-s12/state/config/bundles/"
    "01447ea4d264ca541a3ea250fa57f22c029447719e49b4cf16946a5c67f32650/calibration.json"
)
SESSION12_BUNDLE_DIR = Path(
    "C:/Users/SSAFY/simai-local/session12/results/"
    "cal-lobby-init-f0ab1c6b-jba2-2f6cc097/bundle-393d5fbdabad41749559af4526a0a2ce"
)


def _render_cone_bgr(
    K: np.ndarray,
    R: np.ndarray,
    C: np.ndarray,
    cone_center_xy: tuple[float, float] = (3.0, 3.2),
    floor_z: float = 0.0,
    height_m: float = TRAFFIC_CONE_HEIGHT_M,
    base_side_m: float = TRAFFIC_CONE_BASE_SIDE_M,
    top_diam_m: float = TRAFFIC_CONE_TOP_DIAMETER_M,
    image_size: tuple[int, int] = (2304, 1296),
) -> np.ndarray:
    """3D 라바콘(정사각 밑판 + 원뿔대 몸통 + 흰 반사띠 2줄 + 밑판 모서리 흰 스티커)을 핀홀 카메라 영상(BGR)에 렌더링한다."""
    w, h = image_size
    img = np.full((h, w, 3), (185, 190, 195), dtype=np.uint8)
    cx, cy = cone_center_xy
    half = base_side_m * 0.5

    def proj(pts3d: np.ndarray) -> np.ndarray:
        xc = (pts3d - C[None, :]) @ R.T
        uv = (xc[:, :2] / xc[:, 2:3]) @ K[:2, :2].T + K[:2, 2]
        return np.round(uv).astype(np.int32)

    # 1. 정사각 밑판 (주황색)
    theta = np.radians(0.0)
    rot2d = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]], dtype=float)
    local_sq = np.array([[-half, -half], [half, -half], [half, half], [-half, half]], dtype=float) @ rot2d.T
    base_corners = np.column_stack([cx + local_sq[:, 0], cy + local_sq[:, 1], np.full(4, floor_z, dtype=float)])
    cv2.fillConvexPoly(img, proj(base_corners), (15, 70, 235))

    # 밑판 한쪽 모서리에 흰 바코드 스티커 렌더링 (실제 12차 라바콘과 같은 조건)
    local_st = (
        np.array(
            [
                [-half + 0.01, half - 0.09],
                [-half + 0.09, half - 0.09],
                [-half + 0.09, half - 0.01],
                [-half + 0.01, half - 0.01],
            ],
            dtype=float,
        )
        @ rot2d.T
    )
    sticker = np.column_stack([cx + local_st[:, 0], cy + local_st[:, 1], np.full(4, floor_z + 0.002, dtype=float)])
    cv2.fillConvexPoly(img, proj(sticker), (240, 240, 240))

    # 2. 원뿔대 몸통을 층별로 렌더링 (주황/흰 반사띠/주황/흰 반사띠/주황 꼭대기)
    r_bot = 0.135
    r_top = top_diam_m * 0.5
    bands = [
        (0.00, 0.28, (15, 70, 235)),
        (0.28, 0.48, (242, 242, 242)),
        (0.48, 0.62, (15, 70, 235)),
        (0.62, 0.82, (242, 242, 242)),
        (0.82, 1.00, (15, 70, 235)),
    ]
    angles = np.linspace(0.0, 2.0 * np.pi, 36, endpoint=False)
    for f0, f1, color in bands:
        z0 = floor_z + f0 * height_m
        z1 = floor_z + f1 * height_m
        r0 = r_bot + (r_top - r_bot) * f0
        r1 = r_bot + (r_top - r_bot) * f1
        ring0 = np.column_stack([cx + r0 * np.cos(angles), cy + r0 * np.sin(angles), np.full_like(angles, z0)])
        ring1 = np.column_stack([cx + r1 * np.cos(angles), cy + r1 * np.sin(angles), np.full_like(angles, z1)])
        pts2d = proj(np.vstack([ring0, ring1]))
        hull = cv2.convexHull(pts2d)
        cv2.fillConvexPoly(img, hull, color)
    return img


def _make_synthetic_cone_scene(
    *,
    unscaled_factor: float = 1.0 / 1.06,
    true_floor_offset_m: float = 0.035,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, np.ndarray], dict[str, float]]:
    """축척 오차(`unscaled_factor`)와 바닥 오프셋(`true_floor_offset_m`)이 섞인 4카메라 장면 + 라바콘 영상을 만든다."""
    true_heights = {"cam_A": 2.40, "cam_B": 2.42, "cam_C": 2.46, "cam_D": 2.44}
    true_centers = np.array(
        [
            [0.0, 0.0, true_heights["cam_A"]],
            [6.0, 0.0, true_heights["cam_B"]],
            [6.0, 6.0, true_heights["cam_C"]],
            [0.0, 6.0, true_heights["cam_D"]],
        ],
        dtype=float,
    )
    target = np.array([3.0, 3.2, 0.6], dtype=float)
    rotations = []
    for c in true_centers:
        forward = target - c
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        rotations.append(np.vstack((right, np.cross(forward, right), forward)))
    R_true = np.array(rotations, dtype=float)
    K = np.tile([[1100.0, 0.0, 1152.0], [0.0, 1100.0, 648.0], [0.0, 0.0, 1.0]], (4, 1, 1))

    # 실제 물리 공간(true_centers, floor_z=0)에서 라바콘 4카메라 영상을 렌더링한다.
    images_bgr = {
        cid: _render_cone_bgr(K[i], R_true[i], true_centers[i], cone_center_xy=(3.0, 3.2), floor_z=0.0)
        for i, cid in enumerate(CAMS)
    }

    # 초기 복원 좌표계는 참값에 비해 축척이 unscaled_factor 배이고 바닥이 +true_floor_offset_m 만큼 떠 있다고 가정:
    # X_true = s_true * X_init - [0, 0, dz_true]  <=>  X_init = unscaled_factor * (X_true + [0, 0, dz_true])
    C_init = unscaled_factor * (true_centers + np.array([0.0, 0.0, true_floor_offset_m]))

    rng = np.random.default_rng(462)
    tracks: list[dict[str, Any]] = []
    for a, b in PAIRS:
        pts_true = rng.uniform([1.2, 1.2, 0.2], [4.8, 4.8, 1.8], (32, 3))
        pts_init = unscaled_factor * (pts_true + np.array([0.0, 0.0, true_floor_offset_m]))
        uv = np.stack([project(pts_init, c, K, R_true, C_init)[0] for c in (a, b)], axis=1)
        for i, pair_uv in enumerate(uv):
            tracks.append(
                {
                    "id": f"{a}-{b}:{i}",
                    "pair": [a, b],
                    "uv": pair_uv.tolist(),
                    "nodes": [[a, 1000 * b + i], [b, 1000 * a + i]],
                    "split": "holdout" if i % 4 == 0 else "train",
                }
            )

    R_init = R_true.copy()
    R_init[1] = Rotation.from_euler("x", 1.2, degrees=True).as_matrix() @ R_init[1]
    cameras = []
    for idx, cid in enumerate(CAMS):
        R_c = R_init[idx]
        t_c = -R_c @ C_init[idx]
        cameras.append(
            {
                "camera_id": cid,
                "image_size": [2304, 1296],
                "K": K[idx].tolist(),
                "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.0] * 5},
                "R": R_c.tolist(),
                "t": t_c.tolist(),
                "camera_height_m": round(float(C_init[idx, 2]), 4),
                "reprojection_error_px": {"median": 3.5, "p95": 8.0},
            }
        )
    doc: dict[str, Any] = {
        "schema_version": "2.1.0",
        "calibration_version": "cal-cone-synth-001",
        "provider_source": "G1_AUTO",
        "provider_version": "simai-session-calibration-0.2.0",
        "state": "PROVISIONAL",
        "created_at": 1760342400000,
        "coordinate_frame": {
            "frame": "WORLD",
            "unit": "m",
            "handedness": "right",
            "up_axis": "+Z",
            "site_id": "site-cone-01",
        },
        "scale_basis": "MEASURED_CAMERA_HEIGHT",
        "world_axes_source": "WALL_AND_FLOOR",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }
    doc["content_hash"] = content_hash(doc)
    pairs_meta = {
        f"{CAMS[a]}-{CAMS[b]}": {
            "train": sum(t["pair"] == [a, b] and t["split"] == "train" for t in tracks),
            "holdout": sum(t["pair"] == [a, b] and t["split"] == "holdout" for t in tracks),
        }
        for a, b in PAIRS
    }
    return doc, {"tracks": tracks, "pairs": pairs_meta}, images_bgr, true_heights


def test_catalog_registers_traffic_cone_and_opening_sign() -> None:
    objs, _ = mo.load_catalog()
    by_id = {o.id: o for o in objs}
    assert "traffic_cone" in by_id
    assert "opening_sign" in by_id

    cone = by_id["traffic_cone"]
    assert cone.edges["height"] == pytest.approx(TRAFFIC_CONE_HEIGHT_M)
    assert cone.edges["base_side"] == pytest.approx(TRAFFIC_CONE_BASE_SIDE_M)
    assert cone.edges["top_diameter"] == pytest.approx(TRAFFIC_CONE_TOP_DIAMETER_M)

    sign = by_id["opening_sign"]
    assert sign.edges["width"] == pytest.approx(OPENING_SIGN_WIDTH_M)
    assert sign.edges["height"] == pytest.approx(OPENING_SIGN_HEIGHT_M)
    assert sign.edges["side_edge"] == pytest.approx(OPENING_SIGN_SIDE_EDGE_M)
    assert sign.edges["bottom_diagonal"] == pytest.approx(OPENING_SIGN_BOTTOM_DIAGONAL_M)
    assert sign.raw["dimensions_m"]["edge_band"] == pytest.approx(OPENING_SIGN_EDGE_BAND_M)

    raw_doc = json.loads(mo.DEFAULT_CATALOG.read_text(encoding="utf-8"))
    pending_names = [p["name"] for p in raw_doc.get("pending", [])]
    assert "라바콘" not in pending_names


def test_joint_scale_and_floor_recovers_true_scale_in_synthetic_4cam_scene() -> None:
    true_scale = 1.07
    true_dz = 0.04
    calib_doc, tracks_doc, images_bgr, true_heights = _make_synthetic_cone_scene(
        unscaled_factor=1.0 / true_scale,
        true_floor_offset_m=true_dz,
    )

    # 카메라 입력 높이에 ±1.5 cm 실측 노이즈 주입
    noisy_heights = {
        "cam_A": true_heights["cam_A"] + 0.015,
        "cam_B": true_heights["cam_B"] - 0.010,
        "cam_C": true_heights["cam_C"] + 0.012,
        "cam_D": true_heights["cam_D"] - 0.015,
    }

    res = run_stage_a_rotation_ba(
        calib_doc,
        tracks_doc,
        camera_heights_m=noisy_heights,
        height_tolerance_cm=5.0,
        anchor_camera_id="cam_D",
        backgrounds_by_cam=images_bgr,
    )

    assert res.converged is True
    assert res.scale_source == "JOINT"
    assert res.scale_estimation is not None
    est = res.scale_estimation
    assert est["scale_source"] == "JOINT"
    assert est["disagreement_warning"] is False
    assert est["prop_only_scale"] == pytest.approx(true_scale, rel=0.03)
    assert est["joint_scale"] == pytest.approx(true_scale, rel=0.015)
    assert abs(est["joint_scale"] - true_scale) < abs(est["height_only_scale"] - true_scale)
    assert abs(est["joint_scale"] - true_scale) < abs(est["prop_only_scale"] - true_scale)
    assert est["applied_scale"] == pytest.approx(est["joint_scale"], abs=1e-6)
    assert est["floor_offset_m"] == pytest.approx(true_dz, abs=0.02)

    # 보정 문서 계약 검증 및 scale_source / scale_estimation 기록 확인
    assert schema_errors(res.calibration_doc, "calibration") == []
    assert res.calibration_doc["scale_source"] == "JOINT"
    assert res.calibration_doc["scale_estimation"]["joint_scale"] == est["joint_scale"]
    assert "MEASURED_OBJECT" in res.calibration_doc["scale_cross_check_basis"]

    # 공동 추정 후 카메라 높이 오차가 모두 허용치(5 cm) 이내로 들어왔는지 확인
    for cid in CAMS:
        assert res.height_checks[cid]["within_tolerance"] is True
        assert abs(res.height_checks[cid]["difference_cm"]) < 2.5

    # object_cross_checks 에 TRAFFIC_CONE 통과 기록이 남는지 확인
    assert len(res.object_cross_checks) == 1
    chk = res.object_cross_checks[0]
    assert chk["kind"] == "TRAFFIC_CONE"
    assert chk["passed"] is True
    assert len(chk["cameras"]) == 4


def test_disagreement_above_5pct_emits_warning_and_keeps_height_only_scale() -> None:
    calib_doc, tracks_doc, images_bgr, true_heights = _make_synthetic_cone_scene(
        unscaled_factor=1.0,
        true_floor_offset_m=0.0,
    )
    # 카메라 높이 입력이 정물(라바콘)과 15% 어긋나는 상황 주입
    wrong_heights = {cid: h * 1.15 for cid, h in true_heights.items()}

    res = run_stage_a_rotation_ba(
        calib_doc,
        tracks_doc,
        camera_heights_m=wrong_heights,
        height_tolerance_cm=5.0,
        anchor_camera_id="cam_D",
        backgrounds_by_cam=images_bgr,
        disagreement_tolerance=0.05,
    )

    assert res.scale_source == "HEIGHT_ONLY"
    est = res.scale_estimation
    assert est is not None
    assert est["scale_source"] == "HEIGHT_ONLY"
    assert est["disagreement_warning"] is True
    assert est["disagreement_ratio"] > 0.05
    # 평균내지 않고 높이 단독 축척(applied_scale == height_only_scale)을 유지하며, 공동 추정치는 참고로 기록한다
    assert est["applied_scale"] == pytest.approx(est["height_only_scale"], abs=1e-6)
    assert est["floor_offset_m"] == 0.0
    assert est["joint_scale"] != pytest.approx(est["applied_scale"], rel=0.01)
    assert len(est.get("outliers", [])) >= 1
    assert "warning_message" in est and "5.0%" in est["warning_message"]

    # object_cross_checks 에도 불일치 경고가 기록된다
    assert len(res.object_cross_checks) == 1
    assert res.object_cross_checks[0]["passed"] is False
    assert schema_errors(res.calibration_doc, "calibration") == []


def test_scene_without_prop_or_single_camera_only_preserves_height_only_behavior() -> None:
    calib_doc, tracks_doc, images_bgr, true_heights = _make_synthetic_cone_scene(
        unscaled_factor=1.0,
        true_floor_offset_m=0.0,
    )
    blank_imgs = {cid: np.full((1296, 2304, 3), 180, dtype=np.uint8) for cid in CAMS}

    res_no_prop = run_stage_a_rotation_ba(
        calib_doc,
        tracks_doc,
        camera_heights_m=true_heights,
        anchor_camera_id="cam_D",
        backgrounds_by_cam=blank_imgs,
    )
    assert res_no_prop.scale_source == "HEIGHT_ONLY"
    assert res_no_prop.cone_observation is None
    assert res_no_prop.object_cross_checks == []
    for cid in CAMS:
        assert res_no_prop.pose_changes[cid]["center_cm"] == 0.0

    # 1대 카메라(cam_A)에만 라바콘이 보일 때는 삼각측량하지 않고 HEIGHT_ONLY 유지
    one_cam_imgs = copy.deepcopy(blank_imgs)
    one_cam_imgs["cam_A"] = images_bgr["cam_A"]
    assert len(detect_traffic_cone_endpoints(one_cam_imgs["cam_A"], camera_id="cam_A")) == 1
    cone_single, _ = detect_and_triangulate_cone_from_images(calib_doc, one_cam_imgs)
    assert cone_single is None


@pytest.mark.skipif(
    not (SESSION12_CALIB_PATH.exists() and SESSION12_BUNDLE_DIR.exists()),
    reason="Session 12 local data not present on this machine",
)
def test_session12_real_data_cone_triangulation_and_joint_scale() -> None:
    calib_doc = json.loads(SESSION12_CALIB_PATH.read_text(encoding="utf-8"))
    images = {cid: SESSION12_BUNDLE_DIR / f"{cid}.jpg" for cid in CAMS}
    cone, _ = detect_and_triangulate_cone_from_images(calib_doc, images)
    assert cone is not None
    assert len(cone.apex_cameras) == 4
    assert len(cone.base_corners) == 3
    # 12차 보정에서 삼각측량한 라바콘 높이는 실측 67.5 cm 와 1 cm 이내(약 67.78 cm, +0.28 cm)로 일치한다
    assert abs(cone.measured_height_m - TRAFFIC_CONE_HEIGHT_M) < 0.01

    restored_heights = {c["camera_id"]: float(c["camera_height_m"]) for c in calib_doc["cameras"]}
    input_heights = {"cam_A": 2.44, "cam_B": 2.44, "cam_C": 2.48, "cam_D": 2.48}
    est = estimate_joint_scale_and_floor(restored_heights, input_heights, cone=cone)
    assert est["scale_source"] == "JOINT"
    assert est["disagreement_warning"] is False
    assert abs(est["prop_only_scale"] - 1.0) < 0.01
    assert abs(est["joint_scale"] - 1.0) < 0.01
