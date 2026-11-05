"""카메라 4대 시점에서 메시를 렌더해 사람 지운 사진과 MAE(0~1). 기준선 GLB 와 grtmesh OBJ 를 같은 방식(glb_enhance.render_view)으로 잰다.

usage: python evaluate.py baseline|grt <out_dir> [--obj mesh.obj --tex atlas.jpg]
지표: mae_valid = 유효 마스크 전체(메시 없는 화소는 검정으로 간주), mae_covered = 메시가 덮은 화소만, coverage = 유효 화소 중 덮인 비율.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

import common as C

ge = C.ge


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["baseline", "grt"])
    ap.add_argument("out", type=Path)
    ap.add_argument("--obj", type=Path)
    ap.add_argument("--tex", type=Path)
    ap.add_argument("--long-side", type=int, default=1152)
    ap.add_argument("--glb", type=Path, default=C.BASELINE_GLB)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cfg = ge.EnhanceConfig(check_render_long_side=a.long_side)
    if a.kind == "baseline":
        g, model = C.load_glb(a.glb)
        prims, extra = model.prims, {"faces": int(sum(len(p.idx) for p in model.prims))}
    else:
        g, model, nf, shp = C.load_obj_prim(a.obj, a.tex)
        prims, extra = model.prims, {"faces": int(nf), "atlas_hw": list(shp[:2])}
    res = {}
    for cam in C.load_cameras():
        img, cov, scale = ge.render_view(g, model, prims, cam, cfg)
        H, W = img.shape[:2]
        photo = cv2.resize(cam.image, (W, H), interpolation=cv2.INTER_AREA)
        valid = cv2.resize(cam.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
        d = (np.abs(img.astype(np.float32) - photo.astype(np.float32)) / 255.0).mean(axis=2)
        both = valid & cov
        res[cam.camera_id] = {"mae_valid": round(float(d[valid].mean()), 5),
                              "mae_covered": round(float(d[both].mean()), 5) if both.any() else None,
                              "coverage": round(float(both.sum() / valid.sum()), 4)}
        side = np.concatenate([photo, img], axis=1)
        cv2.imwrite(str(a.out / f"{cam.camera_id}_photo_vs_{a.kind}.jpg"), side[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 88])
    res["mean"] = {k: round(float(np.mean([v[k] for v in res.values() if v[k] is not None])), 5) for k in ("mae_valid", "mae_covered", "coverage")}
    res["mesh"] = extra
    (a.out / f"eval_{a.kind}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
