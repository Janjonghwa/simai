"""S15P31A101-535 후보 (a): 보정 외부 파라미터(R, t) + 내부 파라미터(K_undistorted)를 MapAnything 조건 입력으로 변환·검증하는 스크립트.

- 노트북 CPU(`--verify-inputs-only`)에서는 `prepared/prepared.json` 과 `calibration.json` 을 읽어
  `run_mapanything.py --mode known --use-poses 1` 이 받는 `cameras.json` 호환 규격(`cam2world` 4x4 OpenCV,
  `intrinsics_model_res`, `model_input_hw`) 및 직접 주입용 뷰 명세를 생성·검증하고
  `C:/Users/SSAFY/simai-local/align-535/candidate_a_known_poses_input.json` 에 저장한다.
- GPU Pod 실행 명령은 출력만 하고 실행하지 않는다(지시서 규칙: Pod 접속·전송 금지).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.world_frame.inputs import _crop_resize


def build_known_cameras_doc_from_calibration(
    prepared_dir: Path,
    calibration_path: Path,
    *,
    target_long_side: int = 518,
) -> dict[str, Any]:
    """calibration.json 의 (R, t) 와 prepared.json 의 K_undistorted 를 run_mapanything.py --mode known 입력 형식으로 변환한다."""
    prep_doc = json.loads((prepared_dir / "prepared.json").read_text(encoding="utf-8"))
    cal_doc = json.loads(calibration_path.read_text(encoding="utf-8"))
    cams = {c["camera_id"]: c for c in cal_doc["cameras"]}

    views_out = []
    for pv in prep_doc["views"]:
        cid = pv["camera_id"]
        c = cams[cid]
        w, h = int(pv["image_size"][0]), int(pv["image_size"][1])
        # MapAnything fixed_mapping 기본 해상도(긴 변 518, 14의 배수)
        th = int(round(h * (target_long_side / max(w, h)) / 14.0) * 14)
        tw = int(target_long_side)
        rw, rh, left, top = _crop_resize(w, h, th, tw)

        K_und = np.asarray(pv["K_undistorted"], dtype=float)
        sx, sy = rw / w, rh / h
        K_model = np.array([
            [K_und[0, 0] * sx, 0.0, (K_und[0, 2] + 0.5) * sx - 0.5 - left],
            [0.0, K_und[1, 1] * sy, (K_und[1, 2] + 0.5) * sy - 0.5 - top],
            [0.0, 0.0, 1.0],
        ], dtype=float)

        R_w2c = np.asarray(c["R"], dtype=float)
        t_w2c = np.asarray(c["t"], dtype=float)
        if not np.allclose(R_w2c.T @ R_w2c, np.eye(3), atol=1e-4) or not np.isclose(np.linalg.det(R_w2c), 1.0, atol=1e-4):
            raise ValueError(f"{cid}: 회전 행렬 직교성 오류")

        T_c2w = np.eye(4, dtype=float)
        T_c2w[:3, :3] = R_w2c.T
        T_c2w[:3, 3] = -R_w2c.T @ t_w2c

        views_out.append({
            "name": pv["name"],
            "camera_id": cid,
            "image_size": [w, h],
            "model_input_hw": [th, tw],
            "K_undistorted": K_und.tolist(),
            "intrinsics_model_res": K_model.tolist(),
            "cam2world": T_c2w.tolist(),
            "is_metric_scale": True,
        })

    pod_cmd = (
        "python -m simai_ai.scene.run_mapanything "
        "--run-id s12_candidate_a_known_poses "
        "--mode known "
        "--images <root>/prepared "
        "--known-from <root>/candidate_a_known_poses_input.json "
        "--use-poses 1 --pose-scale 1.0 --repeat 1"
    )
    return {
        "format": "simai.mapanything_known_poses_spec",
        "jira_key": "S15P31A101-535",
        "calibration_id": cal_doc.get("calibration_id") or cal_doc.get("version_id"),
        "convention": "cam2world 4x4, OpenCV (+X right, +Y down, +Z forward), Calibrated World (m)",
        "pod_queue_command": pod_cmd,
        "views": views_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("C:/Users/SSAFY/simai-local/align-535/ma_single"))
    ap.add_argument("--out", type=Path, default=Path("C:/Users/SSAFY/simai-local/align-535/candidate_a_known_poses_input.json"))
    ap.add_argument("--verify-inputs-only", action="store_true", default=True)
    args = ap.parse_args()

    doc = build_known_cameras_doc_from_calibration(args.root / "prepared", args.root / "calibration.json")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[Candidate (a)] Verified {len(doc['views'])} calibrated camera_poses + intrinsics specs -> {args.out}")
    print(f"[Pod Queue Command (NOT EXECUTED)]: {doc['pod_queue_command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
