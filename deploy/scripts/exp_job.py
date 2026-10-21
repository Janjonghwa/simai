#!/usr/bin/env python3
"""실험 Job 감싸기 (S15P31A101-207). 세션 큐 g1~g3·m1·m2 가 실험 스크립트를 이것으로 부른다.

    python deploy/scripts/exp_job.py --out <실험 결과 폴더> -- <실험 명령...>

1) 결과 폴더에 이미 metrics.json 이 있으면(끝난 결과) 덮어쓰지 않고 실행하지 않는다(종료 코드 4).
   같은 세션의 실패한 시도가 남긴 반쯤 된 폴더(metrics.json 없음)는 이어 쓴다.
2) 명령을 실행한다(표준 입출력은 그대로 Job 로그로).
3) 명령이 끝나면(성공·실패 모두) 결과 폴더의 metrics.json 을 Run 기록용 스칼라 지표로 줄여 $SIMAI_RUN_DIR/metrics.json 에 쓴다.
   gpu_queue 의 기록기(run_record)는 그 파일만 읽고 스칼라만 받는다. 원본 metrics.json 은 Run 기록 results 에 sha256 과 함께 걸린다.
종료 코드: 명령의 종료 코드, 4 결과가 이미 있음, 2 사용법 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

EXIT_EXISTS = 4


def summarize(obj, prefix: str = "") -> dict:
    """중첩 지표를 스칼라만 남긴 평평한 dict 로. 목록은 개수와 항목 필드별 요약(수치 중앙값·최댓값, 참 개수, result 의 SUCCESS 개수)."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, bool) or v is None or isinstance(v, (int, float, str)):
                out[key] = v
            elif isinstance(v, dict):
                out |= summarize(v, f"{key}.")
            elif isinstance(v, list):
                out[f"{key}.count"] = len(v)
                rows = [r for r in v if isinstance(r, dict)]
                fields = sorted({f for r in rows for f in r})
                for f in fields:
                    vals = [r[f] for r in rows if f in r]
                    nums = [x for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
                    if nums and len(nums) == len(vals):
                        out[f"{key}.{f}.p50"] = round(float(statistics.median(nums)), 4)
                        out[f"{key}.{f}.max"] = max(nums)
                    elif vals and all(isinstance(x, bool) for x in vals):
                        out[f"{key}.{f}.true"] = sum(vals)
                    elif f == "result":
                        out[f"{key}.result.success"] = sum(1 for x in vals if x == "SUCCESS")
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args:
        print("사용법: exp_job.py --out <폴더> -- <명령...>", file=sys.stderr)
        return 2
    i = args.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    opts = ap.parse_args(args[:i])
    cmd = args[i + 1:]
    if not cmd:
        print("사용법: exp_job.py --out <폴더> -- <명령...>", file=sys.stderr)
        return 2
    src = opts.out / "metrics.json"
    if src.exists():
        print(f"exp_job: {src} 가 이미 있다. 끝난 결과를 덮어쓰지 않는다(다른 세션 이름으로 돌린다)", file=sys.stderr, flush=True)
        return EXIT_EXISTS
    try:
        code = subprocess.run(cmd).returncode
    except OSError as e:
        print(f"exp_job: 명령을 실행하지 못함: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        code = 127
    run_dir = os.environ.get("SIMAI_RUN_DIR")
    if run_dir and src.is_file():
        try:
            metrics = summarize(json.loads(src.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"exp_job: {src} 를 읽지 못함: {e}", file=sys.stderr, flush=True)
        else:
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            (Path(run_dir) / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif run_dir:
        print(f"exp_job: 실험 지표 없음({src}) → Run 기록 metrics 비움", file=sys.stderr, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
