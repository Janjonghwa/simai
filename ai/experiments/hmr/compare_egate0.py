# EXP-02 E-GATE-0 대조 판정 (S15P31A101-238). 기준은 docs/design/exp-images.md "E-GATE-0 대조 기준".
"""새 환경(이미지·GPU)의 HMR Run 을 E-GATE-0 Run 과 대조한다. 노트북에서 결과 묶음을 받은 뒤 돌린다(GPU·torch 불필요).

    python ai/experiments/hmr/compare_egate0.py --base <E-GATE-0 Run>/metrics.json --new <새 Run>/metrics.json

- 구조(그림별 result·finite·n_people, 적재 구성요소 OK, H6 people_detected)는 E-GATE-0 와 정확히 같아야 한다. GPU·판과 상관없다.
- 지연은 (GPU 이름, torch 판)별 기대값 표 EXPECTED 와 비교한다. 표에 없는 조합은 판정하지 않고 기록만(RECORD_ONLY).
  H1 지연은 '같은 그림 크기·같은 인원 수가 앞에서 이미 나온 그림'(warm)의 process_one_image_ms 중앙값이다.
  처음 보는 입력 모양·인원 수의 첫 실행은 커널 선택·적재가 섞여(검출 200~370 ms, 다인원 HMR 2.5~2.9 s) 비교에서 뺀다.
- 결과 JSON 을 표준 출력에 쓰고, 종료 코드 0 = PASS·PASS_STRUCTURE_ONLY, 1 = HOLD, 2 = 사용법·입력 오류.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

H100 = "NVIDIA H100 80GB HBM3"
# (GPU 이름, torch 판) → 기대값. 측정 Run 하나에서 나온 값이고 같은 조합의 다음 Run 이 쌓이면 갱신한다(덮어쓰지 말고 근거와 함께).
EXPECTED = {
    (H100, "2.5.1+cu124"): {"h1_warm_p50_ms": 330.84, "h6_p50_ms": {"640x1": 15.38, "640x4": 21.47, "1280x1": 18.40, "1280x4": 38.15},
                            "basis": "E-GATE-0 2026-10-16 AP-JP-1 볼륨 micromamba venv (H1_official_opt, H6_det_yolo26x)"},
    (H100, "2.7.1+cu128"): {"h1_warm_p50_ms": 219.73, "h6_p50_ms": {"640x1": 8.61, "640x4": 12.11, "1280x1": 10.26, "1280x4": 30.45},
                            "basis": "2차 GPU 세션 2026-10-22 AP-JP-1 이미지 exp02-fs3db sha256:da77b601 (H1·H6 _img_20261022-s2-exp02)"},
}
TOL_H1 = 0.25      # 같은 GPU·판에서 H1 warm 중앙값 ±25%
TOL_H6 = 0.20      # H6 줄별 p50 ±20%
TOL_PEAK = 0.10    # peak_alloc_mb 최대값 ±10% (GPU 와 거의 무관, E-GATE-0 기준)
MIN_WARM = 3       # warm 그림이 이보다 적으면 지연을 판정하지 않는다


def warm_rows(images: list[dict]) -> list[dict]:
    """같은 크기·같은 인원 수가 앞에서 이미 나온 그림(그 모양의 커널·버퍼가 준비된 뒤의 실행)."""
    seen, out = set(), []
    for r in images:
        key = (r.get("size"), r.get("n_people"))
        if key in seen and r.get("result") == "SUCCESS" and isinstance(r.get("process_one_image_ms"), (int, float)):
            out.append(r)
        seen.add(key)
    return out


def _expected(env: dict) -> dict | None:
    return EXPECTED.get((env.get("gpu"), env.get("torch")))


def _within(value: float, ref: float, tol: float) -> bool:
    return abs(value - ref) <= tol * ref


def _components(run: dict) -> list[dict] | None:
    load = (run.get("load") or {}).get("load")
    return load if isinstance(load, list) else None


def load_diffs(base: dict, new: dict) -> list[dict]:
    """적재 구성요소: 기준 Run 의 구성요소가 새 Run 에 하나씩 있고 모두 OK 여야 한다(빠짐·이름 바뀜·중복·실패는 차이).
    같은 입력·같은 구성의 재현만 대조한다. 보정 K 로 MoGe 를 띄우지 않는 Run(g4)은 구성이 달라 이 대조 대상이 아니다."""
    b, n = _components(base), _components(new)
    if not n:
        return [{"what": "적재 기록(load.load) 없음"}]
    out = [{"what": "적재 실패", "component": x.get("component"), "result": x.get("result")} for x in n if x.get("result") != "OK"]
    names = [x.get("component") for x in n]
    out += [{"what": "적재 구성요소 중복", "component": c} for c in sorted({c for c in names if names.count(c) > 1}, key=str)]
    want = [x.get("component") for x in b] if b else []
    if not want:
        out.append({"what": "기준 Run 에 적재 기록이 없어 구성요소를 대조할 수 없다"})
    out += [{"what": "적재 구성요소 빠짐", "component": c} for c in want if c not in names]
    out += [{"what": "기준에 없는 적재 구성요소", "component": c} for c in names if c not in want and want]
    return out


def judge_images(base: dict, new: dict) -> dict:
    """H0·H1(run_image.py metrics)."""
    out: dict = {"kind": "images", "structure": {"ok": True, "diffs": []}}
    b = {r["image"]: r for r in base.get("images", [])}
    n = {r["image"]: r for r in new.get("images", [])}
    diffs = out["structure"]["diffs"]
    if set(b) != set(n):
        diffs.append({"what": "그림 목록", "base_only": sorted(set(b) - set(n)), "new_only": sorted(set(n) - set(b))})
    for name in sorted(set(b) & set(n)):
        for key in ("result", "n_people", "finite"):
            if b[name].get(key) != n[name].get(key):
                diffs.append({"image": name, "key": key, "base": b[name].get(key), "new": n[name].get(key)})
        if n[name].get("result") != "SUCCESS" or n[name].get("finite") is not True:
            diffs.append({"image": name, "key": "new_not_success", "new": n[name].get("result")})
    if not n:
        diffs.append({"what": "새 Run 에 그림이 없다"})
    diffs += load_diffs(base, new)
    out["structure"]["ok"] = not diffs
    out["success"] = f"{sum(1 for r in n.values() if r.get('result') == 'SUCCESS')}/{len(n)}"
    out["n_people"] = [n[k].get("n_people") for k in sorted(n)]

    peaks_b = [r["peak_alloc_mb"] for r in b.values() if isinstance(r.get("peak_alloc_mb"), (int, float))]
    peaks_n = [r["peak_alloc_mb"] for r in n.values() if isinstance(r.get("peak_alloc_mb"), (int, float))]
    if peaks_b and peaks_n:
        out["peak_alloc"] = {"base_max": max(peaks_b), "new_max": max(peaks_n), "ok": _within(max(peaks_n), max(peaks_b), TOL_PEAK)}
    else:
        out["peak_alloc"] = {"ok": False, "why": "peak_alloc_mb 없음"}  # 모르면 통과로 두지 않는다

    env = new.get("env") or {}
    warm = warm_rows(new.get("images", []))
    lat: dict = {"gpu": env.get("gpu"), "torch": env.get("torch"), "warm_images": [r["image"] for r in warm]}
    if warm:
        lat["warm_p50_ms"] = round(statistics.median(r["process_one_image_ms"] for r in warm), 2)
    exp = _expected(env)
    if exp is None:
        lat.update(status="RECORD_ONLY", why="(GPU, torch) 조합의 기대값이 없다: 기록만")
    elif len(warm) < MIN_WARM:
        lat.update(status="RECORD_ONLY", why=f"warm 그림 {len(warm)}장 < {MIN_WARM}")
    else:
        ref = exp["h1_warm_p50_ms"]
        lat.update(expected_ms=ref, tolerance=TOL_H1, basis=exp["basis"],
                   status="PASS" if _within(lat["warm_p50_ms"], ref, TOL_H1) else "HOLD")
    out["latency"] = lat
    return out


def judge_det(base: dict, new: dict) -> dict:
    """H6(det_bench.py metrics)."""
    key = lambda r: f"{r.get('imgsz')}x{r.get('batch')}"  # noqa: E731
    b = {key(r): r for r in base.get("rows", [])}
    n = {key(r): r for r in new.get("rows", [])}
    diffs = []
    if set(b) != set(n):
        diffs.append({"what": "줄 목록", "base": sorted(b), "new": sorted(n)})
    for k in sorted(set(b) & set(n)):
        if b[k].get("people_detected") != n[k].get("people_detected"):
            diffs.append({"row": k, "key": "people_detected", "base": b[k].get("people_detected"), "new": n[k].get("people_detected")})
    if not n:
        diffs.append({"what": "새 Run 에 줄이 없다"})
    out: dict = {"kind": "det", "structure": {"ok": not diffs, "diffs": diffs}}
    env = new.get("env") or {}
    exp = _expected(env)
    lat: dict = {"gpu": env.get("gpu"), "torch": env.get("torch"), "rows": {}}
    if exp is None:
        lat.update(status="RECORD_ONLY", why="(GPU, torch) 조합의 기대값이 없다: 기록만")
        lat["rows"] = {k: {"p50_ms": (r.get("ms") or {}).get("p50")} for k, r in n.items()}
    else:
        ok = True
        for k, r in n.items():
            v, ref = (r.get("ms") or {}).get("p50"), exp["h6_p50_ms"].get(k)
            good = isinstance(v, (int, float)) and ref is not None and _within(v, ref, TOL_H6)
            ok &= good
            lat["rows"][k] = {"p50_ms": v, "expected_ms": ref, "ok": good}
        lat.update(status="PASS" if ok and n else "HOLD", tolerance=TOL_H6, basis=exp["basis"])
    out["latency"] = lat
    return out


def verdict(res: dict) -> str:
    if not res["structure"]["ok"] or not res.get("peak_alloc", {"ok": True})["ok"] or res["latency"]["status"] == "HOLD":
        return "HOLD"
    return "PASS" if res["latency"]["status"] == "PASS" else "PASS_STRUCTURE_ONLY"


def compare(base: dict, new: dict) -> dict:
    if "rows" in new and "images" not in new:
        res = judge_det(base, new)
    else:
        res = judge_images(base, new)
    res["verdict"] = verdict(res)
    return res


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", type=Path, required=True, help="E-GATE-0 Run 의 metrics.json")
    ap.add_argument("--new", type=Path, required=True, help="새 Run 의 metrics.json")
    a = ap.parse_args(argv)
    try:
        base = json.loads(a.base.read_text(encoding="utf-8"))
        new = json.loads(a.new.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"compare_egate0: 읽지 못함: {e}", file=sys.stderr)
        return 2
    res = compare(base, new)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if res["verdict"] == "HOLD" else 0


if __name__ == "__main__":
    sys.exit(main())
