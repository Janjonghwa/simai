"""12차 현장 데이터(`cal-lobby-init-f0ab1c6b-jba2-2f6cc097`) 오프라인 축척 비교 및 진단 시각화 (S15P31A101-462).

실행:
    cd ai
    uv run python experiments/scale_props/evaluate_session12.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from simai_ai.calibration.world_frame.scale_props import (
    TRAFFIC_CONE_BASE_SIDE_M,
    TRAFFIC_CONE_HEIGHT_M,
    detect_and_triangulate_cone_from_images,
    detect_traffic_cone_endpoints,
    estimate_joint_scale_and_floor,
    undistort_image,
)
from simai_ai.geometry.rig import RigCamera

SESSION12_CALIB_PATH = Path(
    "C:/Users/SSAFY/simai-local/session12/results/lobby-s12/state/config/bundles/"
    "01447ea4d264ca541a3ea250fa57f22c029447719e49b4cf16946a5c67f32650/calibration.json"
)
SESSION12_BUNDLE_DIR = Path(
    "C:/Users/SSAFY/simai-local/session12/results/"
    "cal-lobby-init-f0ab1c6b-jba2-2f6cc097/bundle-393d5fbdabad41749559af4526a0a2ce"
)
DIAG_OUT_DIR = Path("C:/Users/SSAFY/simai-local/scale-props")

INPUT_HEIGHTS_M: dict[str, float] = {
    "cam_A": 2.44,
    "cam_B": 2.44,
    "cam_C": 2.48,
    "cam_D": 2.48,
}


def _project_point(cam: RigCamera, pt_xyz: np.ndarray) -> tuple[int, int]:
    xc = cam.to_camera(pt_xyz[None])[0]
    uv = cam.ideal_pixels((xc[:2] / xc[2])[None])[0]
    return int(round(float(uv[0]))), int(round(float(uv[1])))


def save_cone_detection_diagnostics(
    calib_doc: dict[str, Any],
    images_by_cam: dict[str, Path],
    out_dir: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cone, eps_by_cam = detect_and_triangulate_cone_from_images(calib_doc, images_by_cam)
    if cone is None:
        raise RuntimeError("Session 12 데이터에서 라바콘 삼각측량에 실패했다")

    panels: list[np.ndarray] = []
    for cam_entry in calib_doc["cameras"]:
        cid = str(cam_entry["camera_id"])
        raw_bgr = cv2.imread(str(images_by_cam[cid]), cv2.IMREAD_COLOR)
        und_bgr = undistort_image(raw_bgr, cam_entry["K"], cam_entry.get("distortion"))
        cam_und = RigCamera.from_calibration({**cam_entry, "distortion": {"model": "NONE", "coeffs": []}})
        eps = detect_traffic_cone_endpoints(und_bgr, camera_id=cid, keep_mask=True)
        ep = eps[0]
        x, y, w, h = ep.bbox_xywh
        pad = 90
        h_im, w_im = und_bgr.shape[:2]
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w_im, x + w + pad), min(h_im, y + h + pad)
        crop = und_bgr[y0:y1, x0:x1].copy()

        if ep.mask is not None:
            m_crop = ep.mask[y0:y1, x0:x1].astype(np.uint8)
            contours, _ = cv2.findContours(m_crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(crop, contours, -1, (0, 230, 0), 2)

        # 검출된 2D 꼭대기 (노란 원) + 삼각측량 재투영 3D 꼭대기 (하늘색 십자)
        au, av = int(round(ep.apex_px[0])) - x0, int(round(ep.apex_px[1])) - y0
        cv2.circle(crop, (au, av), 6, (0, 255, 255), -1)
        pu, pv = _project_point(cam_und, cone.apex_xyz)
        cv2.drawMarker(crop, (pu - x0, pv - y0), (255, 255, 0), cv2.MARKER_CROSS, 14, 2)

        # 검출된 2D 밑면 모서리 (자홍색 원)
        for cu, cv_px in ep.base_corners_px:
            cv2.circle(crop, (int(round(cu)) - x0, int(round(cv_px)) - y0), 5, (255, 0, 255), -1)

        # 삼각측량된 3D 밑면 모서리 재투영 (주황 사각형)
        for corner in cone.base_corners:
            if cid in corner.cameras:
                bu, bv = _project_point(cam_und, corner.point_xyz)
                cv2.rectangle(crop, (bu - x0 - 6, bv - y0 - 6), (bu - x0 + 6, bv - y0 + 6), (0, 165, 255), 2)

        panel = cv2.resize(crop, (420, 420), interpolation=cv2.INTER_AREA)
        base_tag = f"base={len(ep.base_corners_px)}pts" if ep.base_visible else "base=occluded(bench)"
        cv2.rectangle(panel, (0, 0), (420, 58), (25, 25, 25), -1)
        cv2.putText(
            panel,
            f"{cid} | bands={ep.band_count} | {base_tag}",
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (255, 255, 255),
            2,
        )
        cv2.putText(
            panel,
            f"apex=({ep.apex_px[0]:.1f}, {ep.apex_px[1]:.1f})",
            (10, 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (180, 240, 255),
            1,
        )
        panels.append(panel)

    top_row = np.hstack(panels[:2])
    bot_row = np.hstack(panels[2:])
    grid = np.vstack([top_row, bot_row])
    out_crop_path = out_dir / "session12_cone_detections_4cam.png"
    cv2.imwrite(str(out_crop_path), grid)

    # 임시 파일 정리
    tmp_debug = out_dir / "_debug_cone_crops.png"
    if tmp_debug.exists():
        tmp_debug.unlink()

    restored_heights = {str(c["camera_id"]): float(c["camera_height_m"]) for c in calib_doc["cameras"]}
    est = estimate_joint_scale_and_floor(
        restored_heights,
        INPUT_HEIGHTS_M,
        cone=cone,
        disagreement_tolerance=0.05,
        height_tolerance_cm=5.0,
    )

    # 카메라 3D 중심 좌표 및 X축 바닥 기울기 분석
    cam_centers: dict[str, list[float]] = {}
    for c in calib_doc["cameras"]:
        cid = str(c["camera_id"])
        R = np.asarray(c["R"], dtype=float)
        t = np.asarray(c["t"], dtype=float)
        center = -R.T @ t
        cam_centers[cid] = [round(float(v), 4) for v in center]

    # X 좌표에 대한 높이 오차(restored - input) 선형 회귀로 바닥 피치 기울기 계산
    xs = np.array([cam_centers[c][0] for c in sorted(cam_centers)], dtype=float)
    dzs = np.array([restored_heights[c] - INPUT_HEIGHTS_M[c] for c in sorted(cam_centers)], dtype=float)
    slope_x, intercept_x = np.polyfit(xs, dzs, 1)
    tilt_deg = math.degrees(math.atan(float(slope_x)))

    summary = {
        "calibration_version": calib_doc["calibration_version"],
        "cone_3d": {
            "apex_xyz_m": [round(float(v), 4) for v in cone.apex_xyz],
            "apex_cameras": list(cone.apex_cameras),
            "apex_max_reprojection_px": cone.apex_max_reprojection_px,
            "apex_ray_angle_deg": cone.apex_ray_angle_deg,
            "base_corners": [
                {
                    "index": c.index,
                    "point_xyz_m": [round(float(v), 4) for v in c.point_xyz],
                    "cameras": list(c.cameras),
                    "max_reprojection_px": c.max_reprojection_px,
                }
                for c in cone.base_corners
            ],
            "mean_floor_z_m": round(float(np.mean(cone.floor_z_values_m)), 4),
            "measured_height_m": round(float(cone.measured_height_m), 4),
            "expected_height_m": TRAFFIC_CONE_HEIGHT_M,
            "height_residual_cm": round((float(cone.measured_height_m) - TRAFFIC_CONE_HEIGHT_M) * 100.0, 2),
            "measured_base_sides_m": [round(float(s), 4) for s in cone.measured_base_sides_m],
            "expected_base_side_m": TRAFFIC_CONE_BASE_SIDE_M,
        },
        "scale_estimation": est,
        "camera_centers_m": cam_centers,
        "floor_tilt_analysis": {
            "slope_dz_per_dx": round(float(slope_x), 5),
            "intercept_m": round(float(intercept_x), 4),
            "pitch_tilt_deg": round(tilt_deg, 3),
            "wall_x0_mean_diff_cm": round(float(np.mean([dzs[2], dzs[3]]) * 100.0), 2),
            "wall_x74_mean_diff_cm": round(float(np.mean([dzs[0], dzs[1]]) * 100.0), 2),
        },
        "diagnostics_image": str(out_crop_path),
    }
    (out_dir / "session12_scale_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary


if __name__ == "__main__":
    cal = json.loads(SESSION12_CALIB_PATH.read_text(encoding="utf-8"))
    imgs = {f"cam_{c}": SESSION12_BUNDLE_DIR / f"cam_{c}.jpg" for c in ("A", "B", "C", "D")}
    out = save_cone_detection_diagnostics(cal, imgs, DIAG_OUT_DIR)
    print(json.dumps(out, indent=2, ensure_ascii=False))
