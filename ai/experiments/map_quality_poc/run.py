"""map_quality_poc 전체 실행: 가시성·아틀라스(캐시) -> OpenReco 굽기 -> 늘어난 면·바닥·벽 -> 한 대 빼고 평가 -> 자유 시점 비교 -> GLB.

usage: python run.py [--stage all|setup|loo|free|glb]   (노트북 CPU, GPU 없음)
산출물은 C:/Users/SSAFY/simai-local/map-quality-poc/ 아래(저장소 밖).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pickle
import time
from pathlib import Path

import cv2
import numpy as np

import common as C
import fills as FL
import meshvis as M
import openreco as O
import views as V

ge = C.ge
OUT = C.WORK
COMPARE = OUT / "compare"
CACHE = OUT / "cache.pkl"
CAMS_ALL = [0, 1, 2, 3]
CFG = {"cos_slant": 0.15, "long_edge": 0.25, "view_cos_min": 0.15, "atlas_res": 4096}


class Ctx:
    pass


def setup() -> Ctx:
    x = Ctx()
    x.t = {}
    x.mesh = M.load_mesh()
    x.cams = C.load_cameras()
    t0 = time.time()
    if CACHE.exists():
        d = pickle.loads(CACHE.read_bytes())
        x.vis, x.atlas = d["vis"], d["atlas"]
        x.t["vis_sec"] = d.get("vis_sec")
    else:
        x.vis = M.compute_vis(x.mesh, x.cams)
        x.t["vis_sec"] = time.time() - t0
        x.atlas = O.make_atlas(x.mesh, CFG["atlas_res"])
        CACHE.write_bytes(pickle.dumps({"vis": x.vis, "atlas": x.atlas, "vis_sec": x.t["vis_sec"]}))
    x.score = M.view_score(x.vis, cos_min=CFG["view_cos_min"])
    x.z0, x.zinfo = FL.floor_level(x.mesh)
    x.planes, x.rej, x.pinfo = FL.find_wall_planes(x.mesh, x.z0)
    return x


# --------------------------------------------------------------------------- 장면 만들기
def faces_keep(x, cam_idx):
    long_, slant = M.stretched_faces(x.mesh, x.vis, cam_idx, CFG["cos_slant"], CFG["long_edge"])
    return ~(long_ | slant), long_, slant


def baseline_prims(x, keep=None):
    prims = []
    for p in x.mesh.model.prims:
        if p.name == "estimated_floor_plane":
            if keep is None:
                prims.append(p)  # 지금 맵 그대로
            continue
        s = next(s for s in x.mesh.slices if s[0] == p.name)
        prims.append(p if keep is None else dataclasses.replace(p, idx=p.idx[keep[s[1]:s[2]]]))
    return prims


def build(x, base, cam_idx, s2, s3, s4, floor_mode="strict", gains=True, flag=False):
    """base: 'tex'(지금 맵 texrecon) | 'or'(OpenReco, cam_idx 사진으로 굽기). 반환 prims, images, info.
    flag=True 이면 관측 여부(255/0) 영상을 같은 번호로 낸다(관측 화소 비율·MAE 용)."""
    info = {}
    keep = faces_keep(x, cam_idx)[0] if s2 else None
    images, prims = {}, []
    if base == "tex":
        prims = baseline_prims(x, keep)
        if s2 or s3 or s4:
            prims = [p for p in prims if p.name != "estimated_floor_plane"]  # 채움으로 대체
    else:  # 'or' 또는 'orc'
        sc = x.score
        g, ginfo = O.exposure_gains(x.mesh, x.vis, x.cams, sc, use=cam_idx) if gains else (np.ones((4, 3)), {})
        fk = None
        if base == "orc":  # 보수형: 사진 간 색이 어긋나는 면·1대에만 보인 회색 메시 면은 회색(모름)
            fk, cinfo = O.photo_consistency(x.mesh, x.vis, x.cams, cam_idx, g)
            info["consistency"] = cinfo
        rgb, obs, st = O.bake(x.mesh, x.vis, x.cams, cam_idx, x.atlas, sc, g, face_keep=fk)
        info.update({"bake": st, "gains": ginfo})
        prims = [O.atlas_prim(x.mesh, x.atlas, 1000, keep)]
        images[1000] = rgb
        if flag:
            images[("flag", 1000)] = np.repeat(((obs > 0) * 255).astype(np.uint8)[:, :, None], 3, 2)
        info["rgb"], info["obs"] = rgb, obs
    nid = 2000
    if s3:
        p, tex, obs, st = FL.build_floor(x.mesh, x.vis, x.cams, cam_idx, x.z0, floor_mode, keep=keep)
        info["floor"] = st
        prims.append(dataclasses.replace(p, image=nid))
        images[nid] = tex
        if flag:
            images[("flag", nid)] = np.repeat(((obs > 0) * 255).astype(np.uint8)[:, :, None], 3, 2)
        nid += 1
    if s4:
        info["walls"] = []
        for k, pl in enumerate(x.planes):
            p, tex, obs, st = FL.build_wall(x.mesh, x.vis, x.cams, cam_idx, x.z0, pl, f"wall_fill_{k}", keep=keep)
            info["walls"].append(st)
            prims.append(dataclasses.replace(p, image=nid))
            images[nid] = tex
            if flag:
                images[("flag", nid)] = np.repeat(((obs > 0) * 255).astype(np.uint8)[:, :, None], 3, 2)
            nid += 1
    return prims, images, info


def render(x, prims, images, cam, long_side=1152, flag=False):
    """flag=True 이면 images 의 ('flag', id) 영상으로 칠해 관측 화소(흰색)를 낸다. 지금 맵(tex) 은 flag 미지원."""
    cfg = ge.EnhanceConfig(check_render_long_side=long_side)
    if flag:
        imgs = {k[1]: v for k, v in images.items() if isinstance(k, tuple)}
        model = ge.SceneModel(prims, imgs)
    else:
        model = ge.SceneModel(prims, {k: v for k, v in images.items() if not isinstance(k, tuple)})
    return ge.render_view(x.mesh.g, model, prims, cam, cfg)


def mae_row(img, cov, photo_cam, flagimg=None):
    H, W = img.shape[:2]
    photo = cv2.resize(photo_cam.image, (W, H), interpolation=cv2.INTER_AREA)
    valid = cv2.resize(photo_cam.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
    d = (np.abs(img.astype(np.float32) - photo.astype(np.float32)) / 255.0).mean(axis=2)
    both = valid & cov
    r = {"mae_valid": float(d[valid].mean()), "mae_covered": float(d[both].mean()) if both.any() else None,
         "coverage": float(both.sum() / valid.sum())}
    if flagimg is not None:
        ob = both & (flagimg[:, :, 0] > 127)
        r["mae_observed"] = float(d[ob].mean()) if ob.any() else None
        r["observed_coverage"] = float(ob.sum() / valid.sum())
    return r, photo, valid


def mean_rows(rows):
    return {k: round(float(np.mean([r[k] for r in rows if r.get(k) is not None])), 5) for k in rows[0]}


# --------------------------------------------------------------------------- 평가
def loo(x):
    variants = [("OR", "or", False, False, False), ("OR+2", "or", True, False, False), ("OR+2+3", "or", True, True, False), ("OR+2+3+4", "or", True, True, True),
                ("ORc", "orc", False, False, False), ("ORc+2+3+4", "orc", True, True, True)]
    res = {v[0]: [] for v in variants}
    res["OR_insample_4cam"] = []
    res["ORc_insample_4cam"] = []
    res["TEX_insample_4cam(지금 맵)"] = []
    res["TEX+2_insample_4cam"] = []
    for hold in range(4):
        cam_idx = [i for i in range(4) if i != hold]
        print(f"[loo] 빼는 카메라 {x.cams[hold].camera_id}", flush=True)
        for name, base, s2, s3, s4 in variants:
            t0 = time.time()
            prims, images, info = build(x, base, cam_idx, s2, s3, s4, flag=True)
            img, cov, _ = render(x, prims, images, x.cams[hold])
            fimg, _, _ = render(x, prims, images, x.cams[hold], flag=True)
            r, photo, valid = mae_row(img, cov, x.cams[hold], fimg)
            r["cam"] = x.cams[hold].camera_id
            res[name].append(r)
            print(f"  {name}: {json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})} ({time.time() - t0:.0f}s)", flush=True)
            if name == "OR+2+3+4":
                cv2.imwrite(str(COMPARE / f"loo_{x.cams[hold].camera_id}_photo_vs_OR234.jpg"),
                            np.concatenate([photo, img], 1)[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 85])
            if name == "OR":
                cv2.imwrite(str(COMPARE / f"loo_{x.cams[hold].camera_id}_photo_vs_OR.jpg"),
                            np.concatenate([photo, img], 1)[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 85])
    # 학습 시점(4대 전부로 굽고 4대 시점에서 잰다): 지난 grtmesh PoC 의 기준선과 같은 방식
    prims, images, info = build(x, "or", CAMS_ALL, False, False, False, flag=True)
    for k in CAMS_ALL:
        img, cov, _ = render(x, prims, images, x.cams[k])
        fimg, _, _ = render(x, prims, images, x.cams[k], flag=True)
        r, photo, valid = mae_row(img, cov, x.cams[k], fimg)
        res["OR_insample_4cam"].append(r)
    prims, images, info = build(x, "orc", CAMS_ALL, False, False, False, flag=True)
    for k in CAMS_ALL:
        img, cov, _ = render(x, prims, images, x.cams[k])
        fimg, _, _ = render(x, prims, images, x.cams[k], flag=True)
        r, photo, valid = mae_row(img, cov, x.cams[k], fimg)
        res["ORc_insample_4cam"].append(r)
    prims = baseline_prims(x)
    prims2 = baseline_prims(x, faces_keep(x, CAMS_ALL)[0])
    for key, pr in (("TEX_insample_4cam(지금 맵)", prims), ("TEX+2_insample_4cam", prims2)):
        for k in CAMS_ALL:
            img, cov, _ = render(x, pr, {}, x.cams[k])
            r, photo, valid = mae_row(img, cov, x.cams[k])
            res[key].append(r)
    summary = {k: mean_rows([{kk: vv for kk, vv in r.items() if kk != "cam"} for r in v]) for k, v in res.items()}
    return res, summary


# --------------------------------------------------------------------------- 자유 시점
VIEWS = {"top": ([4.1, 4.2, 9.5], [4.1, 4.15, 0.0], 70.0),
         "obl1": ([4.0, 4.0, 3.6], [0.3, 9.0, 0.3], 80.0),
         "obl2": ([4.0, 4.0, 3.6], [8.2, 1.0, 0.4], 80.0),
         "obl3": ([4.0, 5.0, 3.0], [1.0, 0.2, 0.5], 80.0)}


def label(img, text):
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(out, text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def free_views(x, extra_floor_lenient=True):
    sets = {
        "current": build(x, "tex", CAMS_ALL, False, False, False),
        "current+234": build(x, "tex", CAMS_ALL, True, True, True),
        "OR": build(x, "or", CAMS_ALL, False, False, False),
        "OR+234": build(x, "or", CAMS_ALL, True, True, True),
        "ORc+234": build(x, "orc", CAMS_ALL, True, True, True),
    }
    if extra_floor_lenient:
        sets["OR+234(floor lenient)"] = build(x, "or", CAMS_ALL, True, True, True, floor_mode="lenient")
    paths = {}
    for vn, (e, tg, fov) in VIEWS.items():
        cam = V.lookat_camera(e, tg, hfov_deg=fov)
        tiles = {}
        for sn, (prims, images, info) in sets.items():
            img, cov, _ = render(x, prims, images, cam, long_side=1280)
            tiles[sn] = label(img, f"{vn}: {sn}")
            cv2.imwrite(str(COMPARE / f"free_{vn}_{sn.replace('+', '_').replace(' ', '').replace('(', '_').replace(')', '')}.jpg"),
                        img[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
        row = np.concatenate([tiles["current"], tiles["current+234"], tiles["OR+234"], tiles["ORc+234"]], 1)
        p = COMPARE / f"compare_{vn}.jpg"
        cv2.imwrite(str(p), row[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 88])
        paths[vn] = str(p)
        print("saved", p, flush=True)
    return paths, sets


# --------------------------------------------------------------------------- GLB 내보내기
def export_glb(path, prims, images):
    import trimesh
    from PIL import Image

    from simai_ai.scene.texture_mesh import GLTF_Y_UP

    scene = trimesh.Scene()
    for p in prims:
        if p.image is None or p.uv is None:
            continue
        # 쓰이는 정점만 추려 작게
        used, inv = np.unique(p.idx.reshape(-1), return_inverse=True)
        pos = p.pos[used] @ GLTF_Y_UP.T
        uv = p.uv[used].copy()
        uv[:, 1] = 1.0 - uv[:, 1]  # trimesh 는 v 원점 아래, 내보낼 때 다시 뒤집는다
        tm = trimesh.Trimesh(pos, inv.reshape(-1, 3), process=False)
        tm.visual = trimesh.visual.TextureVisuals(uv=uv, image=Image.fromarray(images[p.image]))
        scene.add_geometry(tm, geom_name=p.name)
    scene.export(str(path))
    return Path(path).stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "setup", "loo", "free", "glb"])
    a = ap.parse_args()
    COMPARE.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    x = setup()
    print("setup", round(time.time() - t0, 1), "s; floor", x.z0, x.zinfo, "planes", len(x.planes), flush=True)
    report = {"config": CFG, "floor_level": x.zinfo, "z0": x.z0, "planes": x.planes, "planes_rejected": x.rej, "plane_info": x.pinfo,
              "atlas": {k: x.atlas[k] for k in ("size", "sec", "charts")}, "vis_sec": x.t.get("vis_sec")}
    long_, slant = faces_keep(x, CAMS_ALL)[1:]
    n, area, e, cen = M.face_geom(x.mesh.V, x.mesh.F)
    report["stretched"] = {"faces": int(len(x.mesh.F)), "long_edge_faces": int(long_.sum()), "slant_faces": int(slant.sum()),
                           "removed_area_frac": float(area[long_ | slant].sum() / area.sum()),
                           "edge_median_m": float(np.median(e)), "edge_p99_m": float(np.percentile(e.max(1), 99)),
                           "faces_seen_by_any_cam": float((x.score.max(1) > 0).mean()),
                           "sweep": {str(cs): float(M.stretched_faces(x.mesh, x.vis, CAMS_ALL, cs, CFG["long_edge"])[1].sum()) for cs in (0.15, 0.2, 0.25, 0.3, 0.4)}}
    if a.stage in ("all", "setup", "glb"):
        t1 = time.time()
        prims, images, info = build(x, "or", CAMS_ALL, True, True, True)
        report["or_full_build_sec"] = time.time() - t1
        report["or_bake"] = info["bake"]
        report["or_gains"] = info["gains"]
        _, _, infoc = build(x, "orc", CAMS_ALL, True, True, True)
        report["orc_consistency"], report["orc_bake"] = infoc["consistency"], infoc["bake"]
        report["floor"] = info["floor"]
        report["walls"] = info["walls"]
        cv2.imwrite(str(OUT / "openreco_atlas.png"), info["rgb"][:, :, ::-1])
        sz = export_glb(OUT / "openreco_234.glb", prims, images)
        report["glb_bytes"] = sz
        p2, im2, info2 = build(x, "tex", CAMS_ALL, True, True, True)
        report["tex234_floor"] = info2["floor"]
        print("glb", sz, flush=True)
        (OUT / "report_setup.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=float))
    if a.stage in ("all", "loo"):
        res, summary = loo(x)
        report["loo"], report["loo_summary"] = res, summary
        (OUT / "report_loo.json").write_text(json.dumps({"loo": res, "summary": summary}, indent=1, ensure_ascii=False))
        print(json.dumps(summary, indent=1, ensure_ascii=False))
    if a.stage in ("all", "free"):
        paths, sets = free_views(x)
        report["compare_images"] = paths
        for k, (pr, im, inf) in sets.items():
            if "floor" in inf:
                report[f"floor_{k}"] = inf["floor"]
    (OUT / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=float))
    print("total", round(time.time() - t0, 1), "s")


if __name__ == "__main__":
    main()
