"""EXP-01 E-GATE-0: MapAnything Native 추론을 RunPod에서 실행하고 지표·산출물을 저장한다(S15P31A101-74).

선행 PoC(Moge-3-test/pocs/mapanything_room4/scripts/run_mapanything.py)를 옮긴 것이다. 공식 API만 쓴다.

모드:
  images  이미지만 입력(load_images). PoC와 같은 경로.
  known   이미지 + 알려진 intrinsics(+ camera_poses). 이전 Run의 cameras.json을 입력으로 쓰고,
          --pose-scale 로 pose 이동량을 늘려 모델이 pose 입력을 실제로 쓰는지 시험한다.
  prepared  보정용 known-K(S15P31A101-102). `simai_ai.calibration.world_frame.cli prepare` 가 AnyCalib 왜곡을 편 영상과
          펴진 K(prepared.json)를 만들고, 여기서는 그 K 를 intrinsics 로 넣는다(pose 는 넣지 않음). 사람 마스크는 모델 입력을
          바꾸지 않으려고 여기서 쓰지 않고, 노트북의 World 고정(`world_frame.cli fix --prepared`)이 점에서 뺀다.

  python run_mapanything.py --run-id C1_room4_bf16 --images /workspace/simai/datasets/room4
  python run_mapanything.py --run-id C2_room4_apache --model /workspace/simai/weights/map-anything-apache ...
  python run_mapanything.py --run-id C4b_room4_known_pose_x1.25 --mode known --known-from <C1>/cameras.json --use-poses 1 --pose-scale 1.25

좌표: 출력 world = MapAnything world, camera_poses는 OpenCV(+X 오른쪽, +Y 아래, +Z 앞) cam2world, 단위 m(모델 추정, 실측 미검증).
출력: <runs>/<run_id>/{metrics.json, cameras.json, raw.npz, fused.ply, pointcloud.glb, gridmesh.glb, panels/}
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
ROOT = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai"))
# MapAnything 이미지(deploy/mapanything)는 코드를 이미지 안에 두고 SIMAI_MAPANYTHING_REPO 로 가리킨다. 없으면 볼륨 설치(setup_runpod.sh) 위치
MA_REPO = Path(os.environ.get("SIMAI_MAPANYTHING_REPO") or ROOT / "third_party" / "map-anything")

import numpy as np  # noqa: E402
import torch  # noqa: E402


def ma_commit():
    """이미지 사본에는 .git 이 없어 이미지가 적어 둔 커밋(SIMAI_MAPANYTHING_COMMIT)을 쓴다."""
    try:
        out = subprocess.run(["git", "-C", str(MA_REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:  # 이미지에는 git 이 없다
        out = ""
    return out or os.environ.get("SIMAI_MAPANYTHING_COMMIT", "")


def mb(x):
    return round(x / 2**20, 1)


def sha256_file(p: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def gpu_used():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return -1


class Sampler:
    """nvidia-smi 기준 GPU 메모리 최대치를 0.2초마다 잰다."""

    def __init__(self):
        self.base = self.peak = gpu_used()
        self.stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self.stop:
            self.peak = max(self.peak, gpu_used())
            time.sleep(0.2)

    def done(self):
        self.stop = True
        self.t.join(1)
        return {"gpu_used_base_mb": self.base, "gpu_used_peak_mb": self.peak}


def stats(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not len(x):
        return {}
    return {k: round(float(v), 4) for k, v in
            {"min": x.min(), "p5": np.percentile(x, 5), "p50": np.median(x), "p95": np.percentile(x, 95), "max": x.max()}.items()}


def write_ply(path, xyz, rgb):
    from plyfile import PlyData, PlyElement
    v = np.empty(len(xyz), dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    v["x"], v["y"], v["z"] = xyz.T.astype(np.float32)
    v["red"], v["green"], v["blue"] = rgb.T.astype(np.uint8)
    PlyData([PlyElement.describe(v, "vertex")], text=False).write(str(path))


def colorize(x, mask, cmap):
    import matplotlib
    v = x[mask] if mask.any() else x[np.isfinite(x)]
    lo, hi = np.percentile(v, 2), np.percentile(v, 98)
    c = matplotlib.colormaps[cmap](np.clip((x - lo) / max(hi - lo, 1e-9), 0, 1))[..., :3]
    c[~mask] = 0.15
    return (c * 255).astype(np.uint8)


def build_known_views(paths, known_json, use_poses, pose_scale, device):
    """이전 Run의 카메라(모델 해상도 intrinsics, cam2world)를 원본 해상도로 환산해 입력 view를 만든다."""
    import cv2
    cams = {v["name"]: v for v in json.loads(Path(known_json).read_text(encoding="utf-8"))["views"]}
    views = []
    for p in paths:
        name = Path(p).stem
        c = cams[name]
        img = cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2RGB)
        oh, ow = img.shape[:2]
        mh, mw = c["model_input_hw"]
        K = np.array(c["intrinsics_model_res"], float)
        # load_images는 비율 유지 resize 후 가운데 crop → 원본 해상도 환산은 근사(세로 crop 오차 작음)
        s = ow / mw
        K_orig = K.copy()
        K_orig[0, :] *= s
        K_orig[1, :] *= s
        K_orig[1, 2] = (K[1, 2] - mh / 2) * s + oh / 2
        v = {"img": torch.from_numpy(img), "intrinsics": torch.tensor(K_orig, dtype=torch.float32)}
        if use_poses:
            T = np.array(c["cam2world"], float)
            T[:3, 3] *= pose_scale
            v["camera_poses"] = torch.tensor(T, dtype=torch.float32)
            v["is_metric_scale"] = torch.tensor([True])
        views.append(v)
    return views


def prepared_views(prepared_dir):
    """prepared.json → [{name, path, K(원본 해상도, 왜곡 편 영상)}] (torch 없이 읽는다)."""
    d = Path(prepared_dir)
    doc = json.loads((d / "prepared.json").read_text(encoding="utf-8"))
    if doc.get("format") != "simai.mapanything_inputs":
        raise ValueError(f"prepared.json 형식이 아니다: {doc.get('format')!r}")
    out = []
    for v in doc["views"]:
        K = np.asarray(v["K_undistorted"], dtype=float)
        if K.shape != (3, 3) or not (K[0, 0] > 0 and K[1, 1] > 0):
            raise ValueError(f"{v['name']}: K_undistorted 가 3×3 양의 초점 행렬이 아니다")
        out.append({"name": v["name"], "path": str(d / v["image"]), "K": K})
    return out


def build_prepared_views(specs):
    import cv2
    views = []
    for sp in specs:
        img = cv2.cvtColor(cv2.imread(sp["path"]), cv2.COLOR_BGR2RGB)
        views.append({"img": torch.from_numpy(img), "intrinsics": torch.tensor(sp["K"], dtype=torch.float32)})
    return views


def resize_kwargs(mode: str, size: str | None) -> dict:
    """--resize-mode·--size → load_images/preprocess_inputs 인자. fixed_mapping 은 공식 기본(인자 없음)과 같다."""
    if mode == "fixed_mapping":
        if size:
            raise SystemExit("--size 는 fixed_mapping 에 쓰지 않는다(긴 변 518 고정)")
        return {}
    if not size:
        raise SystemExit(f"--resize-mode {mode} 에는 --size 가 필요하다")
    if mode == "fixed_size":
        w, sep, h = size.lower().partition("x")
        if not sep or not w.isdigit() or not h.isdigit():
            raise SystemExit(f"fixed_size 의 --size 는 가로x세로(예 2304x1296): {size!r}")
        return {"resize_mode": mode, "size": (int(w), int(h))}
    if not size.isdigit():
        raise SystemExit(f"{mode} 의 --size 는 정수: {size!r}")
    return {"resize_mode": mode, "size": int(size)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--images", default=str(ROOT / "datasets" / "room4"))
    ap.add_argument("--model", default=str(ROOT / "weights" / "map-anything"))
    ap.add_argument("--mode", choices=["images", "known", "prepared"], default="images")
    ap.add_argument("--prepared", help="prepared 모드: world_frame.cli prepare 출력 폴더(prepared.json)")
    ap.add_argument("--known-from", help="known 모드: 입력으로 쓸 이전 Run의 cameras.json")
    ap.add_argument("--use-poses", type=int, default=0, help="known 모드: camera_poses 도 입력")
    ap.add_argument("--pose-scale", type=float, default=1.0, help="known 모드: 입력 pose 이동량 배율(사용 여부 시험)")
    ap.add_argument("--amp", type=int, default=1)
    ap.add_argument("--amp-dtype", default="bf16")
    ap.add_argument("--minibatch", type=int, default=0, help="0 = 공식 기본(동적)")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--conf-percentile", type=float, default=10)
    ap.add_argument("--resize-mode", choices=["fixed_mapping", "longest_side", "fixed_size", "square"], default="fixed_mapping",
                    help="MapAnything 전처리 resize_mode (공식 기본 fixed_mapping = 긴 변 518)")
    ap.add_argument("--size", default=None,
                    help="longest_side·square: 정수(예 1036), fixed_size: 가로x세로(예 2304x1296, 14 배수로 내림)")
    ap.add_argument("--runs-dir", default=str(ROOT / "runs" / "exp01"))
    ap.add_argument("--git-commit", default="", help="이 스크립트가 있는 저장소 커밋(재현용)")
    a = ap.parse_args()
    resize = resize_kwargs(a.resize_mode, a.size)

    sys.path.insert(0, str(MA_REPO))
    from mapanything.models import MapAnything
    from mapanything.utils.geometry import depthmap_to_world_frame
    from mapanything.utils.image import load_images, preprocess_inputs
    from mapanything.utils.viz import predictions_to_glb
    import cv2

    out = Path(a.runs_dir) / a.run_id
    (out / "panels").mkdir(parents=True, exist_ok=True)
    specs = prepared_views(a.prepared) if a.mode == "prepared" else None
    if specs is not None:
        paths, names = [sp["path"] for sp in specs], [sp["name"] for sp in specs]
    else:
        paths = sorted(glob.glob(os.path.join(a.images, "*.jpg")) + glob.glob(os.path.join(a.images, "*.png")))
        names = [Path(p).stem for p in paths]
    lock = ROOT / "envs" / "mapanything.lock.txt"
    weights = sorted(Path(a.model).glob("*.safetensors"))
    res = {
        "run_id": a.run_id, "experiment_id": "EXP-01", "e_gate": "E-GATE-0", "jira_key": "S15P31A101-74",
        **({"experiment_id": "VAL-01", "e_gate": None, "jira_key": "S15P31A101-102"} if a.mode == "prepared" else {}),
        "started_at": datetime.now(timezone.utc).isoformat(), "args": vars(a), "result": "ERROR",
        "git_commit": a.git_commit,
        # 이미지에서는 이미지 다이제스트(deploy/README "env_hash"), 볼륨 venv 는 잠금 파일 해시
        "env_hash": os.environ.get("SIMAI_IMAGE_DIGEST") or (sha256_file(lock) if lock.exists() else None),
        "model_version": f"map-anything@{ma_commit()} + {Path(a.model).name}",
        "weights_hash": sha256_file(weights[0]) if weights else None,
        "input_hash": "sha256:" + hashlib.sha256(b"".join(Path(p).read_bytes() for p in paths)).hexdigest(),
        "images": [Path(p).name for p in paths],
        "prepared_sha256": sha256_file(Path(a.prepared) / "prepared.json") if a.mode == "prepared" else None,
        "gpu": {"model": torch.cuda.get_device_name(0), "torch": torch.__version__, "cuda": torch.version.cuda,
                "driver": subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()},
    }
    smp = Sampler()
    try:
        t0 = time.perf_counter()
        model = MapAnything.from_pretrained(a.model).to("cuda").eval()
        torch.cuda.synchronize()
        res["load_s"] = round(time.perf_counter() - t0, 1)
        res["alloc_after_load_mb"] = mb(torch.cuda.memory_allocated())

        t1 = time.perf_counter()
        if a.mode == "images":
            views = load_images(paths, **resize)
        elif a.mode == "prepared":
            views = preprocess_inputs(build_prepared_views(specs), **resize)
        else:
            views = preprocess_inputs(build_known_views(paths, a.known_from, a.use_poses, a.pose_scale, "cuda"), **resize)
        res["preprocess_ms"] = round((time.perf_counter() - t1) * 1000, 1)
        res["model_input_hw"] = list(views[0]["img"].shape[-2:])
        res["resize"] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in resize.items()}
        kw = dict(memory_efficient_inference=True, minibatch_size=a.minibatch or None, use_amp=bool(a.amp),
                  amp_dtype=a.amp_dtype, apply_mask=True, mask_edges=True)
        res["infer_kwargs"] = kw
        times = []
        for _ in range(a.repeat):
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            t2 = time.perf_counter()
            with torch.no_grad():
                outputs = model.infer(views, **kw)
            torch.cuda.synchronize()
            times.append(round((time.perf_counter() - t2) * 1000, 1))
            print(f"[{a.run_id}] infer {times[-1]} ms, peak alloc {mb(torch.cuda.max_memory_allocated())} MB", flush=True)
        res.update(infer_ms=times, infer_ms_warm=round(float(np.mean(times[1:] if len(times) > 1 else times)), 1),
                   peak_alloc_mb=mb(torch.cuda.max_memory_allocated()), peak_reserved_mb=mb(torch.cuda.max_memory_reserved()),
                   result="SUCCESS")
    except torch.cuda.OutOfMemoryError as e:
        res.update(result="OOM", error=str(e)[:500])
    except Exception as e:  # noqa: BLE001
        import traceback
        res.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:500], traceback=traceback.format_exc()[-2000:])
    res.update(smp.done())
    if res["result"] != "SUCCESS":
        (out / "metrics.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[{a.run_id}] {res['result']}: {res.get('error', '')[:300]}")
        return 1

    cams = {"run_id": a.run_id, "convention": "cam2world 4x4, OpenCV, MapAnything world, m (NOT_VERIFIED)", "views": []}
    raw, per_view, W_pts, W_img, W_mask, all_xyz, all_rgb = {}, [], [], [], [], [], []
    for pred, name, p in zip(outputs, names, paths):
        g = lambda k: pred[k][0].float().cpu().numpy() if torch.is_tensor(pred[k]) else np.asarray(pred[k])  # noqa: E731
        depth, K, T, conf = g("depth_z")[..., 0], g("intrinsics"), g("camera_poses"), g("conf")
        mask, img = g("mask")[..., 0].astype(bool), g("img_no_norm")
        pts, valid = depthmap_to_world_frame(pred["depth_z"][0].squeeze(-1), pred["intrinsics"][0], pred["camera_poses"][0])
        pts, valid = pts.float().cpu().numpy(), valid.cpu().numpy().astype(bool)
        fmask = mask & valid & np.isfinite(pts).all(-1)
        thr = np.percentile(conf[fmask], a.conf_percentile) if fmask.any() else np.inf
        pmask = fmask & (conf >= thr)
        scale = float(g("metric_scaling_factor").reshape(-1)[0]) if "metric_scaling_factor" in pred else None
        rgb8 = (img * 255).astype(np.uint8)
        raw.update({f"{name}__depth_z": depth, f"{name}__conf": conf, f"{name}__mask": fmask,
                    f"{name}__intrinsics": K, f"{name}__camera_pose": T})
        h, w = depth.shape
        cams["views"].append({"name": name, "model_input_hw": [h, w], "intrinsics_model_res": K.tolist(), "cam2world": T.tolist(),
                              "metric_scaling_factor": scale,
                              "fov_x_deg": round(float(np.degrees(2 * np.arctan(w / 2 / K[0, 0]))), 2)})
        panel = np.concatenate([rgb8, colorize(depth, fmask, "turbo"), colorize(conf, np.ones_like(fmask), "viridis")], 1)
        cv2.imwrite(str(out / "panels" / f"{name}.jpg"), cv2.cvtColor(panel, cv2.COLOR_RGB2BGR))
        per_view.append({"name": name, "valid_ratio": round(float(fmask.mean()), 4), "depth_m": stats(depth[fmask]),
                         "conf_valid": stats(conf[fmask]), "metric_scaling_factor": scale,
                         "camera_center": T[:3, 3].round(4).tolist(),
                         "rotation_orthonormal_err": round(float(np.abs(T[:3, :3] @ T[:3, :3].T - np.eye(3)).max()), 6)})
        W_pts.append(pts); W_img.append(img); W_mask.append(fmask)
        all_xyz.append(pts[pmask]); all_rgb.append(rgb8[pmask])

    # known 모드: 입력 pose와 출력 pose 비교(모델이 pose 입력을 쓰는지)
    if a.mode == "known" and a.use_poses:
        src = {v["name"]: np.array(v["cam2world"], float) for v in json.loads(Path(a.known_from).read_text(encoding="utf-8"))["views"]}
        cmp = []
        for v in cams["views"]:
            Ti = src[v["name"]].copy()
            Ti[:3, 3] *= a.pose_scale
            To = np.array(v["cam2world"])
            dR = Ti[:3, :3].T @ To[:3, :3]
            cmp.append({"name": v["name"], "center_err_m": round(float(np.linalg.norm(Ti[:3, 3] - To[:3, 3])), 4),
                        "rot_err_deg": round(float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1) / 2, -1, 1)))), 3)})
        res["pose_input_vs_output"] = cmp
        # 카메라 간 거리 비(출력/입력): pose_scale 을 따라가면 입력 pose 를 쓴 것
        ci = np.array([src[v["name"]][:3, 3] * a.pose_scale for v in cams["views"]])
        co = np.array([v["cam2world"] for v in cams["views"]])[:, :3, 3]
        di = np.linalg.norm(ci[:, None] - ci[None], axis=-1)
        do = np.linalg.norm(co[:, None] - co[None], axis=-1)
        iu = np.triu_indices(len(ci), 1)
        res["camera_distance_ratio_out_over_in"] = stats(do[iu] / np.maximum(di[iu], 1e-9))

    xyz, rgb = np.concatenate(all_xyz), np.concatenate(all_rgb)
    write_ply(out / "fused.ply", xyz, rgb)
    np.savez_compressed(out / "raw.npz", names=np.array(names), **raw)
    (out / "cameras.json").write_text(json.dumps(cams, indent=1), encoding="utf-8")
    pr = {"world_points": np.stack(W_pts), "images": np.stack(W_img), "final_masks": np.stack(W_mask)}
    predictions_to_glb({k: v.copy() for k, v in pr.items()}, as_mesh=True).export(str(out / "gridmesh.glb"))
    predictions_to_glb({k: v.copy() for k, v in pr.items()}, as_mesh=False).export(str(out / "pointcloud.glb"))
    res.update(per_view=per_view, points_conf_filtered=int(len(xyz)),
               fused_bbox_p1_p99_m={"p1": np.percentile(xyz, 1, 0).round(3).tolist(), "p99": np.percentile(xyz, 99, 0).round(3).tolist()},
               finished_at=datetime.now(timezone.utc).isoformat())
    (out / "metrics.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[{a.run_id}] SUCCESS warm {res['infer_ms_warm']} ms, peak alloc {res['peak_alloc_mb']} MB, gpu peak {res['gpu_used_peak_mb']} MB, points {len(xyz)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
