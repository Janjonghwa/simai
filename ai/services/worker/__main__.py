"""분석 워커 진입점: `python -m services.worker [--config <설정 파일>] [--once]`.

접속 정보는 환경 변수(.env)로 받는다. 토큰 값은 저장소에 두지 않는다.
- SIMAI_EC2_BASE_URL: EC2 주소 (https)
- SIMAI_WORKER_TOKEN: 워커 토큰 (Authorization: Bearer)
- SIMAI_WORKER_ID: 워커 ID, 봉투 source_id (기본 worker-01)

작업 유형별 처리기 조립은 후속 티켓에서 채운다. 처리기가 없으면 시작하지 않는다.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .client import AuthError, LeaseClient, RequestRejected
from .handlers import JobHandler
from .runner import HandlerStuck, Worker
from .transport import HttpTransport


def build_handlers(config: Path | None) -> Mapping[str, JobHandler]:
    """작업 유형 → 처리기. VLM·정적 3D·HMR 처리기는 각 작업 티켓에서 기능 패키지를 조립해 넣는다."""
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.worker", description="I5 분석 워커 (EC2 에서 작업을 가져간다)")
    parser.add_argument("--config", type=Path, help="분석 워커 설정 파일 경로")
    parser.add_argument("--once", action="store_true", help="대여 한 번만 처리하고 끝낸다")
    parser.add_argument("--wait-ms", type=int, default=25_000, help="long-poll 대기 (0~30000)")
    args = parser.parse_args(argv)
    if not 0 <= args.wait_ms <= 30_000:
        parser.error("--wait-ms 는 0~30000 이다 (계약 job_lease_request)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    handlers = build_handlers(args.config)
    if not handlers:
        print("services.worker: 등록된 작업 처리기가 없다 (처리기 조립은 후속 티켓)", file=sys.stderr)
        return 1
    base_url = os.environ.get("SIMAI_EC2_BASE_URL", "")
    token = os.environ.get("SIMAI_WORKER_TOKEN", "")
    worker_id = os.environ.get("SIMAI_WORKER_ID", "worker-01")
    if not base_url or not token:
        print("services.worker: SIMAI_EC2_BASE_URL·SIMAI_WORKER_TOKEN 이 필요하다 (.env)", file=sys.stderr)
        return 2

    stop = threading.Event()
    client = LeaseClient(
        HttpTransport(base_url, token),
        worker_id,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        monotonic=time.monotonic,
        sleep=stop.wait,
    )
    worker = Worker(client, handlers, clock_ms=lambda: time.time_ns() // 1_000_000, monotonic=time.monotonic, wait_ms=args.wait_ms)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        if args.once:
            worker.run_once(should_stop=stop.is_set)
        else:
            worker.run(stop)
    except (AuthError, RequestRejected) as e:
        print(f"services.worker: {e}", file=sys.stderr)
        return 2
    except HandlerStuck as e:  # 멈추지 않는 처리기 스레드는 프로세스를 다시 띄워야 정리된다
        print(f"services.worker: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
