"""명령줄: run(단말 실행), bench(수신 → 소리 시작 지연 측정)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from typing import TYPE_CHECKING

from simai_site_alert.config import DEFAULT_ENV_FILE, Settings, load_settings
from simai_site_alert.sound import FakeSoundBackend, NullSoundBackend, SoundBackend, WinSoundBackend

if TYPE_CHECKING:
    from simai_site_alert.transport import WebSocketControlChannel


def _sound(kind: str) -> SoundBackend:
    if kind == "win":
        return WinSoundBackend()
    if kind == "fake":
        return FakeSoundBackend()
    return NullSoundBackend("소리 백엔드를 끈 상태로 실행했다(--sound none)")


def build_control_channel(settings: Settings) -> WebSocketControlChannel:
    """EC2 엔드포인트 조회 + 공개키 고정으로 I0 제어 채널을 만든다. 설정이 모자라면 ValueError(값은 문장에 넣지 않는다)."""
    from simai_site_alert.endpoint import Ec2EndpointLookup, EndpointResolver
    from simai_site_alert.pinning import parse_pins
    from simai_site_alert.transport import WebSocketControlChannel

    missing = [name for name, value in (("SIMAI_EC2_BASE_URL", settings.ec2_base_url),
                                        ("SIMAI_DEVICE_TOKEN", settings.device_token),
                                        ("SIMAI_TIER0_PINNED_SPKI_SHA256", settings.pinned_spki)) if not value]
    if missing:
        raise ValueError(", ".join(missing) + " 를 .env 에 넣는다")
    assert settings.ec2_base_url and settings.device_token
    lookup = Ec2EndpointLookup(settings.ec2_base_url, device_token=settings.device_token)
    resolver = EndpointResolver(lookup.fetch, parse_pins(settings.pinned_spki), control_path=settings.control_path)
    return WebSocketControlChannel(resolver=resolver, device_token=settings.device_token, ca_file=settings.ca_file)


def _run(args: argparse.Namespace) -> int:
    from simai_site_alert.app import SiteAlertApp
    from simai_site_alert.core import SiteAlertCore
    from simai_site_alert.display import TextDisplay, TkDisplay
    from simai_site_alert.clock import SystemClock
    from simai_site_alert.heartbeat import Ec2HeartbeatClient
    from simai_site_alert.link_status import SiteLinkMonitor
    from simai_site_alert.relay_status import RelayStatusReceiver

    try:
        settings = load_settings(Path(args.env_file))
    except ValueError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    channel = None
    ec2 = None
    if not args.offline:
        try:
            channel = build_control_channel(settings)
            assert settings.ec2_base_url and settings.device_token
            ec2 = Ec2HeartbeatClient(settings.ec2_base_url, device_token=settings.device_token)
        except ValueError as e:
            print(f"설정 오류: {e} (또는 --offline 으로 실행한다)", file=sys.stderr)
            return 2
    clock = SystemClock()
    core = SiteAlertCore(_sound(args.sound), laptop_id=settings.laptop_id, clock=clock)
    # 장애 감시(관제 중단·원격 관제 장애 표시, 노트북 → Tier 0·EC2 하트비트)는 제어 채널이 있을 때만 켠다
    monitor = SiteLinkMonitor() if channel is not None else None
    app = SiteAlertApp(core, channel, monitor=monitor, ec2=ec2, clock=clock)
    receiver = None
    if monitor is not None:
        try:
            receiver = RelayStatusReceiver(app.on_relay_report, port=settings.relay_status_port,
                                           monotonic=clock.monotonic)
            receiver.start()
        except OSError as e:  # 포트를 못 열면 영상 상태를 모름으로 두고(관제 중단 표시) 경보는 계속 돈다
            logging.getLogger(__name__).error(
                "영상 중계 상태 포트 127.0.0.1:%d 를 열지 못했다 (%s). 영상 상태를 모름으로 표시한다",
                settings.relay_status_port, type(e).__name__)
    test_s = None if args.no_test_alert else args.test_alert_seconds
    if args.headless:
        app.add_display(TextDisplay().update)
        app.start(test_alert_s=test_s)
        try:
            import threading

            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            app.close()
            if receiver is not None:
                receiver.close()
        return 0
    display = TkDisplay(on_site_ack=app.site_ack, on_test_alert=lambda: app.test_alert(args.test_alert_seconds),
                        fullscreen=args.fullscreen)
    app.add_display(display.update)
    app.start(test_alert_s=test_s)
    try:
        display.run()
    finally:
        app.close()
        if receiver is not None:
            receiver.close()
    return 0


def _bench(args: argparse.Namespace) -> int:
    from simai_site_alert.bench import run_bench

    sound = _sound(args.sound)
    if args.sound == "win":
        sound.check()
    result = run_bench(sound, count=args.count, interval_s=args.interval)
    print(f"sound={args.sound} n={result.count} p50={result.p50_ms:.3f}ms p95={result.p95_ms:.3f}ms max={result.max_ms:.3f}ms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simai-site-alert", description="simai 노트북 현장 경보 단말")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="현장 경보 단말 실행 (시작 시 시험 경보)")
    run.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    run.add_argument("--sound", choices=["win", "none"], default="win")
    run.add_argument("--offline", action="store_true", help="제어 채널 없이 실행(시험 경보·화면 확인용)")
    run.add_argument("--headless", action="store_true", help="창 없이 문구만 출력")
    run.add_argument("--fullscreen", action="store_true")
    run.add_argument("--no-test-alert", action="store_true")
    run.add_argument("--test-alert-seconds", type=float, default=4.0)
    run.set_defaults(func=_run)

    bench = sub.add_parser("bench", help="수신 → 소리 시작 지연 측정 (loopback WebSocket)")
    bench.add_argument("--sound", choices=["fake", "win"], default="fake")
    bench.add_argument("--count", type=int, default=300)
    bench.add_argument("--interval", type=float, default=0.02)
    bench.set_defaults(func=_bench)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # 한국어 Windows 콘솔(cp949)에서 못 쓰는 글자로 화면 문구가 죽지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)
