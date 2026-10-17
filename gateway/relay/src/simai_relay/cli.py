"""명령줄: run (카메라별 수신 + I0 헤더, 전송 없이 측정만).

    uv run simai-relay run --config configs/dry-run.toml --seconds 30
    uv run simai-relay run --config configs/my-cameras.toml          # Ctrl+C 로 멈춤

RunPod 전송(TLS)은 S15P31A101-83 이 붙인다. 지금은 CountingSink 로 개수만 세고 카메라별 상태·측정을 주기적으로 로그에 쓴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from simai_recorder.clock import SystemClock

from simai_relay import __version__
from simai_relay.config import DEFAULT_ENV_FILE, ConfigError, RedactSecrets, load_cameras, load_env, secret_values
from simai_relay.relay import RelaySession
from simai_relay.sink import CountingSink

log = logging.getLogger("simai_relay")


def _setup_logging(secrets: list[str], verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactSecrets(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _report(session: RelaySession) -> None:
    for entry, snap in zip(session.heartbeat_cameras(), session.snapshots(), strict=True):
        log.info("상태 %s", json.dumps({**snap, "heartbeat": entry}, ensure_ascii=False))


def cmd_run(args: argparse.Namespace) -> int:
    clock = SystemClock()
    env = load_env(Path(args.env_file))
    _setup_logging(secret_values(env), args.verbose)
    try:
        cameras = load_cameras(Path(args.config), clock, env)
    except (ConfigError, KeyError, TypeError, ValueError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    session = RelaySession(cameras, CountingSink, clock)
    log.info("중계 시작 (simai-relay %s, 카메라 %d대, %s, 전송 없음: 측정만)", __version__, len(cameras),
             "Ctrl+C 로 멈춤" if args.seconds is None else f"{args.seconds:.0f}초")
    session.start(args.seconds)
    try:
        while not session.join(timeout=args.report_interval):
            _report(session)
    except KeyboardInterrupt:
        session.stop()
        session.join()
    _report(session)
    errors = session.errors()
    if errors:
        log.error("오류로 멈춘 카메라: %s", errors)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simai-relay", description="simai 노트북 영상 중계 (I0 촬영 시각 헤더)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="카메라별 수신과 I0 헤더 붙이기 (전송은 S15P31A101-83)")
    run.add_argument("--config", required=True, help="[capture_time]·[[cameras]] TOML (configs/example.toml)")
    run.add_argument("--seconds", type=float, default=None, help="이 시간 뒤 멈춘다 (없으면 Ctrl+C 까지)")
    run.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="카메라 계정 .env (기본: 저장소 루트 .env)")
    run.add_argument("--report-interval", type=float, default=5.0, help="상태 로그 간격(초)")
    run.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    return 2
