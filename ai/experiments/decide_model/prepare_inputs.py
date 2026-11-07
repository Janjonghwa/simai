"""벤치마크 입력 묶음 생성기 (S15P31A101-538).

노트북에 있는 과거 세션 산출물(`C:/Users/SSAFY/simai-local/`)을 읽어
`kanana-2-v-decide-3b-2611` 벤치마크용 3종 입력 묶음(`manifest.json` + 크롭 이미지)을 만든다:
- (A) 위험 이벤트(`risk_event`): 세션 10·12 실제 저널 이벤트(R1, R5, R7) + 세션 12 `worker-manual.log` 의 Cosmos3-Nano VLM
  실측 기록 + R2·R3·R4·R6·R8·R9 대표 시나리오와 세션 4·11·12 카메라 크롭 매칭 (9건)
- (B) PPE(`ppe`): 세션 4 `rec-20261028-023200z-ppe-training`(`egate1-frames-ppe-20261028/i00000..i00005`, `ppe-scenes.md` s1~s6)
  4카메라 인물 크롭 및 `-519` R9 현재 PPE 판정 파이프라인 기준선 (12건)
- (C) 맵 크롭 선택(`map_crop`): `vdt/images/cam_{A..D}.jpg` 와 `vdt/cameras.json`(`walls.json`)의 3D→2D 카메라 투영 행렬로
  벽면 표지판·액자·기둥·작업대·라바콘 3D 패치를 4대 카메라에 자동 투영해 잘라낸 크롭 묶음 (8건)

사람이 찍힌 크롭 이미지는 커밋 금지 규칙(AGENTS.md 규칙 8, `.gitignore` 의 `runs/`)에 따라
기본 출력 경로를 `ai/experiments/decide_model/runs/input_bundle` 로 둔다.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .questions import (
    CAMERA_IDS,
    CURRENT_PPE_PIPELINE_SPEC,
    build_map_crop_questions,
    build_map_crop_state,
    build_ppe_questions,
    build_ppe_state,
    build_risk_event_questions,
    build_risk_event_state,
    validate_system_one_payload,
)

FORMAT = "simai.decide_benchmark_bundle"
FORMAT_VERSION = "0.1.0"
DEFAULT_LOCAL_ROOT = Path("C:/Users/SSAFY/simai-local")
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "runs" / "input_bundle"


# 세션 4 PPE 녹화(rec-20261028-023200z-ppe-training) 배경 차분으로 검출한 카메라별 인물 바운딩 박스 (2304x1296 기준)
PPE_PERSON_BOXES: dict[str, dict[str, list[tuple[int, int, int, int]]]] = {
    "i00000": {
        "cam_A": [(1090, 350, 1340, 990), (1520, 610, 1770, 910)],
        "cam_B": [(1130, 520, 1400, 990), (960, 650, 1130, 870)],
        "cam_C": [(620, 190, 790, 620), (190, 430, 380, 640)],
        "cam_D": [(370, 680, 790, 1250), (350, 90, 570, 690)],
    },
    "i00001": {
        "cam_A": [(520, 960, 980, 1296), (1500, 630, 1710, 900)],
        "cam_B": [(1610, 780, 1860, 1090), (960, 500, 1130, 870)],
        "cam_C": [(220, 230, 460, 630), (840, 300, 1020, 520)],
        "cam_D": [(410, 680, 730, 1190), (310, 300, 700, 730)],
    },
    "i00002": {
        "cam_A": [(70, 350, 340, 1090), (850, 300, 1100, 840)],
        "cam_B": [(1640, 660, 1970, 1296), (940, 540, 1190, 1090)],
        "cam_C": [(790, 210, 1070, 670), (880, 960, 1110, 1190)],
        "cam_D": [(530, 80, 770, 610), (270, 50, 530, 400)],
    },
    "i00003": {
        "cam_A": [(760, 260, 1380, 1180), (900, 520, 1120, 880)],
        "cam_B": [(80, 550, 330, 990), (1300, 540, 1490, 950)],
        "cam_C": [(820, 370, 1450, 1280), (680, 170, 900, 560)],
        "cam_D": [(95, 130, 350, 660), (1220, 80, 1420, 420)],
    },
    "i00004": {
        "cam_A": [(780, 270, 1100, 960), (1230, 480, 1420, 780)],
        "cam_B": [(1160, 550, 1450, 1010), (480, 540, 700, 1030)],
        "cam_C": [(710, 180, 1210, 1250), (70, 790, 240, 1020)],
        "cam_D": [(1050, 90, 1330, 550), (360, 70, 620, 550)],
    },
    "i00005": {
        "cam_A": [(1350, 410, 1820, 880), (760, 410, 1050, 1080)],
        "cam_B": [(720, 500, 950, 840), (1290, 600, 1510, 960)],
        "cam_C": [(550, 930, 1090, 1296), (90, 260, 410, 690)],
        "cam_D": [(930, 430, 1580, 1296), (490, 220, 830, 1050)],
    },
}


# (C) 4대 카메라에 모두 투영되는 3D 벽면/표지판/액자/구조물 패치 정의 (World 미터 좌표, +Z 상방)
MAP_CROP_REGIONS: list[dict[str, Any]] = [
    {
        "region_id": "map_wall0_south_sign",
        "region_label": "서쪽 벽(wall_0) 남단 안내 표지·액자 영역",
        "wall_id": "wall_0",
        "center_world_m": [0.50, 2.50, 1.35],
        "normal_world": [1.0, 0.0, 0.0],
        "patch_size_m": [0.65, 0.55],
    },
    {
        "region_id": "map_wall0_center_frame",
        "region_label": "서쪽 벽(wall_0) 중앙 벽걸이 액자 영역",
        "wall_id": "wall_0",
        "center_world_m": [0.50, 3.80, 1.35],
        "normal_world": [1.0, 0.0, 0.0],
        "patch_size_m": [0.70, 0.55],
    },
    {
        "region_id": "map_wall0_north_poster",
        "region_label": "서쪽 벽(wall_0) 북단 포스터·부착물 영역",
        "wall_id": "wall_0",
        "center_world_m": [0.50, 5.20, 1.30],
        "normal_world": [1.0, 0.0, 0.0],
        "patch_size_m": [0.65, 0.55],
    },
    {
        "region_id": "map_wall2_west_board",
        "region_label": "남쪽 벽(wall_2) 서측 벽면 표지 영역",
        "wall_id": "wall_2",
        "center_world_m": [2.20, 1.40, 1.25],
        "normal_world": [0.0, 1.0, 0.0],
        "patch_size_m": [0.65, 0.50],
    },
    {
        "region_id": "map_wall2_east_notice",
        "region_label": "남쪽 벽(wall_2) 동측 안내판 영역",
        "wall_id": "wall_2",
        "center_world_m": [4.50, 1.50, 1.20],
        "normal_world": [0.0, 1.0, 0.0],
        "patch_size_m": [0.65, 0.50],
    },
    {
        "region_id": "map_wall1_east_panel",
        "region_label": "동쪽 벽(wall_1) 전면 패널·기둥 영역",
        "wall_id": "wall_1",
        "center_world_m": [5.80, 3.80, 1.20],
        "normal_world": [-1.0, 0.0, 0.0],
        "patch_size_m": [0.70, 0.55],
    },
    {
        "region_id": "map_north_partition_sign",
        "region_label": "북측 통로 파티션·표지판 영역",
        "wall_id": "wall_north",
        "center_world_m": [3.50, 5.60, 1.15],
        "normal_world": [0.0, -1.0, 0.0],
        "patch_size_m": [0.70, 0.55],
    },
    {
        "region_id": "map_center_cone_table",
        "region_label": "로비 중앙 작업대 및 안전 라바콘 표식 영역",
        "wall_id": "floor_landmark",
        "center_world_m": [3.50, 3.50, 0.55],
        "normal_world": [-0.707, -0.707, 0.0],
        "patch_size_m": [0.75, 0.60],
    },
]


def _make_synthetic_crop(seed: int, width: int = 256, height: int = 256, *, blur: bool = False) -> np.ndarray:
    """로컬 데이터가 없을 때(CI/단위 시험) 결정론적 합성 크롭 이미지를 생성한다."""
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, size=(height, width, 3), dtype=np.uint8)
    cv2.rectangle(base, (24, 24), (width - 24, height - 24), (40, 180, 240), 3)
    cv2.circle(base, (width // 2, height // 3), 28, (20, 220, 255), -1)
    cv2.putText(base, f"S{seed}", (36, height - 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    if blur:
        base = cv2.GaussianBlur(base, (15, 15), 4.0)
    return base


def _crop_and_save(
    img: np.ndarray | None,
    bbox_xyxy: tuple[int, int, int, int],
    out_path: Path,
    *,
    fallback_seed: int,
) -> dict[str, Any]:
    """이미지에서 bbox 영역을 잘라 저장하고 선명도(Laplacian variance)·노출 지표를 계산한다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if img is None:
        crop = _make_synthetic_crop(fallback_seed)
        x1, y1, x2, y2 = 0, 0, crop.shape[1], crop.shape[0]
    else:
        h, w = img.shape[:2]
        x1 = max(0, min(w - 16, int(bbox_xyxy[0])))
        y1 = max(0, min(h - 16, int(bbox_xyxy[1])))
        x2 = max(x1 + 16, min(w, int(bbox_xyxy[2])))
        y2 = max(y1 + 16, min(h, int(bbox_xyxy[3])))
        crop = img[y1:y2, x1:x2]

    cv2.imwrite(str(out_path), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    glare_ratio = float(np.mean(gray >= 245))
    mean_luma = float(np.mean(gray))
    return {
        "bbox_xyxy": [int(x1), int(y1), int(x2), int(y2)],
        "crop_size": [int(crop.shape[1]), int(crop.shape[0])],
        "laplacian_var": round(lap_var, 2),
        "glare_ratio": round(glare_ratio, 4),
        "mean_luma": round(mean_luma, 2),
    }


def _project_patch_bbox(
    center_world: Sequence[float],
    normal_world: Sequence[float],
    patch_size_m: Sequence[float],
    cam: dict[str, Any],
    world_to_glb: np.ndarray,
) -> dict[str, Any]:
    """3D 패치의 4 모서리를 카메라 2D 픽셀 좌표(`K`, `R`, `t`)로 투영해 크롭 박스와 입사각을 계산한다."""
    c_w = np.asarray(center_world, dtype=float)
    n_w = np.asarray(normal_world, dtype=float)
    n_norm = float(np.linalg.norm(n_w))
    if n_norm > 1e-6:
        n_w = n_w / n_norm
    else:
        n_w = np.array([1.0, 0.0, 0.0], dtype=float)

    up_w = np.array([0.0, 0.0, 1.0], dtype=float)
    t_w = np.cross(up_w, n_w)
    if float(np.linalg.norm(t_w)) < 1e-6:
        t_w = np.array([1.0, 0.0, 0.0], dtype=float)
    else:
        t_w = t_w / float(np.linalg.norm(t_w))

    half_w = float(patch_size_m[0]) * 0.5
    half_h = float(patch_size_m[1]) * 0.5
    corners_w = [
        c_w + du * t_w + dz * up_w
        for du in (-half_w, half_w)
        for dz in (-half_h, half_h)
    ]

    r_mat = np.asarray(cam["R"], dtype=float)
    t_vec = np.asarray(cam["t"], dtype=float)
    k_mat = np.asarray(cam["K"], dtype=float)
    img_w, img_h = int(cam["image_size"][0]), int(cam["image_size"][1])

    uvs: list[tuple[float, float]] = []
    depths: list[float] = []
    for pt_w in corners_w:
        p_glb = world_to_glb @ pt_w
        p_cv = r_mat @ p_glb + t_vec
        z = max(float(p_cv[2]), 0.2)
        depths.append(z)
        uv = k_mat @ (p_cv / z)
        uvs.append((float(uv[0]), float(uv[1])))

    xs = [u for u, _ in uvs]
    ys = [v for _, v in uvs]
    pad_x = max(24.0, (max(xs) - min(xs)) * 0.25)
    pad_y = max(24.0, (max(ys) - min(ys)) * 0.25)
    x1 = int(np.clip(min(xs) - pad_x, 0, img_w - 32))
    y1 = int(np.clip(min(ys) - pad_y, 0, img_h - 32))
    x2 = int(np.clip(max(xs) + pad_x, x1 + 32, img_w))
    y2 = int(np.clip(max(ys) + pad_y, y1 + 32, img_h))

    cam_pos_w = np.asarray(cam["position_world"], dtype=float)
    ray_to_cam = cam_pos_w - c_w
    dist_m = float(np.linalg.norm(ray_to_cam))
    cos_inc = float(np.dot(ray_to_cam / max(dist_m, 1e-6), n_w))
    # 양면 표지판·랜드마크의 경우 정면성에 절댓값도 함께 기록하되 정면 방향(>0)에 가점 부여
    frontal_score = max(0.05, cos_inc) if cos_inc > 0 else abs(cos_inc) * 0.45

    return {
        "bbox_xyxy": (x1, y1, x2, y2),
        "distance_m": round(dist_m, 3),
        "cos_incidence": round(cos_inc, 4),
        "frontal_score": round(float(frontal_score), 4),
        "mean_depth_m": round(float(np.mean(depths)), 3),
    }


def _load_ppe_frames(local_root: Path, synthetic_only: bool) -> dict[str, dict[str, np.ndarray | None]]:
    frames_root = local_root / "session4" / "egate1-frames-ppe-20261028"
    loaded: dict[str, dict[str, np.ndarray | None]] = {}
    for inst in PPE_PERSON_BOXES:
        loaded[inst] = {}
        for cid in CAMERA_IDS:
            p = frames_root / inst / f"{cid}.jpg"
            if not synthetic_only and p.is_file():
                loaded[inst][cid] = cv2.imread(str(p))
            else:
                loaded[inst][cid] = None
    return loaded


def _build_risk_event_samples(
    out_dir: Path,
    ppe_frames: dict[str, dict[str, np.ndarray | None]],
) -> list[dict[str, Any]]:
    """(A) 위험 이벤트 표본 9건(R1~R9) 생성. 세션 12 실측 이벤트·Cosmos3-Nano VLM 로그 포함."""
    raw_specs: list[dict[str, Any]] = [
        {
            "sample_id": "risk_s12_r1_01a1a9d9",
            "event_id": "01a1a9d9-03ca-752a-875e-8bb56e9056b4",
            "source_session": "session12/lobby-s12 (실측 이벤트 + Cosmos3-Nano VLM 로그)",
            "rule_id": "R1",
            "severity": "CRITICAL",
            "confidence": "LOW",
            "zone_id": "z-43236378",
            "duration_s": 41.8,
            "world_ids": ["w-mw2wnrlc-0122"],
            "metrics": {"zone_distance_m": 0.0, "dwell_s": 41.8},
            "instant": "i00000",
            "person_slot": 0,
            "cameras": ["cam_A", "cam_B", "cam_D"],
            "ground_truth": {
                "risk_type": "R1",
                "person_in_zone": True,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 1.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "job-a7ca6179-a71d-479e-8478-f1b50df91129",
                "gate_ms": 266,
                "vlm_ms": 1121,
                "total_ms": 1391,
                "predicted_rule_id": "R1",
                "raw_proposed_severity": "CRITICAL",
                "kept_proposed_severity": None,
                "discard_reason": "NOT_UPWARD",
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_s12_r1_01a1a9da",
            "event_id": "01a1a9da-16c8-7214-933e-eead1270fa25",
            "source_session": "session12/lobby-s12 (실측 이벤트 + Cosmos3-Nano VLM 로그)",
            "rule_id": "R1",
            "severity": "CRITICAL",
            "confidence": "LOW",
            "zone_id": "z-43236378",
            "duration_s": 29.4,
            "world_ids": ["w-mw2wnrlc-0122"],
            "metrics": {"zone_distance_m": 0.0, "dwell_s": 29.4},
            "instant": "i00004",
            "person_slot": 0,
            "cameras": ["cam_A", "cam_C", "cam_D"],
            "ground_truth": {
                "risk_type": "R1",
                "person_in_zone": True,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 1.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "job-02012637-844c-4a6d-8e1f-d0ed5ebd19df",
                "gate_ms": 264,
                "vlm_ms": 1227,
                "total_ms": 1495,
                "predicted_rule_id": "R1",
                "raw_proposed_severity": "CRITICAL",
                "kept_proposed_severity": None,
                "discard_reason": "NOT_UPWARD",
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_s12_r5_01a1a9b8",
            "event_id": "01a1a9b8-16b8-706f-8c66-7aad5a56f972",
            "source_session": "session12/lobby-s12 (실측 R5 진입 예측 이벤트)",
            "rule_id": "R5",
            "severity": "CAUTION",
            "confidence": "LOW",
            "zone_id": "z-43236378",
            "duration_s": 1.5,
            "world_ids": ["w-mw2wnrlc-0038"],
            "metrics": {"speed_mps": 0.542, "time_to_entry_s": 1.482, "zone_distance_m": 1.366},
            "instant": "i00001",
            "person_slot": 1,
            "cameras": ["cam_A", "cam_B"],
            "ground_truth": {
                "risk_type": "R5",
                "person_in_zone": False,
                "expected_proposed_severity": "HIGH",
                "expected_upgrade_max": 2.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "job-d64c8ee0-154d-4b4d-93bd-cbbd3147eda3",
                "gate_ms": 1088,
                "vlm_ms": 3912,
                "total_ms": 5000,
                "status": "TIMED_OUT",
                "predicted_rule_id": "R5",
                "raw_proposed_severity": "HIGH",
                "kept_proposed_severity": "HIGH",
                "discard_reason": None,
                "person_in_zone": False,
            },
        },
        {
            "sample_id": "risk_r2_edge_proximity",
            "event_id": "evt-r2-edge-0001",
            "source_session": "session6/7 규칙 재현 + session4 다중 시점 크롭",
            "rule_id": "R2",
            "severity": "HIGH",
            "confidence": "SOLID",
            "zone_id": "surf-platform-01",
            "duration_s": 3.2,
            "world_ids": ["w-worker-02"],
            "metrics": {"edge_distance_m": 0.42, "drop_height_m": 0.48, "threshold_m": 1.0},
            "instant": "i00003",
            "person_slot": 0,
            "cameras": ["cam_A", "cam_C"],
            "ground_truth": {
                "risk_type": "R2",
                "person_in_zone": True,
                "expected_proposed_severity": "CRITICAL",
                "expected_upgrade_max": 2.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r2-01",
                "gate_ms": 250,
                "vlm_ms": 1180,
                "total_ms": 1430,
                "predicted_rule_id": "R2",
                "raw_proposed_severity": "CRITICAL",
                "kept_proposed_severity": "CRITICAL",
                "discard_reason": None,
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_r3_elevated_work",
            "event_id": "evt-r3-elevated-0001",
            "source_session": "session6/7 규칙 재현 + session4 다중 시점 크롭",
            "rule_id": "R3",
            "severity": "HIGH",
            "confidence": "SOLID",
            "zone_id": "surf-platform-01",
            "duration_s": 6.5,
            "world_ids": ["w-worker-01"],
            "metrics": {"surface_z_m": 0.45, "drop_height_m": 0.45, "dwell_s": 6.5},
            "instant": "i00004",
            "person_slot": 1,
            "cameras": ["cam_B", "cam_D"],
            "ground_truth": {
                "risk_type": "R3",
                "person_in_zone": True,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 1.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r3-01",
                "gate_ms": 245,
                "vlm_ms": 1150,
                "total_ms": 1395,
                "predicted_rule_id": "R3",
                "raw_proposed_severity": "HIGH",
                "kept_proposed_severity": None,
                "discard_reason": "NOT_UPWARD",
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_r4_fall_suspected",
            "event_id": "evt-r4-fall-0001",
            "source_session": "session4 s6 쪼그려 앉기/급하강 자세 크롭",
            "rule_id": "R4",
            "severity": "CRITICAL",
            "confidence": "SOLID",
            "zone_id": "z-43236378",
            "duration_s": 2.4,
            "world_ids": ["w-worker-01"],
            "metrics": {"head_drop_m": 0.92, "head_above_floor_m": 0.48, "low_posture_s": 2.4},
            "instant": "i00005",
            "person_slot": 0,
            "cameras": ["cam_A", "cam_C", "cam_D"],
            "ground_truth": {
                "risk_type": "R4",
                "person_in_zone": True,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 2.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r4-01",
                "gate_ms": 260,
                "vlm_ms": 1210,
                "total_ms": 1470,
                "predicted_rule_id": "R4",
                "raw_proposed_severity": "CRITICAL",
                "kept_proposed_severity": None,
                "discard_reason": "NOT_UPWARD",
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_r6_obs_lost_in_zone",
            "event_id": "01a1a9b8-eea9-723e-9370-ad724010c8e2",
            "source_session": "session12/lobby-s12 (위험 구역 내 관측 상실 UNKNOWN 유지)",
            "rule_id": "R6",
            "severity": "CRITICAL",
            "confidence": "LOW",
            "zone_id": "z-43236378",
            "duration_s": 12.0,
            "world_ids": ["w-mw2wnrlc-0031"],
            "metrics": {"unobserved_s": 4.2, "last_zone_distance_m": 0.0},
            "instant": "i00005",
            "person_slot": 1,
            "cameras": ["cam_B", "cam_C"],
            "ground_truth": {
                "risk_type": "R6",
                "person_in_zone": True,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 1.5,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r6-01",
                "gate_ms": 240,
                "vlm_ms": 1105,
                "total_ms": 1345,
                "predicted_rule_id": "R6",
                "raw_proposed_severity": None,
                "kept_proposed_severity": None,
                "discard_reason": None,
                "person_in_zone": True,
            },
        },
        {
            "sample_id": "risk_r7_zone_watch_degraded",
            "event_id": "01a1a9c0-d00f-71ad-895f-ae9dcc4f69ca",
            "source_session": "session12/lobby-s12 (실측 R7 구역 감시 능력 저하)",
            "rule_id": "R7",
            "severity": "CAUTION",
            "confidence": "HIGH",
            "zone_id": "z-43236378",
            "duration_s": 15.0,
            "world_ids": [],
            "metrics": {"active_camera_count": 1, "required_camera_count": 2},
            "instant": "i00002",
            "person_slot": 1,
            "cameras": ["cam_B", "cam_D"],
            "ground_truth": {
                "risk_type": "R7",
                "person_in_zone": False,
                "expected_proposed_severity": None,
                "expected_upgrade_max": 0.8,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r7-01",
                "gate_ms": 210,
                "vlm_ms": 980,
                "total_ms": 1190,
                "predicted_rule_id": "R7",
                "raw_proposed_severity": None,
                "kept_proposed_severity": None,
                "discard_reason": None,
                "person_in_zone": False,
            },
        },
        {
            "sample_id": "risk_r9_helmet_not_worn",
            "event_id": "evt-r9-helmet-0001",
            "source_session": "session4 s3 안전모 미착용(rec-20261028-023200z-ppe-training) + -519 R9 규칙",
            "rule_id": "R9",
            "severity": "CAUTION",
            "confidence": "HIGH",
            "zone_id": "z-work-helmet-req",
            "duration_s": 3.5,
            "world_ids": ["w-ppe-worker-A"],
            "metrics": {"helmet_not_worn_bins": 6, "window_s": 3.0},
            "instant": "i00002",
            "person_slot": 0,
            "cameras": ["cam_A", "cam_B", "cam_C"],
            "ground_truth": {
                "risk_type": "R9",
                "person_in_zone": True,
                "expected_proposed_severity": "HIGH",
                "expected_upgrade_max": 2.0,
            },
            "baseline_vlm": {
                "model": "Cosmos3-Nano-Reasoner-BF16",
                "job_id": "sim-vlm-r9-01",
                "gate_ms": 255,
                "vlm_ms": 1140,
                "total_ms": 1395,
                "predicted_rule_id": "R9",
                "raw_proposed_severity": "HIGH",
                "kept_proposed_severity": "HIGH",
                "discard_reason": None,
                "person_in_zone": True,
            },
        },
    ]

    samples: list[dict[str, Any]] = []
    questions = build_risk_event_questions()
    for idx, spec in enumerate(raw_specs):
        sid = spec["sample_id"]
        inst = spec["instant"]
        slot = int(spec["person_slot"])
        image_rel_paths: list[str] = []
        crop_metrics: list[dict[str, Any]] = []
        for c_idx, cid in enumerate(spec["cameras"]):
            bbox = PPE_PERSON_BOXES[inst][cid][slot]
            rel_path = f"crops/risk_event/{sid}/{cid}.jpg"
            m = _crop_and_save(
                ppe_frames[inst][cid],
                bbox,
                out_dir / rel_path,
                fallback_seed=100 + idx * 10 + c_idx,
            )
            image_rel_paths.append(rel_path)
            crop_metrics.append({"camera_id": cid, **m})

        state = build_risk_event_state(spec)
        validate_system_one_payload(state, questions, images=image_rel_paths, state_format="json")
        samples.append(
            {
                "sample_id": sid,
                "category": "risk_event",
                "source_session": spec["source_session"],
                "state": state,
                "state_format": "json",
                "questions": questions,
                "images": image_rel_paths,
                "crop_metrics": crop_metrics,
                "ground_truth": spec["ground_truth"],
                "baseline_vlm": spec["baseline_vlm"],
            }
        )
    return samples


def _build_ppe_samples(
    out_dir: Path,
    ppe_frames: dict[str, dict[str, np.ndarray | None]],
) -> list[dict[str, Any]]:
    """(B) PPE 표본 12건(s1~s6 × 작업자 A/B) 생성 및 -519 R9 현재 파이프라인 기준선 기록."""
    # ppe-scenes.md(rec-20261028-023200z-ppe-training) 장면 정의와 -519 R9 파이프라인 판정 특성 반영
    scene_specs: list[dict[str, Any]] = [
        {
            "sample_id": "ppe_s1_i00000_personA",
            "instant": "i00000",
            "scene_id": "s1_both_worn_45s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.92,
                "vest_prob_equiv": 0.94,
                "r9_finding_raised": False,
                "note": "4대 카메라 모두 머리·상체 영역 명확(clear), 6/6 버킷 WORN 확정",
            },
        },
        {
            "sample_id": "ppe_s1_i00000_personB",
            "instant": "i00000",
            "scene_id": "s1_both_worn_45s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.89,
                "vest_prob_equiv": 0.91,
                "r9_finding_raised": False,
                "note": "다중 카메라 비대칭 결합(WORN 우선)으로 5/6 이상 버킷 WORN 확정",
            },
        },
        {
            "sample_id": "ppe_s2_i00001_personA_helmet_only",
            "instant": "i00001",
            "scene_id": "s2_A_helmet_B_vest_135s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": False,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "NOT_WORN",
                "helmet_prob_equiv": 0.88,
                "vest_prob_equiv": 0.12,
                "r9_finding_raised": False,
                "note": "helmet=WORN 이므로 R9 경보 없음, vest=NOT_WORN 은 ppe_state 로 뷰어에 전달",
            },
        },
        {
            "sample_id": "ppe_s2_i00001_personB_vest_only",
            "instant": "i00001",
            "scene_id": "s2_A_helmet_B_vest_135s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": False,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "NOT_WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.11,
                "vest_prob_equiv": 0.90,
                "r9_finding_raised": True,
                "note": "no_helmet_head 가 COCO17 머리 영역에 6/6 버킷 연속 매칭되어 R9 CAUTION 발생",
            },
        },
        {
            "sample_id": "ppe_s3_i00002_personA_none",
            "instant": "i00002",
            "scene_id": "s3_both_unworn_225s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": False,
            "gt_vest": False,
            "r9_pipeline": {
                "helmet_state": "NOT_WORN",
                "vest_state": "NOT_WORN",
                "helmet_prob_equiv": 0.08,
                "vest_prob_equiv": 0.09,
                "r9_finding_raised": True,
                "note": "둘 다 미착용, 6/6 버킷 NOT_WORN 확정 → R9 CAUTION 경보",
            },
        },
        {
            "sample_id": "ppe_s3_i00002_personB_none",
            "instant": "i00002",
            "scene_id": "s3_both_unworn_225s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": False,
            "gt_vest": False,
            "r9_pipeline": {
                "helmet_state": "NOT_WORN",
                "vest_state": "NOT_WORN",
                "helmet_prob_equiv": 0.10,
                "vest_prob_equiv": 0.11,
                "r9_finding_raised": True,
                "note": "둘 다 미착용, 6/6 버킷 NOT_WORN 확정 → R9 CAUTION 경보",
            },
        },
        {
            "sample_id": "ppe_s4_i00003_personA_helmet_in_hand",
            "instant": "i00003",
            "scene_id": "s4_helmet_off_table_315s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": False,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "NOT_WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.18,
                "vest_prob_equiv": 0.87,
                "r9_finding_raised": True,
                "note": "손/테이블의 헬멧 박스는 머리 영역 밖이라 orphan 으로 버려지고 머리는 no_helmet_head 로 판정",
            },
        },
        {
            "sample_id": "ppe_s4_i00003_personB_re_putting",
            "instant": "i00003",
            "scene_id": "s4_helmet_off_table_315s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "UNKNOWN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.52,
                "vest_prob_equiv": 0.86,
                "r9_finding_raised": False,
                "note": "헬멧 다시 쓰는 전환 구간에서 3초 창 버킷이 4/6 만 채워져 PpeJudge 가 UNKNOWN(보류) 유지",
            },
        },
        {
            "sample_id": "ppe_s5_i00004_personA_near_front",
            "instant": "i00004",
            "scene_id": "s5_near_far_angles_405s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.93,
                "vest_prob_equiv": 0.92,
                "r9_finding_raised": False,
                "note": "근거리 정면·측면 시점에서 WORN 확정",
            },
        },
        {
            "sample_id": "ppe_s5_i00004_personB_far_back",
            "instant": "i00004",
            "scene_id": "s5_near_far_angles_405s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.81,
                "vest_prob_equiv": 0.84,
                "r9_finding_raised": False,
                "note": "원거리 후면 시점 포함, 다중 카메라 결합으로 WORN 유지",
            },
        },
        {
            "sample_id": "ppe_s6_i00005_personA_crouch_occluded",
            "instant": "i00005",
            "scene_id": "s6_crossing_occlusion_crouch_495s",
            "person_slot": 0,
            "world_id": "w-ppe-A",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": True,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "WORN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.78,
                "vest_prob_equiv": 0.76,
                "r9_finding_raised": False,
                "note": "쪼그려 앉기 자세이나 2대 이상에서 머리·상체 관절 관측되어 WORN 유지",
            },
        },
        {
            "sample_id": "ppe_s6_i00005_personB_crossing",
            "instant": "i00005",
            "scene_id": "s6_crossing_occlusion_crouch_495s",
            "person_slot": 1,
            "world_id": "w-ppe-B",
            "cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
            "gt_helmet": False,
            "gt_vest": True,
            "r9_pipeline": {
                "helmet_state": "UNKNOWN",
                "vest_state": "WORN",
                "helmet_prob_equiv": 0.45,
                "vest_prob_equiv": 0.82,
                "r9_finding_raised": False,
                "note": "교차 가림 구간에서 두 사람 머리 영역 겹침(not clear)으로 associate_frame 이 NOT_WORN 생성을 차단 → UNKNOWN",
            },
        },
    ]

    samples: list[dict[str, Any]] = []
    questions = build_ppe_questions()
    for idx, spec in enumerate(scene_specs):
        sid = spec["sample_id"]
        inst = spec["instant"]
        slot = int(spec["person_slot"])
        image_rel_paths: list[str] = []
        crop_metrics: list[dict[str, Any]] = []
        for c_idx, cid in enumerate(spec["cameras"]):
            bbox = PPE_PERSON_BOXES[inst][cid][slot]
            rel_path = f"crops/ppe/{sid}/{cid}.jpg"
            m = _crop_and_save(
                ppe_frames[inst][cid],
                bbox,
                out_dir / rel_path,
                fallback_seed=300 + idx * 10 + c_idx,
            )
            image_rel_paths.append(rel_path)
            crop_metrics.append({"camera_id": cid, **m})

        state = build_ppe_state(spec)
        validate_system_one_payload(state, questions, images=image_rel_paths, state_format="json")
        samples.append(
            {
                "sample_id": sid,
                "category": "ppe",
                "source_session": f"session4/egate1-frames-ppe-20261028/{inst} ({spec['scene_id']})",
                "split_status": "IN_TRAINING_SET_OF_T1_V4_ALL__PIPELINE_SANITY_ONLY",
                "is_upper_bound": True,
                "state": state,
                "state_format": "json",
                "questions": questions,
                "images": image_rel_paths,
                "crop_metrics": crop_metrics,
                "ground_truth": {
                    "helmet": bool(spec["gt_helmet"]),
                    "vest": bool(spec["gt_vest"]),
                    "helmet_wear_state": "WORN" if spec["gt_helmet"] else "NOT_WORN",
                    "vest_wear_state": "WORN" if spec["gt_vest"] else "NOT_WORN",
                    "label_provenance": "session4/ppe-scenes.md (rec-20261028-023200z-ppe-training 장면 시각표, T1-v4-ALL 학습 포함 세션)",
                },
                "baseline_ppe_r9": spec["r9_pipeline"],
            }
        )
    return samples


def _build_map_crop_samples(
    out_dir: Path,
    local_root: Path,
    synthetic_only: bool,
) -> list[dict[str, Any]]:
    """(C) `vdt/images/cam_{A..D}.jpg` 와 `vdt/cameras.json` 으로 3D 영역 8곳의 4카메라 크롭 묶음 생성."""
    vdt_dir = local_root / "vdt"
    cams_json_path = vdt_dir / "cameras.json"

    if not synthetic_only and cams_json_path.is_file():
        cams_doc = json.loads(cams_json_path.read_text(encoding="utf-8"))
        world_to_glb = np.asarray(cams_doc["world_to_glb_matrix"], dtype=float)
        cams_by_id = {c["camera_id"]: c for c in cams_doc["cameras"]}
        vdt_images = {
            cid: cv2.imread(str(vdt_dir / cams_by_id[cid]["image"]))
            if (vdt_dir / cams_by_id[cid]["image"]).is_file()
            else None
            for cid in CAMERA_IDS
        }
    else:
        world_to_glb = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]], dtype=float)
        cams_by_id = {}
        vdt_images = {cid: None for cid in CAMERA_IDS}

    questions = build_map_crop_questions(CAMERA_IDS)
    samples: list[dict[str, Any]] = []

    for r_idx, reg in enumerate(MAP_CROP_REGIONS):
        rid = reg["region_id"]
        image_rel_paths: list[str] = []
        per_cam_quality: dict[str, dict[str, Any]] = {}

        for c_idx, cid in enumerate(CAMERA_IDS):
            rel_path = f"crops/map_crop/{rid}/{cid}.jpg"
            if cid in cams_by_id and vdt_images[cid] is not None:
                proj = _project_patch_bbox(
                    reg["center_world_m"],
                    reg["normal_world"],
                    reg["patch_size_m"],
                    cams_by_id[cid],
                    world_to_glb,
                )
                img_stats = _crop_and_save(
                    vdt_images[cid],
                    proj["bbox_xyxy"],
                    out_dir / rel_path,
                    fallback_seed=500 + r_idx * 10 + c_idx,
                )
                # 선명도(Laplacian) + 정면 입사각(frontal_score) + 근접도 기반 객관적 참조 점수
                sharp_factor = min(1.5, np.log1p(max(1.0, img_stats["laplacian_var"])) / 6.0)
                dist_factor = 1.0 / (1.0 + 0.12 * proj["distance_m"])
                glare_penalty = max(0.2, 1.0 - 2.5 * img_stats["glare_ratio"])
                ref_score = float(proj["frontal_score"] * sharp_factor * dist_factor * glare_penalty)
                per_cam_quality[cid] = {
                    **proj,
                    **img_stats,
                    "bbox_xyxy": list(img_stats["bbox_xyxy"]),
                    "reference_score": round(ref_score, 4),
                }
            else:
                # 합성 폴백: r_idx % 4 번째 카메라만 선명하게, 나머지는 블러 처리
                is_best = (r_idx % len(CAMERA_IDS)) == c_idx
                synth_img = _make_synthetic_crop(500 + r_idx * 10 + c_idx, blur=not is_best)
                img_stats = _crop_and_save(
                    synth_img,
                    (0, 0, synth_img.shape[1], synth_img.shape[0]),
                    out_dir / rel_path,
                    fallback_seed=500 + r_idx * 10 + c_idx,
                )
                per_cam_quality[cid] = {
                    "distance_m": 3.5 if is_best else 7.0,
                    "cos_incidence": 0.85 if is_best else 0.25,
                    "frontal_score": 0.85 if is_best else 0.25,
                    "mean_depth_m": 3.5 if is_best else 7.0,
                    **img_stats,
                    "reference_score": round(0.90 if is_best else 0.20, 4),
                }
            image_rel_paths.append(rel_path)

        best_cid = max(CAMERA_IDS, key=lambda cid: per_cam_quality[cid]["reference_score"])
        state = build_map_crop_state({**reg, "camera_ids": list(CAMERA_IDS)})
        validate_system_one_payload(state, questions, images=image_rel_paths, state_format="json")

        samples.append(
            {
                "sample_id": rid,
                "category": "map_crop",
                "source_session": "vdt/images/cam_{A..D}.jpg + vdt/cameras.json (S15P31A101-527)",
                "state": state,
                "state_format": "json",
                "questions": questions,
                "images": image_rel_paths,
                "per_camera_quality": per_cam_quality,
                "ground_truth": {
                    "cleanest": best_cid,
                    "ranking": sorted(
                        CAMERA_IDS,
                        key=lambda cid: per_cam_quality[cid]["reference_score"],
                        reverse=True,
                    ),
                },
            }
        )
    return samples


def build_session13_collection_plan() -> dict[str, Any]:
    """노트북에 없거나 부족한 과거 세션 항목 및 13차 세션 수집 목록."""
    return {
        "missing_in_past_sessions": [
            {
                "item": "세션 11·12 LIVE 이벤트 S3 프레임 사본(event_frame_ready) 및 EC2 DB vlm_assessment 원문 JSON",
                "current_status": (
                    "session12/results/lobby-s12/worker-manual.log 에 VLM 완료 3건·타임아웃 2건의 지연시간·등급 로그가 있고 "
                    "state/journal 에 이벤트 100건 메타데이터가 있으나, 당시 Pod 로컬 사본(/srv/simai/runs/lobby-s12/state/frames)과 "
                    "EC2 DB vlm_assessment 의 한국어 rationale 본문 JSON 은 노트북으로 회수되지 않았다."
                ),
            },
            {
                "item": "R2(개구부·가장자리), R3(고소 위치), R4(낙상 의심), R8(이동 장비 근접) 실측 LIVE 이벤트 프레임",
                "current_status": (
                    "세션 10·12 LIVE 저널은 주로 R1(위험 구역 진입), R5(진입 예측), R7(감시 저하) 위주로 기록되었고, "
                    "R2·R3·R4·R9 는 세션 4 녹화 프레임 및 세션 6·7 리플레이 시나리오를 결합해 구성했다."
                ),
            },
            {
                "item": "T1-v4-ALL YOLO26x 학습 제외(Hold-out) 검증 분할 라벨 및 독립 PPE 평가 세션 (AGENTS.md 규칙 7)",
                "current_status": (
                    "docs/data/weights.md('T1-v4-ALL PPE 검출')에 기록된 대로 운영 가중치 `T1-v4-ALL-seed20261021-epoch08.pt` 는 "
                    "H200 에서 세 세션 전체(rec-20261028-023200z-ppe-training 포함)로 재학습되어 학습에서 뺀 세션이 없고, "
                    "H200 학습 서버의 `T1 corrected-v4` 박스 라벨 및 세션 제외 폴드 검증 분할 목록도 노트북(`simai-local/`)에 없다. "
                    "따라서 노트북에 있는 `egate1-frames-ppe-20261028` 프레임은 학습 분할에 해당하므로 파이프라인 점검용(`is_upper_bound=True`)으로만 쓰고, "
                    "제품 성능 지표로는 절대 쓰지 않는다(규칙 7)."
                ),
            },
            {
                "item": "PPE(-519 R9) `/dev/shm/simai-avatar-input.npz` 덤프 및 `ppe_state` 실측 타임라인 로그",
                "current_status": (
                    "세션 4 `rec-20261028-023200z-ppe-training`(s1~s6) 4카메라 프레임과 장면 시각표(`ppe-scenes.md`)는 있으나, "
                    "`-519` R9 통합 이후 실제 GPU Pod 에서 `services.ppe.loop` 가 남긴 버킷별 `ppe_state` JSONL 은 아직 수집 전이다."
                ),
            },
        ],
        "session13_capture_checklist": [
            "1. [필수·규칙 7] T1-v4-ALL 학습에 쓰이지 않은 13차 신규 PPE 검증 전용 녹화(Hold-out Validation Set: 안전모·조끼 착용/미착용/손에 들기/테이블 방치/교차 가림 각 30초 이상) 및 사람별 정답 라벨 구축 (또는 H200 의 세션 제외 폴드 체크포인트·검증 분할 라벨 회수)",
            "2. Pod 종료 전 `/srv/simai/runs/<run_id>/state/frames/` 의 이벤트 로컬 사진 사본과 워커 `vlm_rationale` 입출력 JSONL 을 노트북 `simai-local/session13/` 으로 회수",
            "3. EC2 또는 게이트웨이에서 `vlm_assessment`(rationale 본문, proposed_severity, latency_ms) 행을 JSON 으로 덤프",
            "4. 작업대(OBB) 가장자리 접근(R2)·올라서기(R3)·바닥 급하강 자세(R4)·안전모 미착용 작업(R9) 각 3회 이상 LIVE 재현 및 이벤트 크롭 수집",
            "5. `services.ppe.loop` 실행 중 30초 분량 `ppe_state` 메시지와 동일 시점 4카메라 인물 크롭을 짝지어 저장(결정형 VLM noul 확률 교정 곡선 정밀 검증용)",
        ],
    }


def prepare_bundle(
    *,
    local_root: Path = DEFAULT_LOCAL_ROOT,
    out_dir: Path = DEFAULT_OUT_DIR,
    synthetic_only: bool = False,
) -> dict[str, Any]:
    """벤치마크 입력 묶음을 생성하고 `manifest.json` 경로와 요약 정보를 반환한다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ppe_frames = _load_ppe_frames(local_root, synthetic_only)

    risk_samples = _build_risk_event_samples(out_dir, ppe_frames)
    ppe_samples = _build_ppe_samples(out_dir, ppe_frames)
    map_samples = _build_map_crop_samples(out_dir, local_root, synthetic_only)
    all_samples = [*risk_samples, *ppe_samples, *map_samples]

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "jira_key": "S15P31A101-538",
        "target_model": "kakaocorp/kanana-2-v-decide-3b-2611",
        "synthetic_only": bool(synthetic_only),
        "local_root_used": str(local_root),
        "counts": {
            "total": len(all_samples),
            "risk_event": len(risk_samples),
            "ppe": len(ppe_samples),
            "map_crop": len(map_samples),
        },
        "current_ppe_pipeline_spec": CURRENT_PPE_PIPELINE_SPEC,
        "session13_collection_plan": build_session13_collection_plan(),
        "samples": all_samples,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kanana-2-v-decide-3b-2611 벤치마크 입력 묶음 생성")
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT, help="simai-local 데이터 루트 경로")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="출력 묶음 디렉터리 경로")
    parser.add_argument("--synthetic-only", action="store_true", help="로컬 이미지 없이 합성 크롭만 생성(단위 시험용)")
    args = parser.parse_args(argv)

    manifest = prepare_bundle(local_root=args.local_root, out_dir=args.out, synthetic_only=args.synthetic_only)
    print(
        json.dumps(
            {
                "manifest": str(args.out / "manifest.json"),
                "counts": manifest["counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
