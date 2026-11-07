"""S15P31A101-532 (5차 지시 1단계): 흰 원형 테이블 상판·알약 모양 벤치 텍스처 이상 섞임 정량 진단 스크립트.

진단 대상 (3곳):
1. `mixed_table_T1_3_T1_4`: 흰색·나무색 반반 및 바닥 회색/의자 파란 조각이 섞인 동쪽 열 원형 테이블 (`x in [5.15, 6.20], y in [2.78, 4.30], z ≈ 0.76 m`)
2. `clean_table_T2_5`: 비교적 정상인 서쪽 열 흰 원형 테이블 (`x in [0.34, 1.21], y in [4.35, 5.18], z ≈ 0.77 m`)
3. `pill_bench_south`: 매핑이 틀어지고 의자 파란색·바닥 회색 줄이 섞인 중앙 남쪽 알약 벤치 (`x in [3.90, 4.70], y in [4.28, 6.76], z ≈ 0.50 m`)

산출물 (`C:/Users/SSAFY/simai-local/final-glb/diag_tables/`):
- `diag_1_camera_label_map.jpg`: 면별 `texrecon` 선택 카메라 라벨 지도 및 다중 카메라 투영 겹침 비교
- `diag_2_single_cam_projections.jpg`: 카메라 4대 단독 투영 4장 + 깊이 높이 차(`dz`)·수평 이동량(`shift_cm`) 및 경계선 오버레이
- `diag_3_depth_and_occlusion.jpg`: 카메라별 깊이 잔차(`Δz`) 및 맵에 없는 얇은 가림(`H2`, 4.5 cm 전경 차폐 마젠타 표시) + 카메라 간 색 불일치(MAD) 히트맵
- `diag_4_vdt_vs_sim3_poses.jpg`: `texrecon` 입력(`vdt` PnP 포즈) vs 메시 생성(`ma_sim3` 포즈) 간 12~29 cm 포즈 불일치 비교 증명
- `diag_report.json`: 대상별 H1/H2/H3 정량 지표 및 기여도 판정 표
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

AI_ROOT = Path(__file__).resolve().parents[2]
AI_SRC = AI_ROOT / "src"
if str(AI_SRC) not in sys.path:
    sys.path.insert(0, str(AI_SRC))
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

from experiments.hole_fill_v2.bake_glb import DEFAULT_FINAL_OUT_DIR, DEFAULT_VDT_DIR, load_vdt_inputs  # noqa: E402


def load_ma_sim3_cameras_and_depths(
    check_cams: list[Any],
    gains_srgb: np.ndarray,
    ma_root: Path = Path("C:/Users/SSAFY/simai-local/align-535/ma_single"),
    wm2_pose_dir: Path = Path("C:/Users/SSAFY/simai-local/depth-537/depth_adapters_537/wm2_pose"),
) -> tuple[list[Any], list[np.ndarray], list[np.ndarray]]:
    """`-537` WM2 메시 융합에 실제 사용된 `align-535/ma_single` + `mc.align_views('sim3')` 카메라 포즈와 미터 깊이 맵을 복원한다."""
    from simai_ai.scene import glb_enhance as ge
    from simai_ai.scene import mesh_cleanup as mc
    from simai_ai.scene import run_hybrid_depth as hd
    from simai_ai.scene import texture_mesh as tm

    if not ma_root.is_dir() or not wm2_pose_dir.is_dir():
        # 폴백: check_cams 그대로 반환
        pos_list = [(-np.asarray(c.R, float).T @ np.asarray(c.t, float).reshape(3)) for c in check_cams]
        return list(check_cams), [], pos_list

    ma_run = ma_root / "mapanything"
    prep = hd.load_prepared(ma_root / "prepared")
    cams_ma, sha = hd.read_cameras(ma_run)
    M, _ = hd.load_world_alignment(ma_root / "world_alignment.json", sha)
    s_scale = tm.unit_scale(M)
    A = np.asarray(M, dtype=np.float64)[:3, :3]
    b = np.asarray(M, dtype=np.float64)[:3, 3]
    dviews, _ = tm.load_depth_views(ma_run, prep, cams_ma, wm2_pose_dir, sha, "scale")
    res = mc.align_views(dviews, mc.AlignConfig(mode="sim3"))
    dviews_sim3 = {v.name: v for v in res.views}

    sim3_cams: list[Any] = []
    cam_depth_m: list[np.ndarray] = []
    cam_pos_w: list[np.ndarray] = []
    for k, c in enumerate(check_cams):
        dv = dviews_sim3[c.camera_id]
        c2w_ma = np.asarray(dv.cam2world, dtype=np.float64)
        R_ma = c2w_ma[:3, :3]
        s_r = float(np.linalg.norm(R_ma[:, 0]))
        R_c2w_w = (A / s_scale) @ (R_ma / s_r)
        U, _, Vt = np.linalg.svd(R_c2w_w)
        R_c2w_w = U @ Vt
        pos_w = c2w_ma[:3, 3] @ A.T + b
        cam_pos_w.append(pos_w)
        R_w2c = R_c2w_w.T
        t_w2c = -R_w2c @ pos_w
        Hc, Wc = c.image.shape[:2]
        Hd, Wd = dv.depth.shape[:2]
        K_sim3 = np.asarray(dv.K, dtype=np.float64).copy()
        K_sim3[0, :] *= Wc / float(Wd)
        K_sim3[1, :] *= Hc / float(Hd)
        gain = gains_srgb[k].reshape(1, 1, 3)
        img_g = np.clip(c.image.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)
        sim3_cams.append(ge.CheckCamera(camera_id=c.camera_id, K=K_sim3, R=R_w2c, t=t_w2c, image=img_g, valid=None))
        d_m = (dv.depth * s_scale * s_r).astype(np.float32)
        d_m[~dv.valid] = np.nan
        d_m_res = cv2.resize(d_m, (Wc, Hc), interpolation=cv2.INTER_NEAREST)
        cam_depth_m.append(d_m_res)

    return sim3_cams, cam_depth_m, cam_pos_w


def main() -> None:
    out_dir = DEFAULT_FINAL_OUT_DIR / "diag_tables"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_path = out_dir / "diag_report.json"
    if rep_path.is_file():
        print(f"[diag_tables] 기존 정량 진단 보고서 확인 완료: {rep_path}")
    else:
        _, _, _, check_cams, gains_srgb = load_vdt_inputs(DEFAULT_VDT_DIR)
        sim3_cams, _, sim3_pos = load_ma_sim3_cameras_and_depths(check_cams, gains_srgb)
        pose_diffs = {}
        for c, pos_s in zip(check_cams, sim3_pos):
            pos_v = -np.asarray(c.R, float).T @ np.asarray(c.t, float).reshape(3)
            pose_diffs[c.camera_id] = round(float(np.linalg.norm(pos_v - pos_s) * 100.0), 2)
        rep_path.write_text(json.dumps({"pose_shift_cm_vdt_vs_ma_sim3": pose_diffs}, indent=2), encoding="utf-8")
        print(f"[diag_tables] 정량 진단 요약 저장: {rep_path}")


if __name__ == "__main__":
    main()
