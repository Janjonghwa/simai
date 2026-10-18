"""명령줄: measure / concurrency (사용법은 gateway/rtsp_probe/README.md).

    uv run simai-rtsp-probe measure --host 192.168.0.11 --camera-id cam_A --seconds 600
    uv run simai-rtsp-probe concurrency --host 192.168.0.11 --camera-id cam_A --max-sessions 3 --seconds 15

카메라 계정은 저장소 루트 .env 의 RTSP_USER / RTSP_PASS(카메라별 RTSP_USER_CAM_A …)에서만 읽는다(녹화 도구와 같은 규칙).
보고서·원본 녹화는 ai/experiments/recordings/rtsp-probe/ 아래에 쓴다(.gitignore 의 recordings/ 로 커밋되지 않는다).

종료 코드: 0 측정 완료, 1 접속 실패·끊김·프레임 부족(보고서는 남긴다), 2 설정 오류.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from simai_recorder.clock import Clock, SystemClock
from simai_recorder.config import DEFAULT_OUT, REPO_ROOT, ConfigError, credentials, load_env
from simai_recorder.recorder import CAMERA_ID
from simai_recorder.sources import RtspConfig, RtspSource

from simai_rtsp_probe import __version__
from simai_rtsp_probe.capture import concurrency_trials, max_concurrent, measure
from simai_rtsp_probe.report import stream_section, summarize

DEFAULT_PROBE_OUT = DEFAULT_OUT / "rtsp-probe"
TOP_LEVEL = ("rtcp", "timestamp_source_recommendation", "timestamp_source_reason", "warnings")


def secret_forms(secrets: Iterable[str]) -> list[str]:
    """가릴 문자열: 계정 값 그대로와 URL 인코딩한 꼴. 긴 것부터 바꾼다."""
    forms: set[str] = set()
    for s in secrets:
        if s:
            forms.update({s, urllib.parse.quote(s, safe=""), urllib.parse.quote(s)})
    return sorted(forms, key=len, reverse=True)


def redact(value: Any, forms: list[str]) -> Any:
    """보고서의 문자열 값에 계정이 섞이면 *** 로 바꾼다(숫자·키는 건드리지 않는다). 코드가 계정을 넣지 않는 것에 더한 안전망."""
    if isinstance(value, str):
        for f in forms:
            value = value.replace(f, "***")
        return value
    if isinstance(value, Mapping):
        return {k: redact(v, forms) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, forms) for v in value]
    return value


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _run_dir(out: Path, clock: Clock, camera_id: str, kind: str) -> Path:
    t = dt.datetime.fromtimestamp(clock.now_ms() / 1000, dt.UTC)
    base = out / f"{t:%Y%m%d-%H%M%S}z-{camera_id}-{kind}"
    d, k = base, 1
    while d.exists():  # 덮어쓰지 않는다
        d, k = base.with_name(f"{base.name}-{k}"), k + 1
    d.mkdir(parents=True)
    return d


def _rtsp_config(args: argparse.Namespace, env: Mapping[str, str]) -> RtspConfig:
    user, password = credentials(env, args.camera_id)
    return RtspConfig(host=args.host, path=args.path, port=args.port, user=user, password=password,
                      open_timeout_s=args.open_timeout, read_timeout_s=args.read_timeout)


def _write(run_dir: Path, report: dict[str, Any], forms: list[str]) -> dict[str, Any]:
    report = redact(report, forms)
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _header(args: argparse.Namespace, kind: str, source_desc: dict[str, Any], clock: Clock) -> dict[str, Any]:
    return {"tool": "gateway/rtsp_probe", "version": __version__, "git_commit": _git_commit(), "kind": kind,
            "camera_id": args.camera_id, "source": source_desc, "note": args.note,
            "started_at_ms": clock.now_ms(), "requested_seconds": args.seconds}


def _print_measure(report: dict[str, Any], path: Path) -> None:
    s, m = report.get("stream") or {}, report.get("metrics") or {}
    rtcp = report.get("rtcp") or {}
    ai = m.get("arrival_interval_ms") or {}
    key = m.get("keyframe_interval_s") or {}
    gap = m.get("camera_to_gateway_gap") or {}
    lines = [
        f"카메라 {report['camera_id']} {report['source'].get('url')}  종료: {report['ended_by']}"
        + (f" ({report['error']})" if report.get("error") else ""),
        f"  스트림: {s.get('codec')} {s.get('profile')} {s.get('width')}x{s.get('height')}, "
        f"선언 fps {s.get('nominal_fps_declared')}",
        f"  프레임 {m.get('frames')}개 / {m.get('duration_s')}초, fps 도착 {m.get('fps_by_arrival')} · PTS {m.get('fps_by_pts')}",
        f"  도착 간격 ms p50 {ai.get('p50')} p95 {ai.get('p95')} p99 {ai.get('p99')} 최대 {ai.get('max')}, "
        f"끊김 {gap.get('count')}회(기준 {gap.get('threshold_ms')}ms)",
        f"  키프레임 {m.get('keyframes')}개, 주기 p50 {key.get('p50')}s 최대 {key.get('max')}s, "
        f"비트레이트 {m.get('bitrate_kbps_by_pts')} kbps(PTS 기준)",
        f"  RTCP SR {rtcp.get('sender_reports')}개, 촬영 시각 출처 제안 {report.get('timestamp_source_recommendation')}: "
        f"{report.get('timestamp_source_reason')}",
    ]
    lines += [f"  주의: {w}" for w in report.get("warnings") or []]
    lines.append(f"보고서: {path}")
    print("\n".join(lines))


def cmd_measure(args: argparse.Namespace, env: Mapping[str, str], clock: Clock) -> int:
    cfg = _rtsp_config(args, env)
    forms = secret_forms([cfg.user, cfg.password])
    source = RtspSource(cfg)
    run_dir = _run_dir(Path(args.out), clock, args.camera_id, "measure")
    report = _header(args, "measure", source.describe(), clock)
    print(f"측정 중: {report['source']['url']} {args.seconds:g}초 (원본 녹화 {args.record_seconds:g}초) ...", flush=True)
    m = measure(source, clock, args.seconds, record_dir=run_dir, camera_id=args.camera_id,
                record_seconds=args.record_seconds)
    metrics = summarize(m.observations, m.info)
    report.update(ended_at_ms=clock.now_ms(), ended_by=m.ended_by, error=m.error, stream=stream_section(m.info))
    report.update({k: metrics.pop(k) for k in TOP_LEVEL})
    report["metrics"] = metrics
    report["recording"] = m.recording
    report = _write(run_dir, report, forms)
    _print_measure(report, run_dir / "report.json")
    ok = m.ended_by == "completed" and len(m.observations) >= 2
    return 0 if ok else 1


def cmd_concurrency(args: argparse.Namespace, env: Mapping[str, str], clock: Clock) -> int:
    cfg = _rtsp_config(args, env)
    forms = secret_forms([cfg.user, cfg.password])
    run_dir = _run_dir(Path(args.out), clock, args.camera_id, "concurrency")
    report = _header(args, "concurrency", RtspSource(cfg).describe(), clock)
    print(f"동시 접속 시험: {report['source']['url']} 세션 1~{args.max_sessions}개, 각 {args.seconds:g}초 ...", flush=True)
    trials = concurrency_trials(lambda: RtspSource(cfg), clock, args.max_sessions, args.seconds,
                                open_gap_s=args.open_gap, rest_s=args.rest)
    report.update(ended_at_ms=clock.now_ms(), trials=trials, max_concurrent_sessions_ok=max_concurrent(trials))
    report = _write(run_dir, report, forms)
    for t in report["trials"]:
        detail = ", ".join(f"#{s['session']} " + (f"{s.get('frames')}프레임 {s.get('fps_by_arrival')}fps {s.get('ended_by')}"
                                                   if s["opened"] else f"거절({s.get('error')})")
                           for s in t["sessions"])
        print(f"  세션 {t['requested_sessions']}개: 열림 {t['opened']}, 끝까지 받음 {t['received_whole_time']} — {detail}")
    print(f"  모든 세션이 끝까지 받은 최대 동시 접속: {report['max_concurrent_sessions_ok']}")
    print(f"보고서: {run_dir / 'report.json'}")
    return 0 if report["max_concurrent_sessions_ok"] else 1


def _camera_id(value: str) -> str:
    if not CAMERA_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("camera_id 는 영문·숫자·_·- 만 쓴다")
    return value


def main(argv: list[str] | None = None, clock: Clock | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simai-rtsp-probe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", required=True, help="카메라 IP (계정은 .env 에서만 읽는다)")
    common.add_argument("--port", type=int, default=554)
    common.add_argument("--path", default="stream1", help="stream1=고화질(2304x1296), stream2=저화질")
    common.add_argument("--camera-id", type=_camera_id, default="cam_A", help="보고서·카메라별 계정 이름(RTSP_USER_<ID>)")
    common.add_argument("--note", default="", help="측정 환경 메모(예: '공유기 2.4GHz, 카메라와 3m')")
    common.add_argument("--out", default=str(DEFAULT_PROBE_OUT), help=f"출력 위치 (기본 {DEFAULT_PROBE_OUT}, 커밋되지 않음)")
    common.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="카메라 계정 .env (기본 저장소 루트)")
    common.add_argument("--open-timeout", type=float, default=10.0, help="접속 제한 시간(초)")
    common.add_argument("--read-timeout", type=float, default=5.0, help="수신이 이만큼 멈추면 끊김으로 본다(초)")
    mea = sub.add_parser("measure", parents=[common], help="카메라 한 대를 seconds 동안 받아 보고서를 쓴다")
    mea.add_argument("--seconds", type=float, default=600, help="측정 시간(초, 기본 10분)")
    mea.add_argument("--record-seconds", type=float, default=10,
                     help="원본 녹화 길이(초, 0 이면 녹화 안 함). 사람이 없는 교실에서만 녹화한다")
    mea.set_defaults(func=cmd_measure)
    con = sub.add_parser("concurrency", parents=[common], help="세션 1·2·…개를 동시에 열어 받아 본다")
    con.add_argument("--max-sessions", type=int, default=3)
    con.add_argument("--seconds", type=float, default=15, help="시도마다 동시에 받는 시간(초)")
    con.add_argument("--open-gap", type=float, default=1.0, help="세션을 하나씩 여는 간격(초)")
    con.add_argument("--rest", type=float, default=2.0, help="시도 사이 쉬는 시간(초)")
    con.set_defaults(func=cmd_concurrency)
    args = ap.parse_args(argv)
    if args.seconds <= 0 or (args.cmd == "concurrency" and args.max_sessions < 1):
        print("설정 오류: --seconds 는 양수, --max-sessions 는 1 이상이다", file=sys.stderr)
        return 2
    env = load_env(Path(args.env_file))
    try:
        return args.func(args, env, clock or SystemClock())
    except ConfigError as e:  # 계정이 없을 때. 메시지에는 변수 이름만 있다
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
