"""정확도 Run 의 저장된 예측(dets_*.json)으로 사람 conf 임계값별 재현율·PCK·정밀도 근사를 다시 센다(GPU 불필요).

NMS-free(e2e) 경로가 같은 conf 0.25 에서 사람을 덜 남기는 것이 점수 보정 차이인지 보려는 용도다.
정밀도 근사 = 매칭된 예측 / conf 이상 예측(관절 주석 없는 GT 사람과 겹친 예측도 틀림으로 세므로 실제보다 낮다).

  python ai/experiments/detection_baseline/conf_sweep.py --run-id acc-4050-coco500-pt-<커밋>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json

import det_common as C
import eval_coco as E


def main() -> None:
    from pycocotools.coco import COCO

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--confs", type=float, nargs="+", default=[0.1, 0.15, 0.2, 0.25, 0.3, 0.4])
    a = ap.parse_args()

    run = C.RUNS / a.run_id
    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    subset = json.loads((C.RUNS / m["input"]["subset_file"]).read_text(encoding="utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        coco_gt = COCO(str(C.COCO_ANN))
    gts = {i: coco_gt.loadAnns(coco_gt.getAnnIds(imgIds=[i], catIds=[1], iscrowd=None)) for i in subset["image_ids"]}
    rows = []
    for r in m["rows"]:
        dets = json.loads((run / f"dets_{r['size']}_{r['mode']}_{r['imgsz']}.json").read_text(encoding="utf-8"))
        preds: dict[int, list[dict]] = {}
        for d in dets:
            preds.setdefault(d["image_id"], []).append(d)
        for conf in a.confs:
            s = E.pck_stats(gts, preds, person_conf=conf)
            n_pred = sum(1 for d in dets if d["score"] >= conf)
            n_match = round(s["person_recall"] * s["gt_people"])
            rows.append({"size": r["size"], "mode": r["mode"], "imgsz": r["imgsz"], "person_conf": conf,
                         "person_recall": s["person_recall"], "pck@0.1": s["pck@0.1"],
                         "precision_approx": round(n_match / n_pred, 4) if n_pred else None, "n_pred": n_pred})
    C.write_json(run / "conf_sweep.json", {"run_id": a.run_id, "source_commit": m["git"]["commit"], "rows": rows})
    print("| 크기 | 경로 | conf | 사람 재현율 | PCK@0.1 | 정밀도 근사 | 예측 수 |\n|---|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['size']} | {x['mode']} | {x['person_conf']} | {x['person_recall']} | {x['pck@0.1']} | {x['precision_approx']} | {x['n_pred']} |")


if __name__ == "__main__":
    main()
