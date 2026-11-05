"""12차 세션 로비 보정·사진 + 현재 GLB 에서 렌더한 깊이 -> grtmesh `multiview_rgbd` 입력(저장소 밖).

사진·깊이는 왜곡을 같은 K 로 편 뒤 1/2 크기(1152x648)로 줄인다(깊이는 최근접). 출력 cameras.json 의 distortion 은 0.
usage: python prepare_input.py <out_dir> [--scale 0.5]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

import common as C


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--glb", type=Path, default=C.BASELINE_GLB)
    a = ap.parse_args()
    g, model = C.load_glb(a.glb)
    cams = C.load_cameras()
    fr = a.out / "frames" / "000000"
    fr.mkdir(parents=True, exist_ok=True)
    allp = np.concatenate([p.pos for p in model.prims])
    lo, hi = np.percentile(allp, 0.5, axis=0) - 0.2, np.percentile(allp, 99.5, axis=0) + 0.2
    out_cams = []
    for c in cams:
        h0, w0 = c.image.shape[:2]
        W, H = round(w0 * a.scale), round(h0 * a.scale)
        K = c.K.copy()
        K[:2] *= a.scale  # 화소 중심 규약 차이는 무시 가능한 0.5*(1-s) px
        t0 = time.time()
        d = C.render_depth(model.prims, c, (H, W), K)
        valid = cv2.resize(c.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        d[~valid] = 0
        img = cv2.resize(c.image, (W, H), interpolation=cv2.INTER_AREA)
        name = c.camera_id.replace("cam_", "cam")
        cv2.imwrite(str(fr / f"{name}_color.png"), img[:, :, ::-1])
        cv2.imwrite(str(fr / f"{name}_depth.png"), np.clip(np.rint(d * 1000), 0, 65535).astype(np.uint16))
        c2w = np.eye(4)
        c2w[:3, :3] = c.R.T
        c2w[:3, 3] = -c.R.T @ c.t
        out_cams.append({"name": name, "width": W, "height": H, "fx": K[0, 0], "fy": K[1, 1], "cx": K[0, 2], "cy": K[1, 2],
                         "camera_to_world": c2w.tolist()})
        print(name, "depth render s", round(time.time() - t0, 1), "covered", round(float((d > 0).mean()), 3))
    (a.out / "cameras.json").write_text(json.dumps({"depth_scale": 1000.0, "bounds": {"min": lo.tolist(), "max": hi.tolist()},
                                                      "cameras": out_cams}, indent=1))
    print("bounds", lo, hi)


if __name__ == "__main__":
    main()
