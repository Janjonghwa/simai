"""현재 맵 메시 + 사진 4장으로 Docker texrecon 을 직접 돌려 설정별 시간·품질을 잰다(S15P31A101-511). 실험 전용(CPU, GPU 없음).

운영 `texture_mesh` 의 Texrecon(Docker 실행기)·write_scene·bake·export_glb 를 그대로 쓴다. 기하는 현재 맵(4대로 만든 TSDF)이라
한 대 빼고 평가에서 기하는 누설이고 텍스처만 정직하다(-505 와 같은 척도: 3대로 굽고 뺀 카메라 사진과 MAE·덮임, 4회 평균).

usage: python bake_exp.py --name base [--term area] [--outlier none] [--faces 150000] [--scale 1.0] [--threads 0] [--no-loo] [--no-free]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time

import cv2
import numpy as np

import common as C
from simai_ai.scene import texture_mesh as tm
from simai_ai.scene import texture_provenance as tp

ge = C.ge
SPEC = "docker:simai/texrecon:dev"


def prepare_photos(cams, scale: float, out):
    """카메라별 (펴진) 사진을 scale 로 줄여 jpg 로 쓰고 texrecon 입력 dict 를 낸다. 사람 지운 배경이라 마스크는 유효 영역만 쓴다."""
    out.mkdir(parents=True, exist_ok=True)
    tex = {}
    for c in cams:
        H0, W0 = c.image.shape[:2]
        H, W = round(H0 * scale), round(W0 * scale)
        img = c.image if scale == 1.0 else cv2.resize(c.image, (W, H), interpolation=cv2.INTER_AREA)
        valid = c.valid if scale == 1.0 else cv2.resize(c.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
        p = out / f"{c.camera_id}.jpg"
        cv2.imwrite(str(p), img[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 95])
        K = np.asarray(c.K, float).copy()
        K[0] *= W / W0
        K[1] *= H / H0
        K[0, 2] = (c.K[0, 2] + 0.5) * W / W0 - 0.5
        K[1, 2] = (c.K[1, 2] + 0.5) * H / H0 - 0.5
        tex[c.camera_id] = {"path": p, "K": K, "hw": (H, W), "person": ~valid, "rgb": img}
    return tex


def read_timings(out_dir):
    """texrecon --write_timings CSV(단계,초)를 읽는다."""
    res = {}
    for f in sorted(out_dir.rglob("*_timings.csv")):
        for line in f.read_text(errors="replace").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2:
                try:
                    res[f"{f.parent.name}/{parts[0]}"] = float(parts[1])
                except ValueError:
                    pass
    return res


def render_glb(glb, cam, long_side=1152):
    g = ge.parse_glb(glb.read_bytes())
    model = ge.load_scene(g)
    cfg = ge.EnhanceConfig(check_render_long_side=long_side)
    return ge.render_view(g, model, model.prims, cam, cfg)


def mae_row(img, cov, cam):
    H, W = img.shape[:2]
    photo = cv2.resize(cam.image, (W, H), interpolation=cv2.INTER_AREA)
    valid = cv2.resize(cam.valid.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
    d = (np.abs(img.astype(np.float32) - photo.astype(np.float32)) / 255.0).mean(axis=2)
    both = valid & cov
    return {"mae_valid": float(d[valid].mean()), "mae_covered": float(d[both].mean()) if both.any() else None,
            "coverage": float(both.sum() / valid.sum())}, photo


def export(b, V, F, path):
    return tm.export_glb(path, V, F, b, np.eye(4), {"axis_convention": "GLTF_Y_UP", "geometry_role": "VISUAL", "safety_use": False})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--term", default="area", choices=["area", "gmi"])
    ap.add_argument("--outlier", default="none", choices=["none", "gauss_damping", "gauss_clamping"], help="texrecon -o (데이터 비용 호출에만 붙인다)")
    ap.add_argument("--faces", type=int, default=150000)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--cpus", type=float, default=0.0, help="docker --cpus (0 = 제한 없음)")
    ap.add_argument("--no-loo", action="store_true")
    ap.add_argument("--no-free", action="store_true")
    a = ap.parse_args(argv)
    out = C.WORK / "runs" / a.name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rec = {"name": a.name, "args": vars(a), "times": {}}
    T = rec["times"]
    t_all = time.perf_counter()

    t = time.perf_counter()
    V, F = C.load_mesh()
    n_in = len(F)
    if a.faces >= 3 * n_in:  # 시간 대리 측정용: 한 번 중점 세분(면 수 4배). 품질은 의미 없다(같은 기하를 쪼갠 것)
        import open3d as o3d
        m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(V), o3d.utility.Vector3iVector(F)).subdivide_midpoint(1)
        V, F = np.asarray(m.vertices, float), np.asarray(m.triangles, np.int64)
    else:
        V, F = C.simplify(V, F, a.faces)
    _, first = np.unique(tp.face_keys(F), return_index=True)
    F = F[np.sort(first)]
    T["mesh_prep_s"] = round(time.perf_counter() - t, 2)
    rec["faces"] = int(len(F))
    rec["faces_input"] = int(n_in)
    cams = C.load_cameras()
    names = [c.camera_id for c in cams]
    tex = prepare_photos(cams, a.scale, out / "photos")
    poses = {c.camera_id: C.cam2world(c) for c in cams}
    cfg = tm.Config(num_threads=a.threads, target_faces=a.faces,
                    terms=(a.term,), loo_terms=())
    tm.write_scene(out / "scene", tex, poses, names)
    tm.write_ply(out / "mesh.ply", V, F)
    views = [tp.TexView(n, tex[n]["K"], poses[n], tex[n]["hw"], tex[n]["person"]) for n in names]
    pairs = tp.face_pairs(F)

    tr = tm.Texrecon(SPEC, out, a.threads)
    rec["texrecon"] = tr.describe()
    with tr:
        if a.cpus > 0 and tr.cid:
            subprocess.run(["docker", "update", f"--cpus={a.cpus}", tr.cid], check=True, capture_output=True)
        t = time.perf_counter()
        tr.run(out / "scene", out / "mesh.ply", out / "costs" / "tex", ["-d", a.term, *([] if a.outlier == "none" else ["-o", a.outlier]), *tm.SKIP_LEVELING, "--write_timings"])
        T["data_costs_s"] = round(time.perf_counter() - t, 2)
        costs = tp.read_spt(out / "costs" / "tex_data_costs.spt")
        t = time.perf_counter()
        entries, frec = tp.filter_costs(costs, V, F, views, 0.05)
        T["filter_s"] = round(time.perf_counter() - t, 2)
        t = time.perf_counter()
        b = tm.bake(tr, out / "all", out / "scene", out / "mesh.ply", V, F, costs, entries, views, cfg, pairs, a.term, 1.0, None)
        T["bake_total_s"] = round(time.perf_counter() - t, 2)
        for k, v in b.seconds.items():
            T[f"texrecon_{k}_s"] = v
        t = time.perf_counter()
        glb = out / "all" / "textured.glb"
        tris = export(b, V, F, glb)
        T["export_glb_s"] = round(time.perf_counter() - t, 2)
        rec["glb_bytes"], rec["glb_triangles"] = glb.stat().st_size, tris
        rec["ratios_area"] = tp.area_ratios(V, F, b.cls)
        rec["holes"] = b.holes
        rec["texrecon_internal_s"] = read_timings(out)
        if not a.no_loo:
            t = time.perf_counter()
            rows = []
            for k, name in enumerate(names):
                ent = entries[entries["view"] != k]
                bl = tm.bake(tr, out / f"loo-{name}", out / "scene", out / "mesh.ply", V, F, costs, ent, views, cfg, pairs, a.term, 1.0, None)
                gl = out / f"loo-{name}" / "textured.glb"
                export(bl, V, F, gl)
                img, cov, _ = render_glb(gl, cams[k])
                r, photo = mae_row(img, cov, cams[k])
                r["cam"] = name
                r["bake_s"] = round(sum(bl.seconds.values()), 2)
                rows.append(r)
                cv2.imwrite(str(out / f"loo_{name}.jpg"), np.concatenate([photo, img], 1)[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 85])
                shutil.rmtree(out / f"loo-{name}" / "final", ignore_errors=True)
            rec["loo"] = rows
            rec["loo_mean"] = {k: round(float(np.mean([r[k] for r in rows])), 5) for k in ("mae_valid", "mae_covered", "coverage")}
            T["loo_s"] = round(time.perf_counter() - t, 1)
        if not a.no_free:
            t = time.perf_counter()
            for vn, (e, tg, fov) in C.VIEWS.items():
                cam = C.lookat_camera(e, tg, hfov_deg=fov)
                img, _, _ = render_glb(glb, cam, 1280)
                cv2.imwrite(str(out / f"free_{vn}.jpg"), img[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            T["free_views_s"] = round(time.perf_counter() - t, 1)
    T["total_s"] = round(time.perf_counter() - t_all, 1)
    rec["texrecon_calls"] = tr.calls
    (out / "result.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(json.dumps({"name": a.name, "times": T, "loo_mean": rec.get("loo_mean"), "ratios": rec["ratios_area"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
