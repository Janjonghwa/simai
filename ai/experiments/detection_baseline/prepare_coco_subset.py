"""COCO val2017 사람 keypoint 부분집합을 고르고 그 이미지만 받는다.

- 주석: recordings/coco/annotations/person_keypoints_val2017.json (annotations_trainval2017.zip 에서 이 파일만 푼다)
- 선택: 사람 주석이 1개 이상인 이미지 ID 를 정렬한 뒤 seed 로 섞어 앞 N개. ID 목록과 해시를 runs/coco_subset_<N>_s<seed>.json 에 남긴다
- 이미지: images.cocodataset.org 에서 개별로 받아 recordings/coco/val2017/ 에 둔다(커밋 안 함)

  python ai/experiments/detection_baseline/prepare_coco_subset.py --n 500 --seed 0
"""
from __future__ import annotations

import argparse
import json
import random
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import det_common as C


def select_ids(n: int, seed: int) -> list[int]:
    ann = json.loads(C.COCO_ANN.read_text(encoding="utf-8"))
    ids = sorted({a["image_id"] for a in ann["annotations"]})
    random.Random(seed).shuffle(ids)
    return sorted(ids[:n])


def fetch(img_id: int) -> None:
    dst = C.COCO_IMG / f"{img_id:012d}.jpg"
    if not dst.exists():
        urllib.request.urlretrieve(f"http://images.cocodataset.org/val2017/{img_id:012d}.jpg", dst)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    ids = select_ids(a.n, a.seed)
    C.COCO_IMG.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(fetch, ids))
    out = C.RUNS / f"coco_subset_{a.n}_s{a.seed}.json"
    C.write_json(out, {
        "source": "COCO val2017 person_keypoints",
        "annotation_sha256": C.sha256_file(C.COCO_ANN),
        "rule": "사람 주석이 1개 이상인 이미지 ID 정렬 → random.Random(seed).shuffle → 앞 n개 → 정렬",
        "n": a.n, "seed": a.seed,
        "image_ids_sha256": C.sha256_bytes(",".join(map(str, ids)).encode()),
        "image_ids": ids,
    })
    print(f"{len(ids)}장 준비 → {out}")


if __name__ == "__main__":
    main()
