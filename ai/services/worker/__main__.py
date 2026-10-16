"""작업 서버 진입점: `python -m services.worker --config <설정 파일>`.

설정을 읽고 기능 패키지를 조립하는 자리만 둔다. 조립은 후속 티켓에서 채운다.
"""

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.worker", description="I5 작업 서버")
    parser.add_argument("--config", type=Path, help="작업 서버 설정 파일 경로")
    parser.parse_args(argv)
    print("services.worker: 아직 조립하지 않았다 (골격)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
