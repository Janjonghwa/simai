"""현재 맵 GLB + 12차 보정·사진 4장으로 운영 후처리(glb_enhance 의 늘어난 면 제거·바닥·벽 채움)를 돌려 효과를 잰다(S15P31A101-511).

- 기본 설정 전/후: 시간, 늘어난 면, 채운 면적과 출처 비율, 4대 시점 MAE(학습 시점), 자유 시점 4장(전/후)
- 늘어난 면 기준 재확인: cos 스윕(삭제 면 수·넓이), 한 대 빼고 판정한 삭제 면 중 빠진 카메라가 정면(cos ≥ 0.4)으로 본 비율(잘못 지운 비율)
- 채움 색 규칙(strict / consistent / lenient)별 관측색 비율
usage: python fill_real.py [--skip-modes]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import time

import cv2
import numpy as np

import common as C
from simai_ai.scene import glb_fill as gf

ge = C.ge
OUT = C.WORK / "fill"


def strip_enhanced(raw: bytes) -> bytes:
    """운영 GLB 는 이미 보강본이다: 옛 바닥 사각형 노드와 보강 표식을 떼고 읽기 전용으로 다시 보강해 본다(원본 파일은 안 바꾼다)."""
    g = ge.parse_glb(raw)
    simai = (g.doc.get("extras") or {}).get("simai")
    if isinstance(simai, dict):
        simai.pop("enhanced", None)
    doc = g.doc
    drop = {i for i, n in enumerate(doc["nodes"]) if n.get("name") == "estimated_floor_plane"}
    for sc in doc["scenes"]:
        sc["nodes"] = [n for n in sc.get("nodes", []) if n not in drop]
    return ge.serialize_glb(g)


def render(glb: bytes, cam, long_side=1280):
    g = ge.parse_glb(glb)
    simai = (g.doc.get("extras") or {}).get("simai")
    if isinstance(simai, dict):
        simai.pop("enhanced", None)
    model = ge.load_scene(g)
    return ge.render_view(g, model, model.prims, cam, ge.EnhanceConfig(check_render_long_side=long_side))[0]


def label(img, text):
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(out, text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-modes", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    raw = strip_enhanced(C.BASELINE_GLB.read_bytes())
    cams = C.load_cameras()
    rep = {}
    cfg = ge.EnhanceConfig()

    t = time.time()
    res = ge.enhance_glb(raw, config=cfg, cameras=cams)
    rep["default_seconds"] = round(time.time() - t, 1)
    rep["default"] = res.report
    print("default", rep["default_seconds"], "s applied", res.report["applied"], res.report.get("reason"), flush=True)
    if res.glb is not None:
        (OUT / "enhanced_default.glb").write_bytes(res.glb)
        for vn, (e, tg, fov) in C.VIEWS.items():
            cam = C.lookat_camera(e, tg, hfov_deg=fov)
            before, after = render(raw, cam), render(res.glb, cam)
            cv2.imwrite(str(OUT / f"free_{vn}_before.jpg"), before[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            cv2.imwrite(str(OUT / f"free_{vn}_after.jpg"), after[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            cv2.imwrite(str(OUT / f"compare_{vn}.jpg"), np.concatenate([label(before, f"{vn}: before"), label(after, f"{vn}: after (stretched removed + floor/wall fill)")], 1)[:, :, ::-1],
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
        print("free views saved", flush=True)

    # --- 늘어난 면 기준 재확인
    g = ge.parse_glb(raw)
    model = ge.load_scene(g)
    mesh = gf.merge_mesh(model.prims)
    t = time.time()
    vis = [gf.render_visibility(mesh, c, cfg.fill_vis_long_side) for c in cams]
    rep["visibility_seconds_4cam"] = round(time.time() - t, 1)
    sweep = {}
    for cs in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        drop, rec = gf.stretched_faces(mesh, vis, dataclasses.replace(cfg, fill_cos_slant=cs))
        # 한 대 빼고 판정 → 빠진 카메라가 정면(cos>=0.4, 보임)으로 본 면 비율
        wrong, tot = 0, 0
        for k in range(len(cams)):
            d3, _ = gf.stretched_faces(mesh, [v for i, v in enumerate(vis) if i != k], dataclasses.replace(cfg, fill_cos_slant=cs))
            px_min = max(1, round(6 * vis[k].scale ** 2))
            seen_k = vis[k].seen_px >= px_min
            tot += int(d3.sum())
            wrong += int((d3 & seen_k & (vis[k].cos >= 0.4)).sum())
        sweep[str(cs)] = {"removed_faces": rec["removed_faces"], "removed_area_m2": rec["removed_area_m2"], "removed_area_ratio": rec["removed_area_ratio"],
                          "loo_removed": tot, "loo_wrongly_removed_seen_frontal_by_held_out": wrong,
                          "loo_wrong_rate": round(wrong / tot, 4) if tot else None}
    rep["stretched_sweep"] = sweep
    print(json.dumps(sweep, ensure_ascii=False), flush=True)

    # --- 늘어난 면 제거만 켜고 4대 시점 MAE(학습 시점) 전/후
    only = dataclasses.replace(cfg, fill_floor=False, fill_walls=False, check_tolerance_mean=1.0, check_tolerance_camera=1.0)
    r2 = ge.enhance_glb(raw, config=only, cameras=cams)
    rep["stretched_only_check"] = (r2.report.get("check") or {})
    rep["stretched_only"] = (r2.report.get("fill") or {}).get("stretched")

    # --- 채움 색 규칙별
    if not a.skip_modes:
        rep["modes"] = {}
        for mode in ("strict", "consistent", "lenient"):
            c2 = dataclasses.replace(cfg, fill_color_mode=mode, check_tolerance_mean=1.0, check_tolerance_camera=1.0)
            t = time.time()
            r3 = ge.enhance_glb(raw, config=c2, cameras=cams)
            fi = r3.report.get("fill") or {}
            rep["modes"][mode] = {"seconds": round(time.time() - t, 1), "fill_ratio": (fi.get("surface_source") or {}).get("fill_ratio"),
                                  "fill_area_m2": (fi.get("surface_source") or {}).get("fill_area_m2"),
                                  "floor": {k: (fi.get("floor") or {}).get(k) for k in ("fill_m2", "projected_ratio", "gray_ratio")},
                                  "check": {k: (r3.report.get("check") or {}).get(k) for k in ("mae_mean_before", "mae_mean_after")}}
            if mode != "consistent" and r3.glb is not None:
                for vn in ("top", "obl2"):
                    e, tg, fov = C.VIEWS[vn]
                    cv2.imwrite(str(OUT / f"free_{vn}_{mode}.jpg"), render(r3.glb, C.lookat_camera(e, tg, hfov_deg=fov))[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(mode, rep["modes"][mode], flush=True)
    (OUT / "fill_real.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=float), encoding="utf-8")


if __name__ == "__main__":
    main()
