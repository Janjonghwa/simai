"""시뮬레이터 실행 진입점. ai/ 에서 실행한다.

    uv run python -m sim.run --scenario basic --out sim/runs/basic
    uv run python -m sim.run --scenario path/to/custom.json --out <dir> --seed 7
    uv run python -m sim.run --list

출력 디렉터리는 없거나 비어 있어야 한다(덮어쓰지 않는다). sim/runs/ 는 커밋되지 않는다(.gitignore runs/).
"""

import argparse
import sys
from pathlib import Path

from sim import scenario
from sim.simulate import simulate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sim.run", description="결정적 가상 교실 시뮬레이터")
    parser.add_argument("--scenario", help="내장 시나리오 이름(basic, podium, crossing) 또는 시나리오 JSON 경로")
    parser.add_argument("--out", type=Path, help="출력 디렉터리 (없거나 비어 있어야 한다)")
    parser.add_argument("--seed", type=int, help="시나리오의 seed 를 덮어쓴다")
    parser.add_argument("--list", action="store_true", help="내장 시나리오 목록")
    args = parser.parse_args(argv)

    if args.list:
        for name in scenario.list_builtin():
            print(name)
        return 0
    if not args.scenario or not args.out:
        parser.error("--scenario 와 --out 이 필요하다")

    try:
        sc = scenario.load(args.scenario, seed=args.seed)
        files = simulate(sc)
    except (scenario.ScenarioError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2

    out: Path = args.out
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"오류: 출력 디렉터리가 비어 있지 않다: {out}", file=sys.stderr)
        return 2
    for rel, data in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    summary = f"카메라 {len(sc.cameras)}대, 사람 {len(sc.people)}명, {sc.duration_ms} ms"
    print(f"{sc.name}: {summary} -> {out} ({len(files)}개 파일)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
