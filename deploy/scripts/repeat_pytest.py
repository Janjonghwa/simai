#!/usr/bin/env python3
"""pytest 를 같은 인자로 N 번 돌려 간헐 실패를 센다 (GPU 1차 세션 큐 f 항목: S15P31A101-45·-175 TLS 시험을 Linux 에서 반복).

    python deploy/scripts/repeat_pytest.py --count 5 --cwd ai -- tests/services/tier0/test_i1_tls.py tests/services/tier0/test_serial_tls.py

회차마다 junit XML 을 $SIMAI_RUN_DIR(없으면 ./repeat-pytest) 에 남기고, 회차·실패 시험 이름을 metrics.json 에 쓴다.
pytest 캐시를 저장소 사본에 쓰지 않는다(-p no:cacheprovider). 종료 코드: 0 모든 회차 통과, 1 실패한 회차가 있음, 2 인자 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def failed_tests(xml_path: Path) -> list[str]:
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return ["<junit XML 없음>"]
    out = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            out.append(f"{case.get('classname')}::{case.get('name')}")
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # pytest 캡처 스트림에는 없다
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="pytest 반복 실행")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--cwd", type=Path, default=Path("."), help="pytest 를 돌릴 폴더 (ai/)")
    ap.add_argument("--timeout-s", type=float, default=900, help="회차 하나의 최대 시간")
    ap.add_argument("pytest_args", nargs="+", help="-- 뒤에 pytest 인자")
    args = ap.parse_args(argv)
    if args.count < 1:
        print("--count 는 1 이상", file=sys.stderr)
        return 2
    out = Path(os.environ.get("SIMAI_RUN_DIR") or "repeat-pytest").resolve()
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for i in range(1, args.count + 1):
        xml = out / f"junit-{i:02d}.xml"
        cmd = [sys.executable, "-m", "pytest", *args.pytest_args, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
        print(f"=== 회차 {i}/{args.count}: {' '.join(cmd)}", flush=True)
        t0 = time.monotonic()
        try:
            rc = subprocess.run(cmd, cwd=args.cwd, timeout=args.timeout_s).returncode
        except subprocess.TimeoutExpired:
            rc = None
        runs.append({"run": i, "exit": rc, "seconds": round(time.monotonic() - t0, 1),
                     "failed": [] if rc == 0 else failed_tests(xml)})
        print(f"=== 회차 {i} → exit {rc} ({runs[-1]['seconds']} s) {runs[-1]['failed'][:5]}", flush=True)
    bad = [r for r in runs if r["exit"] != 0]
    flaky = sorted({t for r in bad for t in r["failed"]})
    (out / "repeat.json").write_text(json.dumps({"count": args.count, "runs": runs, "failed_tests": flaky},
                                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metrics = {"runs": args.count, "failed_runs": len(bad), "failed_tests": ", ".join(flaky)[:500],
               "seconds_max": max(r["seconds"] for r in runs), "platform": sys.platform}
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"repeat_pytest: {args.count - len(bad)}/{args.count} 회차 통과" + (f", 실패 시험 {flaky}" if flaky else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
