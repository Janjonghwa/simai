#!/usr/bin/env python3
"""보정 K 입력 경로 판정 (세션 큐 g4, S15P31A101-243). run_image.py --calibration 결과 metrics.json 을 본다.

    python check_calib_k.py --images 20 --provenance '<Job 기록 input_provenance JSON>' <결과 폴더>/metrics.json

기대(docs/design/exp-images.md "3차 세션 항목"): 모든 그림의 K 출처 CALIBRATION·펴짐(UNDISTORTED_ALPHA1), MoGe 미적재,
모델 초점 − K_used fx < 0.5 px, 재투영 0(모델 자기 투영), finite. 사람이 없는 그림(NO_PERSON)은 초점 비교가 없어도 통과다(사람 수는 기록만).
metrics 의 k_input.input_provenance 는 Job 기록(run_record)의 input_provenance 와 같아야 한다(큐 시작 때 같은 파일에서 만든 값).
종료 코드: 0 통과, 1 어긋남, 2 사용법·읽기 오류.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

FOCAL_TOL_PX = 0.5
REPROJ_TOL_PX = 0.01  # run_image 가 소수 4자리로 반올림한다. 같은 초점·주점으로 투영하므로 0 이어야 한다
OK_RESULTS = ("SUCCESS", "NO_PERSON")


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def check(metrics: dict, n_images: int, provenance: dict | None) -> list[str]:
    """어긋난 점 목록(비면 통과). 모르는 값(필드 없음)은 통과로 보지 않는다."""
    bad: list[str] = []
    rows = metrics.get("images")
    if not isinstance(rows, list) or len(rows) != n_images:
        return [f"그림 {len(rows) if isinstance(rows, list) else '없음'}장 ≠ 기대 {n_images}장"]
    load = metrics.get("load") or {}
    comps = [c.get("component", "") for c in load.get("load") or [] if isinstance(c, dict)]
    if not comps:
        bad.append("적재 기록 없음")
    if load.get("fov") is not None or any(str(c).startswith("fov_") for c in comps):
        bad.append(f"MoGe(FOV) 가 적재됨: fov={load.get('fov')}, {comps}")
    for r in rows:
        name = r.get("image", "?")
        kin = r.get("k_input") or {}
        if kin.get("source") != "CALIBRATION":
            bad.append(f"{name}: k_input.source {kin.get('source')}")
        if kin.get("undistorted") is not True or kin.get("image_space") != "UNDISTORTED_ALPHA1":
            bad.append(f"{name}: 펴짐 {kin.get('undistorted')}, image_space {kin.get('image_space')}")
        if r.get("result") not in OK_RESULTS:
            bad.append(f"{name}: result {r.get('result')} {r.get('error', '')}"[:200])
        if r.get("finite") is not True:
            bad.append(f"{name}: finite {r.get('finite')}")
        people = r.get("result") == "SUCCESS"
        diff = r.get("k_focal_diff_px")
        if people and not (_num(diff) and diff < FOCAL_TOL_PX):
            bad.append(f"{name}: k_focal_diff_px {diff} (기대 < {FOCAL_TOL_PX})")
        reproj = r.get("reproj_err_px")
        if people and not (isinstance(reproj, list) and reproj and all(_num(e) and e <= REPROJ_TOL_PX for e in reproj)):
            bad.append(f"{name}: reproj_err_px {reproj}")
    top = metrics.get("k_input") or {}
    if top.get("count") != {"CALIBRATION": n_images}:
        bad.append(f"k_input.count {top.get('count')}")
    got = top.get("input_provenance")
    want = {k: (provenance or {}).get(k) for k in ("calibration", "is_upper_bound")}
    if not want["calibration"]:
        bad.append("Job 기록 input_provenance 에 calibration 이 없다(큐 시작 때 보정 파일을 읽지 못함)")
    elif got != want:
        bad.append(f"k_input.input_provenance {got} ≠ Job 기록 {want}")
    return bad


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="보정 K 입력 경로 판정 (g4)")
    ap.add_argument("metrics", type=Path)
    ap.add_argument("--images", type=int, required=True, help="기대 그림 수")
    ap.add_argument("--provenance", required=True, help="Job 기록 input_provenance (JSON)")
    a = ap.parse_args(argv)
    try:
        metrics = json.loads(a.metrics.read_text(encoding="utf-8"))
        provenance = json.loads(a.provenance)
    except (OSError, ValueError) as e:
        print(f"check_calib_k: 읽지 못함: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    bad = check(metrics, a.images, provenance)
    for b in bad:
        print(f"check_calib_k: FAIL {b}")
    print(f"check_calib_k: {'PASS' if not bad else 'FAIL'} ({a.images}장, 어긋남 {len(bad)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
