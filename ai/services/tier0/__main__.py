"""Tier 0 진입점: `python -m services.tier0 --config <설정 파일>` (ai/ 에서, 이미지는 deploy/tier0/entrypoint.sh).

영상 수신(또는 SIM 입력)부터 검출 → 카메라별 추적 → 카메라 간 연결 → World Track → 접지 → Risk → 생명주기 → 현장 경보 →
저널·outbox → I1 까지 한 프로세스로 돌린다. 조립은 service.Tier0Service, 설정은 settings, 구조는 docs/design/tier0-service.md.

종료 코드: 0 정상 종료(종료 신호, file 입력은 녹화를 끝까지 재생함), 1 종료 정리 미완료, 2 설정 오류, 3 재시작 복구 거부(운영 절차는
docs/design/tier0-recovery.md), 4 입력·검출기를 열 수 없음(file 입력은 녹화를 끝까지 재생하지 못함 포함), 5 file 입력 결과(JSONL) 기록 실패. 종료 신호(SIGTERM·SIGINT, Windows 는 CTRL_BREAK 도)를 받으면
저널을 비우고 스레드를 정리한 뒤 끝낸다.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="services.tier0", description="Tier 0 실시간 파이프라인 (SIM·LIVE 입력 → 판정 → 현장 경보·I1)"
    )
    parser.add_argument("--config", type=Path, help="Tier 0 설정 파일(JSON) 경로. 환경 변수는 services/tier0/settings.py")
    parser.add_argument("--log-level", default="INFO", help="로그 수준 (기본 INFO)")
    parser.add_argument("--run-seconds", type=float, default=None, help="이 시간이 지나면 정상 종료한다(점검·시험용)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s"
    )

    from services.tier0.service import Tier0Service, Tier0StartError
    from services.tier0.settings import Tier0ConfigError, load_settings

    try:
        settings = load_settings(args.config)
    except Tier0ConfigError as e:
        print(f"services.tier0: 설정 오류: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        logging.getLogger("services.tier0").info("종료 신호 %d", signum)
        stop.set()

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)  # Windows: CTRL_BREAK_EVENT
    for sig in signals:
        signal.signal(sig, on_signal)

    service = Tier0Service(settings)
    try:
        service.start()
    except Tier0StartError as e:
        print(f"services.tier0: 시작하지 않았다: {e}", file=sys.stderr)
        return e.exit_code
    print(f"services.tier0: 시작 ({settings.gateway_id}, 입력 {settings.input_mode})", file=sys.stderr, flush=True)
    deadline = None if args.run_seconds is None else time.monotonic() + args.run_seconds
    try:
        while not stop.is_set() and not service.finished.is_set():  # finished: file 입력이 녹화를 끝까지 재생함
            left = 0.5 if deadline is None else min(0.5, deadline - time.monotonic())
            if left <= 0:
                break
            stop.wait(left)
    except KeyboardInterrupt:
        pass
    failed = None if service.file_input is None else service.file_input.failed
    if service.finished.is_set() and service.file_input is not None and failed is None:
        jsonl = service.jsonl
        lines = "" if jsonl is None else f", world_state {jsonl.world_state_lines}줄·risk_event {jsonl.risk_event_lines}줄"
        print(f"services.tier0: 녹화 재생 끝 ({service.file_input.replay.delivered}건{lines})", file=sys.stderr, flush=True)
    ok = service.stop()
    print(f"services.tier0: 종료 ({'정상' if ok else '정리 미완료'})", file=sys.stderr, flush=True)
    if failed is not None:
        print(f"services.tier0: 녹화 재생을 끝내지 못했다: {failed}", file=sys.stderr, flush=True)
        return 5 if service.output_failed is not None else 4
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
