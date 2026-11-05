#!/usr/bin/env python3
"""분석 워커를 Tier 0 뒤에 띄운다 (S15P31A101-183, -60 Tier 0 GPU 우선 보호의 deploy 쪽 순서). 이미지에는 simai-wait-tier0 로 들어간다.

Tier 0 진입점이 TensorRT 엔진을 준비하는 동안(첫 빌드는 수 분)은 표시 파일(state/tier0.live)이 아직 없다. 분석 워커 접수 문은
표시 파일이 없으면 '관제 없음'으로 보고 작업을 받으므로, 그 틈에 워커가 GPU 메모리를 먼저 잡을 수 있다(-60 MR 남은 일).
그래서 워커는 이 스크립트로 띄운다: 표시 파일의 gpu_priority 가 phase=running·reserve.state=OK 이고 15 초 안에 갱신된 것을
확인한 뒤에만 명령을 exec 한다. 모르면(파일 없음·깨짐·오래됨·기동 중·선확보 PARTIAL 등) 띄우지 않고 기다린다.

    simai-wait-tier0 [--marker 경로] [--timeout-s 1800] [--env-file /workspace/.simai-private/worker.env] -- python -m services.worker
    simai-wait-tier0 --no-tier0 -- python -m services.worker     # 관제가 없는 개발 시간: Tier 0 가 없을 때만(표시 파일이 있으면 거부)

--env-file 은 KEY=VALUE 줄(워커 토큰 등 비밀)을 읽어 exec 하는 명령의 환경에만 넣는다(출력하지 않는다).
종료 코드: exec 한 명령의 코드, 2 인자 오류, 3 시간 안에 Tier 0 가 준비되지 않음(띄우지 않음), 4 --no-tier0 인데 Tier 0 표시 파일이 있음.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

MAX_AGE_S = 15.0  # 표시 파일 갱신 5 s 의 3배 (simai_ai.gpu.admission 기본값과 같다)


def readiness(marker: Path, accept: set[str], max_age_s: float = MAX_AGE_S, now: float | None = None) -> tuple[bool, str]:
    """(준비됨, 이유). 표시 파일의 gpu_priority 칸(-60 marker_section) 형식을 읽는다."""
    try:
        st = marker.stat()
    except FileNotFoundError:
        return False, "표시 파일 없음(Tier 0 가 아직 엔진 준비 중이거나 떠 있지 않다)"
    except OSError as e:
        return False, f"표시 파일 확인 실패: {e}"
    age = (now if now is not None else time.time()) - st.st_mtime
    if abs(age) > max_age_s:
        return False, f"표시 파일이 {age:.0f}s 전 것(상한 {max_age_s:.0f}s)"
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"표시 파일을 읽지 못함: {type(e).__name__}"
    gp = doc.get("gpu_priority") if isinstance(doc, dict) else None
    if not isinstance(gp, dict):
        return False, "gpu_priority 칸 없음(-60 이전 Tier 0)"
    if gp.get("phase") != "running":
        return False, f"phase={gp.get('phase')!r} (running 이어야)"
    state = (gp.get("reserve") or {}).get("state")
    if state not in accept:
        return False, f"reserve.state={state!r} (허용 {sorted(accept)})"
    return True, f"phase=running, reserve.state={state}"


def load_env_file(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 0 준비(phase=running·reserve OK)를 확인한 뒤 명령을 띄운다")
    root = Path(os.environ.get("SIMAI_ROOT") or "/workspace/simai")
    ap.add_argument("--marker", type=Path, default=Path(os.environ.get("SIMAI_TIER0_MARKER") or root / "state" / "tier0.live"))
    ap.add_argument("--timeout-s", type=float, default=1800, help="이 시간 안에 준비되지 않으면 띄우지 않고 끝낸다")
    ap.add_argument("--poll-s", type=float, default=2.0)
    ap.add_argument("--accept-reserve", default="OK", help="허용하는 reserve.state (쉼표). 기본 OK")
    ap.add_argument("--no-tier0", action="store_true", help="Tier 0 가 없는 개발 시간: 표시 파일이 없을 때만 띄운다")
    ap.add_argument("--env-file", type=Path, default=None, help="명령 환경에 넣을 KEY=VALUE 파일(비밀, 출력하지 않음)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- 뒤에 띄울 명령")
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        print("simai-wait-tier0: -- 뒤에 띄울 명령이 필요하다", file=sys.stderr)
        return 2
    env = dict(os.environ)
    if args.env_file is not None:
        try:
            env |= load_env_file(args.env_file)
        except OSError as e:
            print(f"simai-wait-tier0: --env-file 을 읽지 못함 ({type(e).__name__})", file=sys.stderr)
            return 2

    validated = os.environ.get("SIMAI_VALIDATED_WORKER_FRACTION")
    if validated is not None:
        try:
            if float(env.get("SIMAI_WORKER_TORCH_MEMORY_FRACTION", "")) != float(validated):
                raise ValueError
        except ValueError:
            print("simai-wait-tier0: 워커 GPU 상한이 기동 시 검증한 값과 다르다. 기동기를 다시 시작한다", file=sys.stderr)
            return 2

    if args.no_tier0:
        if args.marker.exists():
            print(f"simai-wait-tier0: --no-tier0 인데 Tier 0 표시 파일이 있다({args.marker}). 띄우지 않는다", file=sys.stderr)
            return 4
    else:
        accept = {s.strip() for s in args.accept_reserve.split(",") if s.strip()}
        deadline = time.monotonic() + args.timeout_s
        last = ""
        while True:
            ok, why = readiness(args.marker, accept)
            if ok:
                print(f"simai-wait-tier0: Tier 0 준비됨 ({why}) → {cmd[0]} 시작", file=sys.stderr, flush=True)
                break
            if why != last:
                print(f"simai-wait-tier0: 기다림: {why}", file=sys.stderr, flush=True)
                last = why
            if time.monotonic() >= deadline:
                print(f"simai-wait-tier0: {args.timeout_s:.0f}s 안에 Tier 0 가 준비되지 않아 띄우지 않는다", file=sys.stderr)
                return 3
            time.sleep(args.poll_s)
    try:
        if os.name == "nt":  # Windows 의 exec 는 새 프로세스를 띄우고 바로 끝나 종료 코드·출력을 잇지 못한다(노트북 시험용)
            import subprocess

            return subprocess.call(cmd, env=env)
        os.execvpe(cmd[0], cmd, env)
    except OSError as e:
        print(f"simai-wait-tier0: {cmd[0]} 를 실행하지 못함: {e}", file=sys.stderr)
        return 2
    return 0  # pragma: no cover - exec 뒤에는 오지 않는다


if __name__ == "__main__":
    sys.exit(main())
