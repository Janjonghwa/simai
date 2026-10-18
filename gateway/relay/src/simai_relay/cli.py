"""명령줄: run (카메라별 수신 + I0 헤더, --send 면 카메라별 TLS 로 Tier 0 에 전송), bench-transport (G0 전송 비교 측정).

    uv run simai-relay run --config configs/dry-run.toml --seconds 30             # 전송 없이 측정만
    uv run simai-relay run --config configs/my-cameras.toml --send                # Tier 0 로 전송, Ctrl+C 로 멈춤
    uv run simai-relay bench-transport --seconds 20                               # 카메라별 연결 vs 단일 연결 다중화

--send 는 .env 의 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN·SIMAI_TIER0_PINNED_SPKI_SHA256 로 Tier 0 엔드포인트를 조회하고
카메라마다 TLS 연결을 연다(tls_sink). 없으면 CountingSink 로 개수만 센다. 카메라별 상태·측정을 주기적으로 로그에 쓴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from simai_recorder.clock import SystemClock

from simai_relay import __version__
from simai_relay.config import (
    DEFAULT_ENV_FILE,
    ConfigError,
    RedactSecrets,
    load_cameras,
    load_env,
    load_tier0_settings,
    secret_values,
)
from simai_relay.epoch_store import DEFAULT_EPOCH_STATE, EpochStore
from simai_relay.relay import RelaySession
from simai_relay.sink import CountingSink, SinkFactory

log = logging.getLogger("simai_relay")


def _setup_logging(secrets: list[str], verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactSecrets(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _report(session: RelaySession, transport: object | None = None) -> None:
    sends = {s["camera_id"]: s for s in transport.snapshots()} if transport is not None else {}  # type: ignore[attr-defined]
    for entry, snap in zip(session.heartbeat_cameras(), session.snapshots(), strict=True):
        record = {**snap, "heartbeat": entry}
        if snap["camera_id"] in sends:
            record["tier0_send"] = sends[snap["camera_id"]]
        log.info("상태 %s", json.dumps(record, ensure_ascii=False))


def _tier0_transport(env: dict[str, str]):  # noqa: ANN202
    """--send: EC2 조회 → 엔드포인트 감시 → 카메라별 TLS 송신 묶음. (watcher, factory)."""
    from simai_site_alert.endpoint import Ec2EndpointLookup
    from simai_site_alert.pinning import parse_pins

    from simai_relay.tls_sink import EndpointWatcher, TlsSinkFactory

    settings = load_tier0_settings(env)
    try:
        pins = parse_pins(settings.pinned_spki)
        lookup = Ec2EndpointLookup(settings.ec2_base_url, device_token=settings.device_token)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    watcher = EndpointWatcher(lookup.fetch, pins)
    factory = TlsSinkFactory(watcher.source, device_token=settings.device_token, pins=pins,
                             laptop_id=settings.laptop_id, ca_file=settings.ca_file)
    return watcher, factory


def cmd_run(args: argparse.Namespace) -> int:
    clock = SystemClock()
    env = load_env(Path(args.env_file))
    env.update({k: v for k, v in os.environ.items() if k.startswith("SIMAI_")})
    _setup_logging(secret_values(env), args.verbose)
    watcher = factory = None
    try:
        cameras = load_cameras(Path(args.config), clock, env)
        epochs = EpochStore(Path(args.epoch_state))
        if args.send:
            watcher, factory = _tier0_transport(env)
    except (ConfigError, KeyError, TypeError, ValueError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    sink_factory: SinkFactory = factory if factory is not None else CountingSink
    session = RelaySession(cameras, sink_factory, clock, epoch_store=epochs)
    log.info("중계 시작 (simai-relay %s, 카메라 %d대, %s, %s)", __version__, len(cameras),
             "Ctrl+C 로 멈춤" if args.seconds is None else f"{args.seconds:.0f}초",
             "Tier 0 로 카메라별 TLS 전송" if factory is not None else "전송 없음: 측정만")
    if watcher is not None:
        watcher.start()
    session.start(args.seconds)
    try:
        while not session.join(timeout=args.report_interval):
            _report(session, factory)
    except KeyboardInterrupt:
        session.stop()
        session.join()
    finally:
        if watcher is not None:
            watcher.stop()
    _report(session, factory)
    errors = session.errors()
    if errors:
        log.error("오류로 멈춘 카메라: %s", errors)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simai-relay", description="simai 노트북 영상 중계 (I0 촬영 시각 헤더)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="카메라별 수신과 I0 헤더 붙이기, --send 면 Tier 0 로 카메라별 TLS 전송")
    run.add_argument("--config", required=True, help="[capture_time]·[[cameras]] TOML (configs/example.toml)")
    run.add_argument("--seconds", type=float, default=None, help="이 시간 뒤 멈춘다 (없으면 Ctrl+C 까지)")
    run.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="카메라 계정 .env (기본: 저장소 루트 .env)")
    run.add_argument("--report-interval", type=float, default=5.0, help="상태 로그 간격(초)")
    run.add_argument("--epoch-state", default=str(DEFAULT_EPOCH_STATE),
                     help="카메라별로 쓴 최대 stream_epoch 를 남기는 파일 (재시작해도 epoch 가 줄지 않게. 기본 ~/.simai)")
    run.add_argument("--send", action="store_true",
                     help="Tier 0 로 전송한다 (.env 의 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN·SIMAI_TIER0_PINNED_SPKI_SHA256)")
    run.add_argument("--verbose", action="store_true")
    from simai_relay import bench

    bench.add_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "bench-transport":
        return bench.cmd_bench(args)
    return 2
