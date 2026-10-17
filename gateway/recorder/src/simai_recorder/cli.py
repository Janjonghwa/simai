"""명령줄: record / manifest / verify.

    uv run simai-recorder record --config sessions/example.toml --seconds 600
    uv run simai-recorder manifest <세션 디렉터리>     # 파일에서 manifest 를 다시 계산
    uv run simai-recorder verify <세션 디렉터리>       # 해시·크기 확인 (Network Volume 에 올린 뒤)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from simai_recorder import __version__
from simai_recorder.clock import SystemClock
from simai_recorder.config import DEFAULT_OUT, REPO_ROOT, ConfigError, load_env, load_session
from simai_recorder.manifest import validate_manifest, verify, write_manifest
from simai_recorder.recorder import record_session


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _summary(manifest: dict) -> str:
    lines = [f"세션 {manifest['session_id']} ({manifest['scenario']}), 입력 {manifest['input_kinds']}"]
    for cam in manifest["cameras"]:
        s = cam["settings"]
        lines.append(
            f"  {cam['camera_id']}: 프레임 {cam['frame_count']}, epoch {len(cam['segments'])}, "
            f"재접속 {cam['reconnects']}, 키프레임 전 버림 {cam['dropped_before_keyframe']}, "
            f"{s['width']}x{s['height']} {s['nominal_fps']} fps"
        )
    return "\n".join(lines)


def cmd_record(args: argparse.Namespace) -> int:
    clock = SystemClock()
    env = load_env(Path(args.env_file))
    try:
        spec = load_session(Path(args.config), clock, env, session_id=args.session_id)
    except (ConfigError, KeyError, TypeError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    print(f"녹화 시작: {spec.session_id} → {out / spec.session_id} "
          f"({'Ctrl+C 로 멈춤' if args.seconds is None else f'{args.seconds:.0f}초'})", flush=True)
    recorded_by = {"tool": "gateway/recorder", "version": __version__, "git_commit": _git_commit(),
                   "gateway_id": args.gateway_id}
    session_dir = record_session(spec, out, clock, args.seconds, recorded_by=recorded_by)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    print(_summary(manifest))
    errors = validate_manifest(manifest)
    for e in errors:
        print(f"manifest 검사: {e}", file=sys.stderr)
    print(f"manifest: {session_dir / 'manifest.json'}")
    return 1 if errors else 0


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest = write_manifest(Path(args.session_dir))
    print(_summary(manifest))
    errors = validate_manifest(manifest)
    for e in errors:
        print(f"manifest 검사: {e}", file=sys.stderr)
    return 1 if errors else 0


def cmd_verify(args: argparse.Namespace) -> int:
    problems = verify(Path(args.session_dir))
    for p in problems:
        print(p, file=sys.stderr)
    print("확인 통과" if not problems else f"문제 {len(problems)}건")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simai-recorder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="카메라를 동시에 녹화하고 manifest 를 쓴다")
    rec.add_argument("--config", required=True, help="세션 설정 TOML")
    rec.add_argument("--seconds", type=float, default=None, help="녹화 시간(초). 없으면 Ctrl+C 까지")
    rec.add_argument("--out", default=str(DEFAULT_OUT), help=f"출력 위치 (기본 {DEFAULT_OUT}, 커밋되지 않음)")
    rec.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="카메라 계정 .env (기본 저장소 루트)")
    rec.add_argument("--session-id", default=None, help="세션 ID (기본 rec-<UTC 시각>-<시나리오>)")
    rec.add_argument("--gateway-id", default="laptop-01", help="녹화한 노트북 식별자")
    rec.set_defaults(func=cmd_record)
    man = sub.add_parser("manifest", help="세션 디렉터리의 파일에서 manifest 를 다시 계산한다")
    man.add_argument("session_dir")
    man.set_defaults(func=cmd_manifest)
    ver = sub.add_parser("verify", help="manifest 의 해시·크기와 파일을 비교한다")
    ver.add_argument("session_dir")
    ver.set_defaults(func=cmd_verify)
    args = ap.parse_args(argv)
    return args.func(args)
