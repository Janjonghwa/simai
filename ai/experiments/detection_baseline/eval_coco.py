"""YOLO26-pose 크기별 keypoint 정확도를 COCO val2017 부분집합으로 잰다 (G0 검출 기준선, S15P31A101-18).

공개 데이터(DATASET) 지표다. 교실 상단 시점·책상 가림 조건의 제품 정확도가 아니므로 G0 합격 판정에 쓰지 않고
크기 사이의 상대 비교에만 쓴다(교실 주석 GT 측정은 녹화 데이터가 생긴 뒤 따로 한다).

지표
- OKS AP: pycocotools COCOeval(iouType=keypoints), imgIds 를 부분집합으로 제한. 예측은 conf 0.001 까지 모두 넣는다
- PCK@0.1: 운영 임계값(사람 conf >= 0.25)을 넘은 예측을 GT 사람과 bbox IoU >= 0.5 로 탐욕 매칭한 뒤,
  보이는 GT 관절(v>0)마다 거리 <= 0.1 × max(GT bbox w, h) 이면 맞음. 매칭 안 된 GT 사람의 관절은 틀림으로 센다
- 관절 누락률: 매칭된 사람의 보이는 GT 관절 중 예측 관절 신뢰도 < 0.5 인 비율(하류에서 관절을 버리는 기준)
- 사람 재현율: 매칭된 GT 사람 / 관절 주석이 있는 비군중 GT 사람

  python ai/experiments/detection_baseline/eval_coco.py --run-id acc-4050-coco500
  python ai/experiments/detection_baseline/eval_coco.py --run-id acc-4050-coco500-trt --backend engine
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json

import numpy as np
import torch

import det_common as C

MODES = {"nms": True, "e2e": False}
JOINTS = ["nose", "l_eye", "r_eye", "l_ear", "r_ear", "l_shoulder", "r_shoulder", "l_elbow", "r_elbow",
          "l_wrist", "r_wrist", "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle"]
PERSON_CONF = 0.25  # ultralytics predict 기본 conf, Tier 0 운영 임계값의 출발점
KPT_CONF = 0.5
PCK_ALPHA = 0.1
MATCH_IOU = 0.5


def iou_xywh(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a: (N,4) xywh, b: (M,4) xywh → (N,M) IoU."""
    ax2, ay2 = a[:, 0] + a[:, 2], a[:, 1] + a[:, 3]
    bx2, by2 = b[:, 0] + b[:, 2], b[:, 1] + b[:, 3]
    iw = np.clip(np.minimum(ax2[:, None], bx2[None]) - np.maximum(a[:, None, 0], b[None, :, 0]), 0, None)
    ih = np.clip(np.minimum(ay2[:, None], by2[None]) - np.maximum(a[:, None, 1], b[None, :, 1]), 0, None)
    inter = iw * ih
    union = (a[:, 2] * a[:, 3])[:, None] + (b[:, 2] * b[:, 3])[None] - inter
    return inter / np.maximum(union, 1e-9)


def pck_stats(gts: dict[int, list[dict]], preds: dict[int, list[dict]], person_conf: float = PERSON_CONF) -> dict:
    """이미지별 GT·예측 목록으로 PCK@0.1, 관절 누락률, 사람 재현율을 센다."""
    hit = np.zeros(17)
    vis = np.zeros(17)
    vis_matched = np.zeros(17)
    low = np.zeros(17)
    n_gt = n_match = 0
    for img_id, g in gts.items():
        g = [x for x in g if not x["iscrowd"] and x["num_keypoints"] > 0]
        if not g:
            continue
        p = [x for x in preds.get(img_id, []) if x["score"] >= person_conf]
        gk = np.array([x["keypoints"] for x in g], dtype=float).reshape(len(g), 17, 3)
        gb = np.array([x["bbox"] for x in g], dtype=float)
        n_gt += len(g)
        vis += (gk[:, :, 2] > 0).sum(0)
        if not p:
            continue
        pk = np.array([x["keypoints"] for x in p], dtype=float).reshape(len(p), 17, 3)
        pb = np.array([x["bbox"] for x in p], dtype=float)
        iou = iou_xywh(gb, pb)
        used_g, used_p = set(), set()
        for gi, pi in sorted(np.ndindex(iou.shape), key=lambda ij: -iou[ij]):
            if iou[gi, pi] < MATCH_IOU:
                break
            if gi in used_g or pi in used_p:
                continue
            used_g.add(gi)
            used_p.add(pi)
            n_match += 1
            v = gk[gi, :, 2] > 0
            d = np.linalg.norm(pk[pi, :, :2] - gk[gi, :, :2], axis=1)
            thr = PCK_ALPHA * max(gb[gi, 2], gb[gi, 3])
            hit += v & (d <= thr)
            vis_matched += v
            low += v & (pk[pi, :, 2] < KPT_CONF)
    per_joint = {j: round(float(hit[i] / vis[i]), 4) for i, j in enumerate(JOINTS)}
    miss = {j: round(float(low[i] / vis_matched[i]), 4) for i, j in enumerate(JOINTS)}
    lower = [JOINTS.index(j) for j in ("l_knee", "r_knee", "l_ankle", "r_ankle")]
    return {
        "pck@0.1": round(float(hit.sum() / vis.sum()), 4),
        "pck@0.1_knee_ankle": round(float(hit[lower].sum() / vis[lower].sum()), 4),
        "pck@0.1_per_joint": per_joint,
        "kpt_lowconf_rate": round(float(low.sum() / vis_matched.sum()), 4),
        "kpt_lowconf_rate_knee_ankle": round(float(low[lower].sum() / vis_matched[lower].sum()), 4),
        "kpt_lowconf_rate_per_joint": miss,
        "person_recall": round(n_match / n_gt, 4),
        "gt_people": n_gt,
    }


def oks_ap(coco_gt, dets: list[dict], img_ids: list[int]) -> dict:
    from pycocotools.cocoeval import COCOeval

    if not dets:
        return {}
    with contextlib.redirect_stdout(io.StringIO()):
        dt = coco_gt.loadRes(dets)
        ev = COCOeval(coco_gt, dt, "keypoints")
        ev.params.imgIds = img_ids
        ev.evaluate()
        ev.accumulate()
        ev.summarize()
    names = ["AP", "AP50", "AP75", "AP_M", "AP_L", "AR", "AR50", "AR75", "AR_M", "AR_L"]
    return {f"oks_{k}": round(float(v), 4) for k, v in zip(names, ev.stats)}


def predict_all(model, img_ids: list[int], imgsz: int, nms: bool, quantize: int) -> list[dict]:
    dets = []
    for img_id in img_ids:
        r = model.predict(str(C.COCO_IMG / f"{img_id:012d}.jpg"), imgsz=imgsz, quantize=quantize, nms=nms,
                          conf=0.001, max_det=100, verbose=False)[0]
        if r.keypoints is None or len(r.boxes) == 0:
            continue
        xyxy = r.boxes.xyxy.cpu().numpy()
        conf = r.boxes.conf.cpu().numpy()
        kxy = r.keypoints.xy.cpu().numpy()
        kc = r.keypoints.conf.cpu().numpy() if r.keypoints.conf is not None else np.ones(kxy.shape[:2])
        for b, s, k, c in zip(xyxy, conf, kxy, kc):
            kp = np.concatenate([k, c[:, None]], axis=1).reshape(-1)
            dets.append({"image_id": img_id, "category_id": 1, "score": float(s),
                         "bbox": [float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])],
                         "keypoints": [round(float(x), 2) for x in kp]})
    return dets


def main() -> None:
    from pycocotools.coco import COCO

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--sizes", nargs="+", default=list(C.SIZES), choices=C.SIZES)
    ap.add_argument("--imgsz", type=int, nargs="+", default=[640])
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    ap.add_argument("--quantize", type=int, default=16, choices=[16, 32], help="16 = FP16 (pt). engine 은 늘 FP16")
    ap.add_argument("--backend", default="pt", choices=["pt", "engine"])
    ap.add_argument("--subset", default=str(C.RUNS / "coco_subset_500_s0.json"))
    a = ap.parse_args()

    subset = json.loads(open(a.subset, encoding="utf-8").read())
    img_ids = subset["image_ids"]
    with contextlib.redirect_stdout(io.StringIO()):
        coco_gt = COCO(str(C.COCO_ANN))
    gts = {i: coco_gt.loadAnns(coco_gt.getAnnIds(imgIds=[i], catIds=[1], iscrowd=None)) for i in img_ids}

    out_dir = C.RUNS / a.run_id
    res = {"run_id": a.run_id, "jira_key": "S15P31A101-18", "kind": "accuracy", "started_at": C.now_ms(),
           "git": C.git_info(), "env": C.env_info(),
           "input": {"source": "DATASET", "dataset_id": "coco/val2017/person_keypoints", "subset_file": a.subset.replace("\\", "/").split("/")[-1],
                     "n_images": len(img_ids), "image_ids_sha256": subset["image_ids_sha256"],
                     "annotation_sha256": subset["annotation_sha256"]},
           "config": {k: v for k, v in vars(a).items() if k != "run_id"} | {
               "person_conf": PERSON_CONF, "kpt_conf": KPT_CONF, "pck_alpha": PCK_ALPHA, "match_iou": MATCH_IOU},
           "weights": {}, "rows": []}
    for size in a.sizes:
        for mode in a.modes:
            for s in a.imgsz:
                model, w = C.load_backend(size, mode, s, a.backend)
                res["weights"][f"{size}_{mode}_{s}"] = w
                dets = predict_all(model, img_ids, s, MODES[mode], a.quantize)
                preds: dict[int, list[dict]] = {}
                for d in dets:
                    preds.setdefault(d["image_id"], []).append(d)
                row = {"size": size, "mode": mode, "imgsz": s, "n_dets": len(dets),
                       **oks_ap(coco_gt, dets, img_ids), **pck_stats(gts, preds)}
                C.write_json(out_dir / f"dets_{size}_{mode}_{s}.json", dets)
                res["rows"].append(row)
                print(f"[{a.run_id}] {size} {mode} imgsz={s} OKS-AP={row.get('oks_AP')} PCK@0.1={row['pck@0.1']} "
                      f"무릎·발목 PCK={row['pck@0.1_knee_ankle']} 누락={row['kpt_lowconf_rate']} 재현율={row['person_recall']}", flush=True)
                C.write_json(out_dir / "metrics.json", res)
                del model
                torch.cuda.empty_cache()
    res["finished_at"] = C.now_ms()
    res["status"] = "SUCCEEDED"
    C.write_json(out_dir / "metrics.json", res)
    print(f"[{a.run_id}] 완료 → {out_dir / 'metrics.json'}")


if __name__ == "__main__":
    main()
