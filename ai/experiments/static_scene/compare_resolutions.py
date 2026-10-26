"""MapAnything 입력 해상도 비교 (S15P31A101-282). 같은 입력을 해상도만 바꿔 돈 Run 들을 한 표로 모은다.

    uv run --project ai python ai/experiments/static_scene/compare_resolutions.py <Run 폴더> [<Run 폴더> ...] [--json out.json]

Run 폴더마다 run_mapanything.py 의 metrics.json 과(있으면) `world_frame.cli fix --out <Run>/world-fix` 기록을 읽는다.
보정용(바닥 rms·지지 점·카메라 간 재투영)과 배경용(모델 입력 크기·원본 한 점이 덮는 픽셀·유효 점 비율·융합 점 수)을 나눠 낸다.
판정은 하지 않는다(값만). 사람이 찍힌 입력이라 결과는 커밋하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def row(run_dir: Path, original_width: int | None = None) -> dict:
    """Run 폴더 하나 → 표 한 줄. 없는 값은 None(지어내지 않는다)."""
    m = _load(run_dir / "metrics.json") or {}
    fix = _load(run_dir / "world-fix")
    hw = m.get("model_input_hw")
    views = m.get("per_view") or []
    valid = [v.get("valid_ratio") for v in views if isinstance(v.get("valid_ratio"), (int, float))]
    out = {
        "run": run_dir.name,
        "result": m.get("result"),
        "resize": m.get("resize") or ({} if m else None),
        "model_input_hw": hw,
        "px_per_point": round(original_width / hw[1], 2) if original_width and hw else None,
        "infer_ms_warm": m.get("infer_ms_warm"),
        "peak_alloc_mb": m.get("peak_alloc_mb"),
        "valid_ratio_mean": round(statistics.fmean(valid), 4) if valid else None,
        "points": m.get("points_conf_filtered"),
        "fix_status": None, "floor_inliers": None, "floor_rms": None,
        "reproj_median_px": None, "reproj_p95_px": None,
    }
    if fix:
        floor = (fix.get("evidence") or {}).get("floor") or {}
        cams = ((fix.get("reprojection") or {}).get("cameras") or {}).values()
        cams = [c for c in cams if isinstance(c, dict)]  # 짝 점이 없는 카메라는 None 으로 기록된다(inputs.cross_view_reprojection)
        med = [c["median_px"] for c in cams if isinstance(c.get("median_px"), (int, float))]
        p95 = [c["p95_px"] for c in cams if isinstance(c.get("p95_px"), (int, float))]
        out.update(fix_status=f"{fix.get('status')} {fix.get('reason') or ''}".strip(), floor_inliers=floor.get("inliers"),
                   floor_rms=floor.get("rms_ma"), reproj_median_px=round(statistics.median(med), 2) if med else None,
                   reproj_p95_px=round(statistics.median(p95), 2) if p95 else None)
    return out


COLUMNS = (("run", "Run"), ("model_input_hw", "입력 H×W"), ("px_per_point", "원본 px/점"), ("infer_ms_warm", "추론 ms"),
           ("peak_alloc_mb", "VRAM MB"), ("valid_ratio_mean", "유효 비율"), ("points", "점 수"), ("fix_status", "World 고정"),
           ("floor_inliers", "바닥 지지점"), ("floor_rms", "바닥 rms"), ("reproj_median_px", "재투영 중앙 px"),
           ("reproj_p95_px", "재투영 p95 px"))


def table(rows: list[dict]) -> str:
    def cell(v):
        if v is None:
            return "-"
        if isinstance(v, list):
            return "×".join(str(x) for x in v)
        return str(v)
    lines = ["| " + " | ".join(h for _, h in COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(cell(r.get(k)) for k, _ in COLUMNS) + " |" for r in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--original-width", type=int, default=2304, help="원본(펴진) 그림 가로 px — 한 점이 덮는 원본 픽셀 계산")
    ap.add_argument("--json", type=Path, help="표를 JSON 으로도 쓴다(있으면 덮어쓰지 않음)")
    a = ap.parse_args(argv)
    missing = [str(r) for r in a.runs if not (r / "metrics.json").is_file()]
    if missing:
        print(f"compare_resolutions: metrics.json 없음: {missing}", file=sys.stderr)
        return 2
    rows = [row(r, a.original_width) for r in a.runs]
    print(table(rows))
    if a.json:
        with a.json.open("x", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
