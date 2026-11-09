"""명령줄: run (카메라별 수신 + I0 헤더, --send 면 카메라별 TLS 로 Tier 0 에 전송), bench-transport (G0 전송 비교 측정).

    uv run simai-relay run --config configs/dry-run.toml --seconds 30             # 전송 없이 측정만
    uv run simai-relay run --config configs/my-cameras.toml --send                # Tier 0 로 전송, Ctrl+C 로 멈춤
    uv run simai-relay run --config configs/my-cameras.toml --preview-port 47032  # 관제 앱 CCTV 로컬 미리보기(127.0.0.1)
    uv run simai-relay run --config configs/my-cameras.toml --remote-cctv         # 노트북 밖 관리자 원격 시청(EC2 경유)
    uv run simai-relay bench-transport --seconds 20                               # 카메라별 연결 vs 단일 연결 다중화

--send 는 .env 의 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN·SIMAI_TIER0_PINNED_SPKI_SHA256 로 Tier 0 엔드포인트를 조회하고
카메라마다 TLS 연결을 연다(tls_sink). 없으면 CountingSink 로 개수만 센다. 카메라별 상태·측정을 주기적으로 로그에 쓴다.
--preview-port 를 주면(기본 꺼짐) 같은 기록을 127.0.0.1 WebSocket 으로 관제 앱 CCTV 화면에 넘긴다(preview, --send 와 무관).
--remote-cctv 를 주면(기본 꺼짐) .env 의 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN 으로 EC2 CCTV 수신(wss /ws/devices/cctv)에
늘 붙어 있다가 원격 시청자가 있을 때만 같은 기록을 올린다(remote, 대기열은 로컬 미리보기·Tier 0 전송과 따로).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from simai_recorder.clock import SystemClock
from simai_recorder.contract import ContractsNotFoundError

from simai_relay import __version__
from simai_relay.config import (
    DEFAULT_ENV_FILE,
    ConfigError,
    RedactSecrets,
    load_cameras,
    load_env,
    load_remote_cctv_settings,
    load_tier0_settings,
    secret_values,
)
from simai_relay.epoch_store import DEFAULT_EPOCH_STATE, EpochStore
from simai_relay.preview import (
    DEFAULT_ORIGINS,
    DEFAULT_PREVIEW_PORT,
    PreviewHub,
    PreviewServer,
    preview_factory,
)
from simai_relay.recording import (
    DEFAULT_MAX_QUEUE_FRAMES as DEFAULT_RECORD_QUEUE_FRAMES,
    DEFAULT_MIN_FREE_GB,
    RelayRecorder,
    recording_factory,
)
from simai_relay.relay import RelaySession
from simai_relay.remote import RemoteUploader, make_remote_hub
from simai_relay.sink import CountingSink, SinkFactory
from simai_relay.status_report import DEFAULT_PORT as DEFAULT_STATUS_PORT
from simai_relay.status_report import RelayStatusPublisher

log = logging.getLogger("simai_relay")


def _setup_logging(secrets: list[str], verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactSecrets(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _report(session: RelaySession, transport: object | None = None, preview: PreviewHub | None = None,
            remote: RemoteUploader | None = None, recorder: RelayRecorder | None = None) -> None:
    sends = {s["camera_id"]: s for s in transport.snapshots()} if transport is not None else {}  # type: ignore[attr-defined]
    # 로그는 도착 간격 창을 비우지 않는다(창은 1초마다 단말로 보내는 상태 보고가 비운다)
    for entry, snap in zip(session.heartbeat_cameras(reset_window=False), session.snapshots(), strict=True):
        record = {**snap, "heartbeat": entry}
        if snap["camera_id"] in sends:
            record["tier0_send"] = sends[snap["camera_id"]]
        if preview is not None:
            record["preview"] = preview.camera_snapshot(snap["camera_id"])
        if remote is not None:
            record["remote_cctv"] = remote.hub.camera_snapshot(snap["camera_id"])
        if recorder is not None:
            record["recording"] = recorder.camera_snapshot(snap["camera_id"])
        log.info("상태 %s", json.dumps(record, ensure_ascii=False))
    if preview is not None:
        log.info("미리보기 상태 %s", json.dumps(preview.snapshot(), ensure_ascii=False))
    if remote is not None:
        log.info("원격 CCTV 상태 %s", json.dumps(remote.snapshot(), ensure_ascii=False))
    if recorder is not None:
        log.info("녹화 상태 %s", json.dumps(recorder.snapshot(), ensure_ascii=False))


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
    remote_settings = None
    recorder: RelayRecorder | None = None
    try:
        if not 0 <= args.status_port <= 65535:
            raise ValueError("--status-port 는 0~65535 다")
        if args.preview_port is not None and not 1 <= args.preview_port <= 65535:
            raise ValueError("--preview-port 는 1~65535 다")
        if args.record_min_free_gb < 0:
            raise ValueError("--record-min-free-gb 는 0 이상이다")
        if args.record_queue_frames < 1:
            raise ValueError("--record-queue-frames 는 1 이상이다")
        config_path = Path(args.config)
        cameras = load_cameras(config_path, clock, env)
        epochs = EpochStore(Path(args.epoch_state))
        if args.send:
            watcher, factory = _tier0_transport(env)
        if args.remote_cctv:
            remote_settings = load_remote_cctv_settings(env)
        if args.record_dir is not None:
            laptop_id = (env.get("SIMAI_LAPTOP_ID") or "laptop-01").strip()
            recorder = RelayRecorder(
                Path(args.record_dir),
                cameras,
                clock,
                config_path=config_path,
                gateway_id=laptop_id,
                requested_duration_s=args.seconds,
                min_free_bytes=int(args.record_min_free_gb * (1024**3)),
                max_queue_frames=args.record_queue_frames,
            )
    except (ConfigError, ContractsNotFoundError, FileExistsError, KeyError, TypeError, ValueError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    sink_factory: SinkFactory = factory if factory is not None else CountingSink
    if recorder is not None:
        sink_factory = recording_factory(sink_factory, recorder)
    hub = preview_server = None
    if args.preview_port is not None:
        hub = PreviewHub([c.camera_id for c in cameras])
        preview_server = PreviewServer(hub, args.preview_port, args.preview_origin or DEFAULT_ORIGINS)
        try:
            preview_server.start()
        except OSError as e:
            print(f"미리보기 포트를 열지 못했다(127.0.0.1:{args.preview_port}): {type(e).__name__}", file=sys.stderr)
            return 2
        sink_factory = preview_factory(sink_factory, hub)
    remote = None
    if remote_settings is not None:
        try:
            remote = RemoteUploader(make_remote_hub([c.camera_id for c in cameras]), remote_settings.ec2_base_url,
                                    remote_settings.device_token)
        except ValueError as e:
            if preview_server is not None:
                preview_server.stop()
            print(f"설정 오류: {e}", file=sys.stderr)
            return 2
        # 로컬 미리보기와 따로 둔 허브. 원래 전송(Tier 0)·로컬 미리보기를 먼저 하고, 원격 쪽 고장은 수신으로 올리지 않는다
        sink_factory = preview_factory(sink_factory, remote.hub)
    if recorder is not None:
        recorder.start()
    session = RelaySession(cameras, sink_factory, clock, epoch_store=epochs)
    log.info("중계 시작 (simai-relay %s, 카메라 %d대, %s, %s%s)", __version__, len(cameras),
             "Ctrl+C 로 멈춤" if args.seconds is None else f"{args.seconds:.0f}초",
             "Tier 0 로 카메라별 TLS 전송" if factory is not None else "전송 없음: 측정만",
             f", 동시 녹화 {recorder.record_dir}" if recorder is not None else "")
    if watcher is not None:
        watcher.start()
    if remote is not None:
        remote.start()
    session.start(args.seconds)
    publisher = None
    if args.status_port:
        # 현장 경보 단말에 1초마다 상태 보고(관제 중단 판단·laptop_heartbeat). --send 가 아니면 Tier 0 영상 연결은 없다
        video = factory.video_cameras if factory is not None else list
        publisher = RelayStatusPublisher(lambda: session.heartbeat_cameras(), video, port=args.status_port)
        publisher.start()
    try:
        while not session.join(timeout=args.report_interval):
            _report(session, factory, hub, remote, recorder)
    except KeyboardInterrupt:
        session.stop()
        session.join()
    finally:
        if publisher is not None:
            publisher.stop()
        if watcher is not None:
            watcher.stop()
        if preview_server is not None:
            preview_server.stop()
        if remote is not None:
            remote.stop()
        if recorder is not None:
            recorder.close(session)
    _report(session, factory, hub, remote, recorder)
    errors = session.errors()
    if errors:
        log.error("오류로 멈춘 카메라: %s", errors)
        return 1
    if recorder is not None and recorder.manifest_errors:
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
    run.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT,
                     help="현장 경보 단말에 상태를 알리는 127.0.0.1 UDP 포트 (기본 47031, 단말 SIMAI_RELAY_STATUS_PORT 와 같게. 0 이면 끔)")
    run.add_argument("--preview-port", type=int, default=None,
                     help=f"관제 앱 CCTV 로컬 미리보기 WebSocket 포트 (127.0.0.1 에서만, 주지 않으면 끔. 관제 앱 기본 {DEFAULT_PREVIEW_PORT})")
    run.add_argument("--preview-origin", action="append", default=None,
                     help="미리보기에 붙을 수 있는 관제 앱 출처 (여러 번, 주면 기본 목록을 바꾼다. 기본: "
                          + ", ".join(DEFAULT_ORIGINS) + ")")
    run.add_argument("--remote-cctv", action="store_true",
                     help="노트북 밖 관리자 CCTV 원격 시청: EC2(wss /ws/devices/cctv)에 늘 붙어 있다가 보는 사람이 있을 때만 "
                          "올린다 (.env 의 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN, 주지 않으면 끔)")
    run.add_argument("--record-dir", default=None,
                     help="송신하는 압축 프레임·I0 헤더를 녹화 세그먼트와 manifest 로 저장할 폴더 (주지 않으면 녹화 안 함)")
    run.add_argument("--record-min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB,
                     help=f"녹화 유지 최소 디스크 여유 공간(GB, 기본 {DEFAULT_MIN_FREE_GB:g}). 하한 아래면 녹화만 중단하고 송신은 계속")
    run.add_argument("--record-queue-frames", type=int, default=DEFAULT_RECORD_QUEUE_FRAMES,
                     help=f"카메라별 녹화 큐 프레임 상한 (기본 {DEFAULT_RECORD_QUEUE_FRAMES}). 넘치면 녹화 프레임만 버림")
    run.add_argument("--verbose", action="store_true")
    from simai_relay import bench

    bench.add_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "bench-transport":
        return bench.cmd_bench(args)
    return 2
