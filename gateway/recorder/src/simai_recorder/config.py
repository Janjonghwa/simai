"""세션 설정(TOML)과 카메라 계정(.env) 읽기.

카메라 계정은 저장소 루트 .env 에서만 읽는다(커밋 금지). 환경 변수가 있으면 그것이 먼저다.
    RTSP_USER, RTSP_PASS                  4대 공통 계정
    RTSP_USER_CAM_A, RTSP_PASS_CAM_A      카메라별 계정(있으면 공통보다 먼저). camera_id 를 대문자로, - 는 _ 로
세션 설정 형식은 sessions/example.toml 과 README 를 본다.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import Clock
from simai_recorder.manifest import validate_consent, validate_storage_uri
from simai_recorder.recorder import CameraSpec, SessionSpec
from simai_recorder.sources import (
    FaultPlan,
    FaultySource,
    FileSource,
    PacketSource,
    RtspConfig,
    RtspSource,
    SyntheticSource,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT = REPO_ROOT / "ai" / "experiments" / "recordings"  # .gitignore 의 recordings/ 로 제외된다


class ConfigError(ValueError):
    pass


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("RTSP_")})
    return env


def _env_key(camera_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", camera_id.upper())


def credentials(env: Mapping[str, str], camera_id: str) -> tuple[str, str]:
    key = _env_key(camera_id)
    user = env.get(f"RTSP_USER_{key}") or env.get("RTSP_USER")
    password = env.get(f"RTSP_PASS_{key}") or env.get("RTSP_PASS")
    if not user or not password:
        raise ConfigError(f"{camera_id}: .env 에 RTSP_USER/RTSP_PASS(또는 RTSP_USER_{key}/RTSP_PASS_{key})가 없다")
    return user, password


def default_session_id(clock: Clock, scenario: str) -> str:
    t = dt.datetime.fromtimestamp(clock.now_ms() / 1000, dt.UTC)
    slug = re.sub(r"[^a-z0-9]+", "-", scenario.lower()).strip("-") or "session"
    return f"rec-{t:%Y%m%d-%H%M%S}z-{slug}"


def _capture_model(base: Mapping[str, Any], override: Mapping[str, Any] | None) -> CaptureTimeModel:
    return CaptureTimeModel(**{**base, **(override or {})})


def _source(cam: Mapping[str, Any], clock: Clock, env: Mapping[str, str], config_dir: Path) -> PacketSource:
    kind = cam.get("kind")
    cid = cam["camera_id"]
    source: PacketSource
    if kind == "rtsp":
        if "faults" in cam:
            raise ConfigError(f"{cid}: 실제 카메라에는 faults 를 쓰지 않는다")
        if not cam.get("host"):
            raise ConfigError(f"{cid}: host 가 없다")
        user, password = credentials(env, cid)
        return RtspSource(RtspConfig(host=cam["host"], path=cam.get("path", "stream1"), port=int(cam.get("port", 554)),
                                     user=user, password=password))
    if kind == "file":
        path = Path(cam["file"])
        source = FileSource(path if path.is_absolute() else config_dir / path, clock,
                            realtime=bool(cam.get("realtime", True)), loop=bool(cam.get("loop", False)),
                            fps=cam.get("fps"))
    elif kind == "synthetic":
        source = SyntheticSource(clock, width=int(cam.get("width", 320)), height=int(cam.get("height", 180)),
                                 fps=float(cam.get("fps", 25)),
                                 keyframe_interval_s=float(cam.get("keyframe_interval_s", 2.0)))
    else:
        raise ConfigError(f"{cid}: kind 는 rtsp / file / synthetic 중 하나다 (받은 값 {kind!r})")
    if "faults" in cam:
        source = FaultySource(source, FaultPlan.from_dict(cam["faults"]), clock)
    return source


def load_session(path: Path, clock: Clock, env: Mapping[str, str], session_id: str | None = None) -> SessionSpec:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sess = data.get("session", {})
    scenario = sess.get("scenario")
    if not scenario:
        raise ConfigError("[session] scenario 가 없다")
    storage = sess.get("storage_uri", "")
    if not isinstance(storage, str) or validate_storage_uri(storage):
        raise ConfigError("[session] storage_uri 는 volume://<datacenter>/<path> 또는 s3:// 여야 한다")
    cams = data.get("cameras", [])
    if not cams:
        raise ConfigError("[[cameras]] 가 없다")
    consent = data.get("consent")
    kinds = sorted({"RTSP" if c.get("kind") == "rtsp" else str(c.get("kind")).upper() for c in cams})
    if errors := validate_consent(consent, kinds):
        raise ConfigError("; ".join(errors))
    base_model = data.get("capture_time", {})
    cameras = [
        CameraSpec(
            camera_id=c["camera_id"],
            source=_source(c, clock, env, path.parent),
            capture_time=_capture_model(base_model, c.get("capture_time")),
            firmware=c.get("firmware") or None,
        )
        for c in cams
    ]
    return SessionSpec(
        session_id=session_id or sess.get("session_id") or default_session_id(clock, scenario),
        scenario=scenario,
        cameras=cameras,
        storage_uri=storage,
        consent=consent,
        description=sess.get("description", ""),
        calibration_version=sess.get("calibration_version") or None,
        notes=sess.get("notes", ""),
    )
