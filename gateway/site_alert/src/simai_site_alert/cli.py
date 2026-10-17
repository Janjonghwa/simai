"""명령줄: run(단말 실행), bench(수신 → 소리 시작 지연 측정)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from simai_site_alert.config import DEFAULT_ENV_FILE, load_settings
from simai_site_alert.sound import FakeSoundBackend, NullSoundBackend, SoundBackend, WinSoundBackend


def _sound(kind: str) -> SoundBackend:
    if kind == "win":
        return WinSoundBackend()
    if kind == "fake":
        return FakeSoundBackend()
    return NullSoundBackend("소리 백엔드를 끈 상태로 실행했다(--sound none)")


def _run(args: argparse.Namespace) -> int:
    from simai_site_alert.app import SiteAlertApp
    from simai_site_alert.core import SiteAlertCore
    from simai_site_alert.display import TextDisplay, TkDisplay
    from simai_site_alert.transport import WebSocketControlChannel

    settings = load_settings(Path(args.env_file))
    channel = None
    if not args.offline:
        if not settings.control_url or not settings.device_token:
            print("설정 오류: SIMAI_TIER0_CONTROL_URL 과 SIMAI_DEVICE_TOKEN 을 .env 에 넣거나 --offline 으로 실행한다",
                  file=sys.stderr)
            return 2
        try:
            channel = WebSocketControlChannel(settings.control_url, device_token=settings.device_token,
                                              ca_file=settings.ca_file)
        except ValueError as e:
            print(f"설정 오류: {e}", file=sys.stderr)
            return 2
    core = SiteAlertCore(_sound(args.sound), laptop_id=settings.laptop_id)
    app = SiteAlertApp(core, channel)
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
        return 0
    display = TkDisplay(on_site_ack=app.site_ack, on_test_alert=lambda: app.test_alert(args.test_alert_seconds),
                        fullscreen=args.fullscreen)
    app.add_display(display.update)
    app.start(test_alert_s=test_s)
    try:
        display.run()
    finally:
        app.close()
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
