"""모델 출력 K 검사 (S15P31A101-312). 출력 K 를 기대 K(넘긴 원본 K × 해상도 비, 자르기 반영)와 비교한다.

MapAnything(map-anything@3d10cf7)은 넘긴 K 를 광선으로 바꿔 조건으로만 쓰고, 출력 K 는 모델이 예측한 광선에서 다시 구한다.
학습 해상도(긴 변 518) 밖에서는 이 예측이 세로로 틀어진다(4차 세션 r1036·r1554·rfull fy −30~−40%, 설계
docs/design/mapanything-input-resolution.md "4차 세션 비교 무효"). Run 끝에 이 검사를 metrics 에 남겨 그런 Run 을 보정·비교에
쓰지 않게 한다. 실패로 끝내지 않고 경고로 남긴다(Run 자체는 비교 근거다).

같은 폴더 스크립트가 `import intrinsics_check` 로 쓴다(스크립트를 직접 실행하면 이 폴더가 sys.path 첫 항목). 표준 라이브러리와
numpy 만 쓴다(DA3·WorldMirror·Pi3X 전용 가상환경에서도 돈다).

    # 이미 가져온 MapAnything Run 을 노트북에서 다시 본다(torch 없음)
    uv run --project ai python ai/experiments/static_scene/intrinsics_check.py <Run 폴더> --prepared <prepare 출력 폴더>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# 허용 상대 오차. 근거(설계 문서): 정상 Run(r518 교실·로비 8대) 최대 2.1%, DA3 자세 조건 0.3% 이하·자세 없이 2.2%,
# 틀어진 Run(r1036·r1554·rfull 16대) fy −29.7~−40.5%. 5% 는 정상의 2배 넘게 여유, 틀어진 최소의 1/6.
TOL_REL = 0.05
FIELDS = (("fx", 0, 0), ("fy", 1, 1), ("cx", 0, 2), ("cy", 1, 2))


def expected_crop_resize_k(K, src_wh, dst_wh) -> np.ndarray:
    """map-anything `crop_resize_if_necessary` 규칙: 덮도록 한 배율(s = max(w/W, h/H))로 줄이고 가운데를 자른다.
    픽셀 중심 정수 규약(x' = (x + 0.5)·s − 0.5)에서 자른 만큼 주점을 옮긴다. 반 픽셀 반올림 차이는 무시한다(허용치의 1/100 미만)."""
    (W, H), (w, h) = src_wh, dst_wh
    s = max(w / W, h / H)
    out = np.asarray(K, float).copy()
    out[0, 0] *= s
    out[1, 1] *= s
    out[0, 2] = (out[0, 2] + 0.5) * s - 0.5 - (W * s - w) / 2
    out[1, 2] = (out[1, 2] + 0.5) * s - 0.5 - (H * s - h) / 2
    return out


def expected_axis_resize_k(K, src_wh, dst_wh) -> np.ndarray:
    """축마다 따로 늘이는 규칙(DA3·WorldMirror·Pi3X 처리 해상도: 14 배수로 맞추며 가로·세로 배율이 조금 다르다, 자르기 없음)."""
    (W, H), (w, h) = src_wh, dst_wh
    sx, sy = w / W, h / H
    out = np.asarray(K, float).copy()
    out[0, 0] *= sx
    out[1, 1] *= sy
    out[0, 2] = (out[0, 2] + 0.5) * sx - 0.5
    out[1, 2] = (out[1, 2] + 0.5) * sy - 0.5
    return out


def rel_errors(K_got, K_exp) -> dict[str, float]:
    """fx·fy·cx·cy 상대 오차((얻은 − 기대) / 기대, 부호 있음)."""
    g, e = np.asarray(K_got, float), np.asarray(K_exp, float)
    return {k: round(float((g[i, j] - e[i, j]) / e[i, j]), 5) for k, i, j in FIELDS}


def check(views: list[dict], tol: float = TOL_REL) -> dict:
    """views: [{name, expected, output, input?}]. input 은 라이브러리가 모델에 넘긴 K(있으면 전처리도 같은 허용치로 본다).

    status: OK, MISMATCH(출력 또는 입력 K 가 허용치 밖), SKIPPED(비교할 K 없음). 모르면 OK 로 채우지 않는다."""
    if not views:
        return {"status": "SKIPPED", "reason": "넘긴 K 없음(images 모드)", "tol_rel": tol}
    rows, worst = [], (0.0, None)
    for v in views:
        out = rel_errors(v["output"], v["expected"])
        Ko = np.asarray(v["output"], float)
        row = {"name": v["name"], **{f"{k}_rel": e for k, e in out.items()},
               "fy_over_fx": round(float(Ko[1, 1] / Ko[0, 0]), 4), "max_rel": max(abs(e) for e in out.values())}
        for k, e in out.items():
            if abs(e) > worst[0]:
                worst = (abs(e), f"{v['name']} output {k}")
        if v.get("input") is not None:
            inp = rel_errors(v["input"], v["expected"])
            row["input_max_rel"] = max(abs(e) for e in inp.values())
            for k, e in inp.items():
                if abs(e) > worst[0]:
                    worst = (abs(e), f"{v['name']} input {k}")
        rows.append(row)
    return {"status": "MISMATCH" if worst[0] > tol else "OK", "tol_rel": tol, "max_rel_err": round(worst[0], 5),
            "worst": worst[1], "views": rows}


def warning(rec: dict) -> str | None:
    """MISMATCH 면 metrics `warnings` 에 넣을 한 줄."""
    if rec.get("status") != "MISMATCH":
        return None
    return (f"출력 K 가 기대 K 와 {rec['max_rel_err']:.1%} 다르다({rec['worst']}, 허용 {rec['tol_rel']:.0%}). "
            "모델 예측 K 가 틀어진 Run 이라 그 K·깊이·자세를 보정·World 고정·비교 근거로 쓰지 않는다(S15P31A101-312)")


def check_mapanything_run(run_dir: Path, prepared_dir: Path, tol: float = TOL_REL) -> dict:
    """가져온 MapAnything Run(raw.npz)과 prepared.json 으로 다시 검사한다(입력 K 는 원본, 모델 입력 크기는 깊이 크기)."""
    doc = json.loads((Path(prepared_dir) / "prepared.json").read_text(encoding="utf-8"))
    raw = np.load(Path(run_dir) / "raw.npz")
    views = []
    for v in doc["views"]:
        key = f"{v['name']}__intrinsics"
        if key not in raw:
            continue
        h, w = raw[f"{v['name']}__depth_z"].shape[:2]
        exp = expected_crop_resize_k(v["K_undistorted"], tuple(v["image_size"]), (w, h))
        views.append({"name": v["name"], "expected": exp, "output": raw[key]})
    return check(views, tol)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path, help="MapAnything Run 폴더(raw.npz)")
    ap.add_argument("--prepared", required=True, type=Path, help="그 Run 의 prepare 출력 폴더(prepared.json)")
    ap.add_argument("--tol", type=float, default=TOL_REL)
    a = ap.parse_args(argv)
    bad = 0
    for run in a.runs:
        rec = check_mapanything_run(run, a.prepared, a.tol)
        bad += rec["status"] == "MISMATCH"
        print(f"{run.name}: {rec['status']} 최대 {rec.get('max_rel_err', 0):.1%} ({rec.get('worst')})")
        for r in rec.get("views", []):
            print(f"  {r['name']} fx {r['fx_rel']:+.1%} fy {r['fy_rel']:+.1%} cx {r['cx_rel']:+.1%} cy {r['cy_rel']:+.1%}"
                  f" fy/fx {r['fy_over_fx']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
