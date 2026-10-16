"""Tier 0 진입점: `python -m services.tier0 --config <설정 파일>`.

설정을 읽고 기능 패키지를 조립하는 자리만 둔다. 조립은 후속 티켓에서 채운다.
"""

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.tier0", description="Tier 0 실시간 파이프라인")
    parser.add_argument("--config", type=Path, help="Tier 0 설정 파일 경로")
    parser.parse_args(argv)
    print("services.tier0: 아직 조립하지 않았다 (골격)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
