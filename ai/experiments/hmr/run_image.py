# EXP-02 E-GATE-0 (S15P31A101-86): 선행 PoC Moge-3-test/pocs/human_fast_sam3d_g3_lite/scripts 에서 옮김. 경로·입력 준비만 바꿨다.
"""Fast SAM 3D Body 이미지 추론 → 렌더 / 메쉬 / 원본 npz / 지표.

  bash run_wsl.sh scripts/run_image.py --run-id A1_fast_official_smoke --images inputs/smoke --mode official --preset opt
  bash run_wsl.sh scripts/run_image.py --run-id A3_fast_split_all --images inputs/images --mode split --preset opt

mode
  official   : 공식 demo 구성 — YOLO26x-Pose 검출 + MoGe2 FOV + HMR 동시 GPU 적재, hand_box_source=yolo_pose
  split      : 기존 PoC 와 같은 bbox·K (A3_vith_split_all 출력에서 복원) → HMR 만. hand_box_source=body_decoder (병렬 decoder 비활성)
  split_pose : split 과 같은 bbox·K + 같은 bbox 에 IoU 매칭한 YOLO26x-Pose keypoints (사전 계산, 해제 후 HMR) → 공식 병렬 decoder 경로
"""
import argparse
import gc
import sys
import time
import traceback
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", required=True); ap.add_argument("--images", nargs="+", required=True)
ap.add_argument("--variant", default="dinov3"); ap.add_argument("--preset", default="opt")
ap.add_argument("--env", nargs="*", default=[], help="추가 환경변수 K=V")
ap.add_argument("--mode", default="split", choices=["official", "split", "split_pose"])
ap.add_argument("--inference-type", default="full", choices=["full", "body"])
ap.add_argument("--yolo", default=None)
ap.add_argument("--bbox-thr", type=float, default=0.5)
ap.add_argument("--no-render", action="store_true")
a = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fs3db_common as C  # noqa: E402
env = C.apply_preset(a.preset, a.env)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402


def images(paths):
    out = []
    for p in paths:
        p = Path(p)
        out += sorted(p.glob("*.jpg")) if p.is_dir() else [p]
    return out


def reproj_err(p, K):
    """pred_keypoints_2d 와 (kp3d + cam_t) 를 K 로 투영한 값의 평균 px 차 (좌표계·단위 검증)."""
    X = p["pred_keypoints_3d"] + p["pred_cam_t"][None]
    f = float(np.asarray(p["focal_length"]).reshape(-1)[0])
    u = f * X[:, 0] / X[:, 2] + K[0, 2]; v = f * X[:, 1] / X[:, 2] + K[1, 2]
    return float(np.mean(np.hypot(u - p["pred_keypoints_2d"][:, 0], v - p["pred_keypoints_2d"][:, 1])))


def main():
    rid = a.run_id
    res = {"run_id": rid, "args": vars(a), "preset_env": env, "env": C.env_summary(), "images": []}
    gs = C.GpuSampler()
    files = images(a.images)
    try:
        pre = {}
        if a.mode in ("split", "split_pose"):
            for f in files:
                img, boxes, K = C.baseline_inputs(f.stem)
                pre[f.stem] = {"boxes": boxes, "K": K}
            if a.mode == "split_pose":
                from tools.build_detector import HumanDetector
                torch.cuda.reset_peak_memory_stats(); t0 = time.perf_counter()
                det = HumanDetector(name="yolo_pose", device="cuda", model=str(a.yolo or C.YOLO))
                res["yolo_load_s"] = round(time.perf_counter() - t0, 2)
                for f in files:
                    img = cv2.imread(str(f)); C.sync(); t0 = time.perf_counter()
                    r = det.run_human_detection(img, det_cat_id=0, bbox_thr=0.3, nms_thr=0.3, default_to_full_image=False)
                    C.sync()
                    kps, ious = C.match_kps(pre[f.stem]["boxes"], r["boxes"], r["keypoints"])
                    pre[f.stem].update(kps=kps, match_iou=ious, yolo_ms=round((time.perf_counter() - t0) * 1000, 1), yolo_n=len(r["boxes"]))
                res["stage1_yolo_peak_alloc_mb"] = C.mb(torch.cuda.max_memory_allocated())
                del det; gc.collect(); torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            est, comps, timed, info = C.build(a.variant, precomputed_pose=a.mode == "split_pose")
        else:
            est, comps, timed, info = C.build(a.variant, detector="yolo_pose", detector_model=str(a.yolo or C.YOLO), fov="moge2")
        res["load"] = info
        hbs = "body_decoder" if a.mode == "split" else "yolo_pose"
        for f in files:
            img = cv2.imread(str(f)); row = {"image": f.name, "size": f"{img.shape[1]}x{img.shape[0]}"}
            try:
                if a.mode == "official":
                    out, st = C.run_one(est, timed, img, inference_type=a.inference_type, bbox_thr=a.bbox_thr, hand_box_source=hbs)
                    H, W = img.shape[:2]
                    fo = float(np.asarray(out[0]["focal_length"]).reshape(-1)[0]) if out else 0
                    K = np.array([[fo, 0, W / 2], [0, fo, H / 2], [0, 0, 1]])
                else:
                    pr = pre[f.stem]; K = pr["K"]
                    if a.mode == "split_pose":
                        comps["det"].set(pr["boxes"], pr["kps"])
                        row.update(yolo_ms=pr["yolo_ms"], kps_match_iou=pr["match_iou"])
                        out, st = C.run_one(est, timed, img, cam_int=K, inference_type=a.inference_type, hand_box_source=hbs)
                    else:
                        out, st = C.run_one(est, timed, img, bboxes=pr["boxes"], cam_int=K, inference_type=a.inference_type, hand_box_source=hbs)
                row.update(st)
                row["finite"] = C.finite(out)
                row["reproj_err_px"] = [round(reproj_err(p, K), 4) for p in out]
                row["result"] = "SUCCESS" if out and row["finite"] else ("NO_PERSON" if not out else "NAN_OUTPUT")
                C.save_native(C.OUT / rid / "native" / f"{f.stem}.npz", out)
                row["meshes"] = C.save_meshes(C.OUT / rid / "meshes", f.stem, out, est.faces)
                if not a.no_render and out:
                    t0 = time.perf_counter()
                    vis = C.render(img, out, est.faces)
                    row["render_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    d = C.OUT / rid / "renders"; d.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(d / f"{f.stem}.jpg"), vis)
            except torch.cuda.OutOfMemoryError as e:
                row.update(result="OOM", error=str(e)[:300], peak_alloc_mb=C.mb(torch.cuda.max_memory_allocated()))
                gc.collect(); torch.cuda.empty_cache()
            except Exception as e:  # noqa: BLE001
                row.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:400], traceback=traceback.format_exc()[-1500:])
            print(f"[{rid}] {f.name} {row.get('result')} n={row.get('n_people')} total={row.get('process_one_image_ms')} ms "
                  f"hmr={row.get('hmr_ms')} det={row.get('detect_ms')} fov={row.get('fov_ms')} peak={row.get('peak_alloc_mb')} MB reproj={row.get('reproj_err_px')}", flush=True)
            res["images"].append(row)
        ok = [r for r in res["images"] if r.get("result") == "SUCCESS"]
        res["result"] = "SUCCESS" if len(ok) == len(files) else ("PARTIAL" if ok else "FAIL")
    except torch.cuda.OutOfMemoryError as e:
        res.update(result="OOM", load_fail=getattr(e, "load_info", None), error=str(e)[:600], traceback=traceback.format_exc()[-2000:])
    except Exception as e:  # noqa: BLE001
        res.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:600], traceback=traceback.format_exc()[-3000:])
    res.update(gs.stop())
    C.write_json(C.OUT / rid / "metrics.json", res)
    print(f"[{rid}] RESULT {res['result']} {res.get('error', '')[:300]}")


if __name__ == "__main__":
    main()
