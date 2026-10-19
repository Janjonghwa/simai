"""무마커 내부 파라미터 추정 실행 도구 (명세 4.3 1단계, FR-23). `ai/` 에서:

    uv run python -m simai_ai.calibration.intrinsics.cli --session ../gateway/recorder/sessions/classroom.toml
    uv run python -m simai_ai.calibration.intrinsics.cli --rtsp cam_A=192.168.0.11 --rtsp cam_B=192.168.0.12:554/stream1
    uv run python -m simai_ai.calibration.intrinsics.cli --file cam1=a.mp4 --file cam2=b.mp4 --frames-source SIM

입력(카메라 RTSP 또는 시험용 영상 파일, 여러 대)을 `--duration` 초 동안 `--interval` 초 간격으로 메모리에만 받아 시간 중앙값
배경을 만들고, 카메라별로 AnyCalib → GeoCalib → 고전 기준 순서(`--backends`)로 추정한 뒤 같은 렌즈 4대 공통값을 합쳐
기록 JSON(`simai.intrinsics_estimate`) 하나만 쓴다. 영상·이미지·중간 프레임은 디스크(임시 폴더 포함)에 쓰지 않는다.

- 카메라 주소: `--session`(gateway/recorder 세션 TOML 의 [[cameras]], kind rtsp·file) 또는 `--rtsp`·`--file`.
  이 도구는 gateway 패키지를 import 하지 않고 같은 형식을 직접 읽는다.
- 계정: 저장소 루트 `.env`(또는 `--env-file`)의 RTSP_USER_<camera_id 대문자, - 는 _>/RTSP_PASS_<…>, 없으면 RTSP_USER/RTSP_PASS.
  환경 변수 RTSP_* 가 있으면 그것이 먼저다. 계정 값은 출력·기록·예외 메시지에 남기지 않는다.
- 학습 모델 가중치: `--anycalib-weights`·`--anycalib-sha256`(또는 SIMAI_ANYCALIB_WEIGHTS·SIMAI_ANYCALIB_SHA256), GeoCalib 도 같다.
  가중치·패키지가 없으면 그 백엔드는 사용 불가로 기록되고 다음 백엔드로 넘어간다(기록의 cameras[].backend 에 남는다).
- 출력 기본 위치: `ai/experiments/recordings/intrinsics/` (.gitignore 의 recordings/ 로 제외).
- 종료 코드: 0 기록을 썼고 status OK 카메라가 하나 이상, 3 기록은 썼지만 OK 카메라가 없음, 2 인자·설정 오류.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from simai_ai.calibration.intrinsics.capture import (
    SOURCE_FILE,
    SOURCE_RTSP,
    CameraInput,
    CaptureConfig,
    RtspCredentials,
)
from simai_ai.calibration.intrinsics.chain import (
    BACKEND_METHODS,
    DEFAULT_BACKEND_ORDER,
    StraightnessCheckConfig,
    build_default_chain,
)
from simai_ai.calibration.intrinsics.estimate import STATUS_OK, WeightsRef
from simai_ai.calibration.intrinsics.runner import FILE_FRAME_SOURCES, frames_source_for, run_intrinsics

AI_DIR = Path(__file__).resolve().parents[4]
REPO_ROOT = AI_DIR.parent
DEFAULT_OUT_DIR = AI_DIR / "experiments" / "recordings" / "intrinsics"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NO_ESTIMATE = 3


class ConfigError(ValueError):
    pass


def load_env(path: Path | None) -> dict[str, str]:
    """`.env` 의 KEY=VALUE 와 환경 변수 RTSP_* (환경 변수가 먼저)."""
    env: dict[str, str] = {}
    if path is not None and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("RTSP_")})
    return env


def env_key(camera_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", camera_id.upper())


def credentials_for(env: Mapping[str, str], camera_id: str) -> RtspCredentials:
    """카메라 계정. 없으면 ConfigError(메시지에 키 이름만, 값은 넣지 않는다)."""
    key = env_key(camera_id)
    user = env.get(f"RTSP_USER_{key}") or env.get("RTSP_USER")
    password = env.get(f"RTSP_PASS_{key}") or env.get("RTSP_PASS")
    if not user or not password:
        raise ConfigError(f"{camera_id}: .env 에 RTSP_USER_{key}/RTSP_PASS_{key}(또는 RTSP_USER/RTSP_PASS)가 없다")
    return RtspCredentials(user, password)


def parse_rtsp_arg(value: str, default_path: str = "stream1", default_port: int = 554) -> CameraInput:
    """CAM=HOST[:PORT][/PATH] → RTSP 입력. 계정은 여기 넣지 않는다(.env)."""
    if "@" in value:  # 값을 메시지에 넣지 않는다(계정이 들어 있을 수 있다)
        raise ConfigError("--rtsp 에 계정을 넣지 않는다(.env 의 RTSP_USER/RTSP_PASS 를 쓴다)")
    camera_id, sep, rest = value.partition("=")
    if not sep or not camera_id or not rest:
        raise ConfigError(f"--rtsp 는 CAM=HOST[:PORT][/PATH] 형식이다: {value!r}")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return CameraInput(camera_id, SOURCE_RTSP, host=host, port=int(port) if port else default_port,
                       path=path or default_path)


def parse_file_arg(value: str) -> CameraInput:
    camera_id, sep, path = value.partition("=")
    if not sep or not camera_id or not path:
        raise ConfigError(f"--file 은 CAM=PATH 형식이다: {value!r}")
    return CameraInput(camera_id, SOURCE_FILE, file=Path(path))


def load_session_cameras(path: Path, default_path: str = "stream1", default_port: int = 554) -> list[CameraInput]:
    """gateway/recorder 세션 TOML 의 [[cameras]] 를 읽는다(kind rtsp·file 만. 다른 절은 쓰지 않는다)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cams = data.get("cameras") or []
    if not cams:
        raise ConfigError(f"{path.name}: [[cameras]] 가 없다")
    out: list[CameraInput] = []
    for cam in cams:
        cid = cam.get("camera_id")
        kind = cam.get("kind")
        if not cid:
            raise ConfigError(f"{path.name}: camera_id 가 없는 [[cameras]] 가 있다")
        if kind == "rtsp":
            out.append(CameraInput(cid, SOURCE_RTSP, host=cam.get("host"), port=int(cam.get("port", default_port)),
                                   path=cam.get("path", default_path)))
        elif kind == "file":
            file = Path(cam["file"])
            out.append(CameraInput(cid, SOURCE_FILE, file=file if file.is_absolute() else path.parent / file))
        else:
            raise ConfigError(f"{cid}: 이 도구는 kind rtsp·file 만 읽는다(받은 값 {kind!r})")
    return out


def weights_from(path: str | None, sha256: str | None, name: str) -> WeightsRef | None:
    if not path and not sha256:
        return None
    if not path or not sha256:
        raise ConfigError(f"{name} 가중치는 경로와 sha256 을 함께 준다")
    try:
        return WeightsRef(path, sha256)
    except ValueError as e:
        raise ConfigError(f"{name}: {e}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m simai_ai.calibration.intrinsics.cli",
        description="카메라 RTSP·영상 파일 → 메모리 시간 중앙값 배경 → 내부 파라미터 추정 기록 JSON (영상은 저장하지 않는다)",
    )
    src = p.add_argument_group("입력")
    src.add_argument("--session", type=Path, help="gateway/recorder 세션 TOML ([[cameras]] 만 읽는다)")
    src.add_argument("--rtsp", action="append", default=[], metavar="CAM=HOST[:PORT][/PATH]")
    src.add_argument("--file", action="append", default=[], metavar="CAM=PATH", help="시험용 영상 파일")
    src.add_argument("--cameras", help="쓸 camera_id 목록(쉼표). 없으면 모두")
    src.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="RTSP 계정 .env (기본: 저장소 루트)")
    src.add_argument("--frames-source", choices=FILE_FRAME_SOURCES, default="FILE",
                     help="파일 입력의 출처(input_provenance.frames.source). RTSP 는 항상 LIVE")
    cap = p.add_argument_group("수집")
    cap.add_argument("--duration", type=float, default=CaptureConfig.duration_s, help="수집 시간(초)")
    cap.add_argument("--interval", type=float, default=CaptureConfig.interval_s, help="프레임 간격(초)")
    cap.add_argument("--min-frames", type=int, default=CaptureConfig.min_frames)
    cap.add_argument("--max-frames", type=int, default=CaptureConfig.max_frames)
    cap.add_argument("--open-timeout", type=float, default=CaptureConfig.open_timeout_s)
    cap.add_argument("--read-timeout", type=float, default=CaptureConfig.read_timeout_s)
    est = p.add_argument_group("추정")
    est.add_argument("--backends", default=",".join(DEFAULT_BACKEND_ORDER),
                     help=f"시도 순서(쉼표, {sorted(BACKEND_METHODS)}). 앞 백엔드가 사용 불가일 때만 다음으로 넘어간다")
    est.add_argument("--anycalib-weights", default=os.environ.get("SIMAI_ANYCALIB_WEIGHTS"))
    est.add_argument("--anycalib-sha256", default=os.environ.get("SIMAI_ANYCALIB_SHA256"))
    est.add_argument("--geocalib-weights", default=os.environ.get("SIMAI_GEOCALIB_WEIGHTS"))
    est.add_argument("--geocalib-sha256", default=os.environ.get("SIMAI_GEOCALIB_SHA256"))
    est.add_argument("--device", default="cpu", help="학습 모델 장치(기본 cpu)")
    est.add_argument("--no-verify", action="store_true", help="학습 결과의 직선성 확인을 끈다(시험용)")
    p.add_argument("--out", type=Path, help=f"기록 JSON 경로 (기본 {DEFAULT_OUT_DIR}/intrinsics-<UTC>.json)")
    return p


def collect_inputs(args: argparse.Namespace) -> list[CameraInput]:
    cameras: list[CameraInput] = []
    if args.session is not None:
        cameras += load_session_cameras(args.session)
    cameras += [parse_rtsp_arg(v) for v in args.rtsp]
    cameras += [parse_file_arg(v) for v in args.file]
    if args.cameras:
        wanted = [c.strip() for c in args.cameras.split(",") if c.strip()]
        missing = sorted(set(wanted) - {c.camera_id for c in cameras})
        if missing:
            raise ConfigError(f"--cameras 에 있는데 입력에 없는 카메라: {missing}")
        cameras = [c for c in cameras if c.camera_id in wanted]
    if not cameras:
        raise ConfigError("입력 카메라가 없다(--session, --rtsp, --file 중 하나 이상)")
    return cameras


def main(argv: Sequence[str] | None = None, *, now_ms: int | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cameras = collect_inputs(args)
        frames_source = frames_source_for(cameras, args.frames_source)
        capture = CaptureConfig(
            duration_s=args.duration,
            interval_s=args.interval,
            min_frames=args.min_frames,
            max_frames=args.max_frames,
            open_timeout_s=args.open_timeout,
            read_timeout_s=args.read_timeout,
        )
        order = [b.strip() for b in args.backends.split(",") if b.strip()]
        chain = build_default_chain(
            order,
            anycalib_weights=weights_from(args.anycalib_weights, args.anycalib_sha256, "AnyCalib"),
            geocalib_weights=weights_from(args.geocalib_weights, args.geocalib_sha256, "GeoCalib"),
            device=args.device,
            verify=None if args.no_verify else StraightnessCheckConfig(),
        )
    except (ConfigError, ValueError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return EXIT_CONFIG

    credentials: dict[str, RtspCredentials] = {}
    credential_errors: dict[str, str] = {}
    if any(c.kind == SOURCE_RTSP for c in cameras):
        env = load_env(args.env_file)
        for cam in cameras:
            try:
                credentials[cam.camera_id] = credentials_for(env, cam.camera_id)
            except ConfigError as e:
                credential_errors[cam.camera_id] = str(e)

    created_at = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    out = args.out or DEFAULT_OUT_DIR / f"intrinsics-{_utc_stamp(created_at)}.json"
    print(f"수집 시작: 카메라 {len(cameras)}대, {capture.duration_s:g}s 동안 {capture.interval_s:g}s 간격 "
          f"(영상은 메모리에서만 처리, 저장하지 않음)", file=sys.stderr)
    result = run_intrinsics(
        cameras,
        chain=chain,
        capture_config=capture,
        created_at=created_at,
        frames_source=frames_source,
        credentials=credentials,
        credential_errors=credential_errors,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(result.record, out)
    any_ok = any(r.status == STATUS_OK for r in result.cameras)
    return EXIT_OK if any_ok else EXIT_NO_ESTIMATE


def _utc_stamp(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y%m%d-%H%M%Sz")


def _print_summary(record: Mapping, out: Path) -> None:
    for cam in record.get("cameras", []):
        backend = cam.get("backend") or {}
        line = (f"{cam['camera_id']}: {cam['status']} method={cam.get('method', '-')} "
                f"frames={cam['capture'].get('frames_used', 0)}")
        if backend.get("fallback"):
            line += f" (기본 백엔드 {backend['order'][0]} 대신 {backend.get('selected')} 사용)"
        check = backend.get("straightness_check")
        if check:
            line += f" 직선성 확인={check['status']}"
        if cam.get("reasons"):
            line += " 사유: " + "; ".join(cam["reasons"])
        print(line)
    shared = record.get("shared_lens")
    if shared is not None:
        common = shared["common"]
        print(f"같은 렌즈 공통값: {common['status']} (합의 {shared['consensus']})")
    elif "run" in record and record["run"].get("shared_lens_skipped"):
        print(record["run"]["shared_lens_skipped"])
    print(f"기록: {out}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
