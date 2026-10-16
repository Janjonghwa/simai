# EXP-02 E-GATE-0 (S15P31A101-86): 선행 PoC Moge-3-test/pocs/human_fast_sam3d_g3_lite/scripts 에서 옮김. 경로·입력 준비만 바꿨다.
"""Fast SAM 3D Body 벤치마크 — 기존 PoC benchmark.py 와 같은 조건 (CUDA 동기화, warmup 3 / repeat 10, 큰 bbox 순 k 명).

  bash run_wsl.sh scripts/benchmark.py --run-id D1_fast_off --preset off
  bash run_wsl.sh scripts/benchmark.py --run-id D2_fast_opt --preset opt --modes split split_pose
  bash run_wsl.sh scripts/benchmark.py --run-id D4_fast_official_e2e --preset opt --e2e

입력: 1명 t1_front_taichi (544×960), 다인원 t3_multi_f050 (768×432). bbox·K = 기존 PoC A3 출력 그대로 (ViTDet CPU 박스, MoGe2-L K).
조건
  hmr_batch[k] / hmr_seq[k] : k = 1,2,3,4,5. mode split (body_decoder 손 박스) / split_pose (같은 박스 + YOLO26x-Pose kps → 병렬 decoder)
  e2e_official              : YOLO26x-Pose + MoGe2 + HMR 동시 적재, process_one_image 1 회 (검출·FOV·HMR 포함, 렌더 제외)
  render / mesh             : 공식 visualize_sample_together (llvmpipe CPU), PLY 저장
"""
import argparse
import csv
import gc
import sys
import tempfile
import time
import traceback
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", required=True); ap.add_argument("--variant", default="dinov3"); ap.add_argument("--preset", default="opt")
ap.add_argument("--env", nargs="*", default=[])
ap.add_argument("--images", nargs="+", default=["t1_front_taichi", "t3_multi_f050"])
ap.add_argument("--warmup", type=int, default=3); ap.add_argument("--repeat", type=int, default=10)
ap.add_argument("--ks", type=int, nargs="+", default=[1, 2, 3, 4, 5]); ap.add_argument("--types", nargs="+", default=["full", "body"])
ap.add_argument("--modes", nargs="+", default=["split"], choices=["split", "split_pose"])
ap.add_argument("--no-seq", action="store_true"); ap.add_argument("--e2e", action="store_true"); ap.add_argument("--no-post", action="store_true")
ap.add_argument("--yolo", default=None)
a = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fs3db_common as C  # noqa: E402
env = C.apply_preset(a.preset, a.env)
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402


def area(b):
    return (b[2] - b[0]) * (b[3] - b[1])


def main():
    rid = a.run_id
    res = {"run_id": rid, "args": vars(a), "preset_env": env, "env": C.env_summary(), "rows": []}
    rows = res["rows"]; gs = C.GpuSampler()

    def measure(label, fn, k, img_name, itype, mode, extra=None):
        row = {"condition": label, "mode": mode, "image": img_name, "n_people": k, "inference_type": itype, "warmup": a.warmup, "repeat": a.repeat, **(extra or {})}
        try:
            for _ in range(a.warmup):
                fn()
            torch.cuda.reset_peak_memory_stats()
            ts, hm, det, fv, npl, fin = [], [], [], [], [], []
            for _ in range(a.repeat):
                st = fn()
                ts.append(st["total_ms"]); hm.append(st.get("hmr_ms") or np.nan); det.append(st.get("detect_ms") or np.nan); fv.append(st.get("fov_ms") or np.nan)
                npl.append(st.get("n_out", k)); fin.append(st.get("finite", True))
            p = C.pct(ts)
            nm = lambda x: round(float(np.nanmean(x)), 2) if not np.all(np.isnan(x)) else None
            row.update(result="SUCCESS" if all(fin) else "NAN_OUTPUT", mean_ms=p["mean"], p50_ms=p["p50"], p95_ms=p["p95"], min_ms=p["min"],
                       hmr_mean_ms=nm(hm), detect_mean_ms=nm(det), fov_mean_ms=nm(fv), fps=round(1000 / p["mean"], 3),
                       persons_per_s=round(k * 1000 / p["mean"], 3), n_out=int(np.median(npl)),
                       peak_alloc_mb=C.mb(torch.cuda.max_memory_allocated()), peak_reserved_mb=C.mb(torch.cuda.max_memory_reserved()))
        except torch.cuda.OutOfMemoryError as e:
            row.update(result="OOM", error=str(e)[:300], peak_alloc_mb=C.mb(torch.cuda.max_memory_allocated())); gc.collect(); torch.cuda.empty_cache()
        except Exception as e:  # noqa: BLE001
            row.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:300], traceback=traceback.format_exc()[-1200:])
        row["gpu_used_peak_mb_sofar"] = gs.peak
        print(f"[{rid}] {row.get('condition')} {mode} {img_name} k={k} {itype} {row.get('result')} mean={row.get('mean_ms')} p95={row.get('p95_ms')} "
              f"hmr={row.get('hmr_mean_ms')} peak={row.get('peak_alloc_mb')}/{row.get('peak_reserved_mb')} {row.get('error', '')[:150]}", flush=True)
        rows.append(row)

    try:
        pre = {}
        for s in a.images:
            img, boxes, K = C.baseline_inputs(s)
            order = np.argsort([-area(b) for b in boxes]); boxes = boxes[order]
            pre[s] = {"img": img, "boxes": boxes, "K": K}
        if "split_pose" in a.modes:
            from tools.build_detector import HumanDetector
            det = HumanDetector(name="yolo_pose", device="cuda", model=str(a.yolo or C.YOLO))
            for s, d in pre.items():
                r = det.run_human_detection(d["img"], det_cat_id=0, bbox_thr=0.3, nms_thr=0.3, default_to_full_image=False)
                d["kps"], d["match_iou"] = C.match_kps(d["boxes"], r["boxes"], r["keypoints"])
                res.setdefault("kps_match_iou", {})[s] = d["match_iou"]
            del det; gc.collect(); torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        est, comps, timed, info = C.build(a.variant, precomputed_pose="split_pose" in a.modes)
        res["load"] = info
        for s, d in pre.items():
            img, boxes, K = d["img"], d["boxes"], d["K"]
            for mode in a.modes:
                hbs = "yolo_pose" if mode == "split_pose" else "body_decoder"
                for itype in a.types:
                    for k in a.ks:
                        if k > len(boxes):
                            continue

                        def call(idx, it=itype, md=mode, h=hbs):
                            if md == "split_pose":
                                comps["det"].set(boxes[idx], d["kps"][idx])
                                out, st = C.run_one(est, timed, img, cam_int=K, inference_type=it, hand_box_source=h)
                            else:
                                out, st = C.run_one(est, timed, img, bboxes=boxes[idx], cam_int=K, inference_type=it, hand_box_source=h)
                            return out, st

                        def batch_fn(kk=k):
                            out, st = call(list(range(kk)))
                            return {"total_ms": st["process_one_image_ms"], "hmr_ms": st.get("hmr_ms"), "n_out": len(out), "finite": C.finite(out)}
                        measure("hmr_batch", batch_fn, k, s, itype, mode)
                        if not a.no_seq and k > 1:
                            def seq_fn(kk=k):
                                tt = hh = 0.0; fin = True
                                for i in range(kk):
                                    out, st = call([i]); tt += st["process_one_image_ms"]; hh += st.get("hmr_ms") or 0; fin &= C.finite(out)
                                return {"total_ms": tt, "hmr_ms": hh, "finite": fin}
                            measure("hmr_seq", seq_fn, k, s, itype, mode)
            if not a.no_post:
                out, _ = C.run_one(est, timed, img, bboxes=boxes, cam_int=K, hand_box_source="body_decoder") if "split" in a.modes else (None, None)
                if out:
                    tmp = Path(tempfile.mkdtemp())

                    def render_fn():
                        t0 = time.perf_counter(); C.render(img, out, est.faces); return {"total_ms": (time.perf_counter() - t0) * 1000}

                    def mesh_fn():
                        t0 = time.perf_counter(); C.save_meshes(tmp, "b", out, est.faces); return {"total_ms": (time.perf_counter() - t0) * 1000}
                    measure("render_official_vis", render_fn, len(out), s, "-", "-")
                    measure("mesh_export_ply", mesh_fn, len(out), s, "-", "-")
        del est; comps.clear(); gc.collect(); torch.cuda.empty_cache()

        if a.e2e:
            torch.cuda.reset_peak_memory_stats()
            try:
                est, comps, timed, info = C.build(a.variant, detector="yolo_pose", detector_model=str(a.yolo or C.YOLO), fov="moge2")
                res["load_e2e"] = info
                for s, d in pre.items():
                    for itype in a.types:
                        def e2e_fn(it=itype, img=d["img"]):
                            out, st = C.run_one(est, timed, img, inference_type=it, bbox_thr=0.5, hand_box_source="yolo_pose")
                            return {"total_ms": st["process_one_image_ms"], "hmr_ms": st.get("hmr_ms"), "detect_ms": st.get("detect_ms"),
                                    "fov_ms": st.get("fov_ms"), "n_out": len(out), "finite": C.finite(out)}
                        measure("e2e_official", e2e_fn, len(d["boxes"]), s, itype, "official")
            except torch.cuda.OutOfMemoryError as e:
                rows.append({"condition": "e2e_official", "result": "OOM_AT_LOAD", "error": str(e)[:300], "load_fail": getattr(e, "load_info", None)})
                print(f"[{rid}] e2e OOM at load", flush=True)
    except Exception as e:  # noqa: BLE001
        res.update(error=f"{type(e).__name__}: {e}"[:600], traceback=traceback.format_exc()[-3000:]); print(res["traceback"])
    res.update(gs.stop())
    C.write_json(C.OUT / rid / "metrics.json", res)
    keys = sorted({k for r in rows for k in r if k not in ("traceback", "load_fail")},
                  key=lambda k: (k not in ("condition", "mode", "image", "n_people", "inference_type", "result"), k))
    with open(C.OUT / rid / "metrics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    print(f"[{rid}] done → {C.OUT / rid}/metrics.csv")


if __name__ == "__main__":
    main()
