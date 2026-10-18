"""Tier 0 서비스 설정: 설정 파일(JSON, --config) + 환경 변수(.env, 배포·비밀 값).

설정 파일 (모든 키 선택, 경로는 설정 파일 위치 기준 상대 경로도 된다):

    {
      "gateway_id": "tier0-01",               봉투 source_id·이벤트 gateway_id (환경 변수 SIMAI_TIER0_ID 가 우선)
      "state_dir": "/workspace/simai/state",   Network Volume 상태 루트 (기본 $SIMAI_ROOT/state)
      "scene_version": "scene-0",              world_state scene_version (정적 3D 확정 전 기본안)
      "calibration_file": "calibration.json",  보정 문서(contracts/providers/calibration). SIM 은 기본 run_dir/calibration.json
      "input": {"mode": "sim", "run_dir": "...", "anchor_wall_ms": 1760342400000, "arrival_lag_ms": 60},
               {"mode": "live", "camera_ids": ["cam1", "cam2", "cam3", "cam4"]}
      "bootstrap": {"archive": "...", "config_version": "cfg-1"},   체크포인트의 묶음을 보관소에서 찾지 못할 때 쓸 시작 묶음
      "lifecycle": {"clear_hold_ms": 3000, "persistence": [2, 3], "resume_window_ms": 10000},
      "tick_ms": 100,                          판정 주기 (10 Hz, 명세 12.2 world_state)
      "checkpoint_interval_s": 1.0,            체크포인트 주기 (명세 3.2 초기값 1초)
      "drain_s": 5.0                           종료 때 저널 쓰기·스레드 정리를 기다리는 상한
    }

환경 변수:

    SIMAI_ROOT                       Network Volume 루트 (기본 /workspace/simai). state_dir 기본값의 부모
    SIMAI_TIER0_ID                   Tier 0 인스턴스 ID (기본 tier0-01)
    SIMAI_TIER0_ENGINE               YOLO26x-pose TensorRT 엔진 경로 (deploy/tier0/entrypoint.sh 가 넣는다, LIVE 입력)
    SIMAI_EC2_I1_URL / SIMAI_EC2_BASE_URL (+ SIMAI_EC2_I1_PATH), SIMAI_TIER0_TOKEN, SIMAI_EC2_CA_FILE
                                     I1 접속 (i1_client.I1Settings). 둘 다 없으면 EC2 없이 돈다(오프라인, health EC2_LINK_LOST)
    SIMAI_TIER0_CONTROL_PORT         I0 제어 채널(wss) 대칭 매핑 요청 번호. 실제 포트는 RUNPOD_TCP_PORT_<요청 번호>.
                                     없으면 노트북 없이 돈다(health GATEWAY_LINK_LOST)
    SIMAI_TIER0_CONTROL_LISTEN_HOST  제어 채널 듣는 주소 (기본 0.0.0.0)
    SIMAI_TIER0_TLS_CERT_FILE, SIMAI_TIER0_TLS_KEY_FILE   Tier 0 TLS 서버 인증서·키 (I0 영상·제어 공용)
    SIMAI_LAPTOP_TOKEN_SHA256        노트북 기기 토큰 sha256 지문 목록
    엔드포인트 등록(endpoint_registration): RUNPOD_PUBLIC_IP, SIMAI_TIER0_VIDEO_PORTS, RUNPOD_TCP_PORT_* 등

값을 모르면 지어내지 않는다. 필요한 값이 빠지면 Tier0ConfigError 로 시작하지 않는다(어떤 값이 없는지 이름만 적는다).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.tier0.endpoint_registration import DEFAULT_TIER0_ID
from services.tier0.i1_client import I1ConfigError, I1Settings
from simai_ai.risk import LifecyclePolicy, Persistence

DEFAULT_ROOT = "/workspace/simai"


class Tier0ConfigError(ValueError):
    """설정을 모르거나 틀렸다. 문장에 비밀 값을 넣지 않는다."""


@dataclass(frozen=True)
class SimInput:
    run_dir: Path
    anchor_wall_ms: int | None = None
    arrival_lag_ms: int = 60
    max_backlog_ms: int = 500


@dataclass(frozen=True)
class ControlSettings:
    host: str
    port: int
    cert_file: str
    key_file: str
    token_hashes: str = field(repr=False)


@dataclass(frozen=True)
class Tier0Settings:
    gateway_id: str
    state_dir: Path
    scene_version: str
    input_mode: str
    calibration_file: Path
    sim: SimInput | None = None
    camera_ids: tuple[str, ...] = ()
    bootstrap_archive: Path | None = None
    bootstrap_config_version: str | None = None
    lifecycle: LifecyclePolicy = field(default_factory=LifecyclePolicy)
    tick_ms: int = 100
    checkpoint_interval_s: float = 1.0
    drain_s: float = 5.0
    engine_path: str | None = None
    i1: I1Settings | None = None
    control: ControlSettings | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def journal_dir(self) -> Path:
        return self.state_dir / "journal"

    @property
    def checkpoint_dir(self) -> Path:
        return self.state_dir / "checkpoints"

    @property
    def archive_dir(self) -> Path:
        return self.state_dir / "config"

    @property
    def baselines_file(self) -> Path:
        return self.state_dir / "calibration_baselines.json"

    @property
    def live_marker(self) -> Path:
        return self.state_dir / "tier0.live"


def _path(base: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise Tier0ConfigError(f"{name} 는 경로 문자열이다")
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _int(doc: Mapping[str, Any], key: str, default: int, minimum: int = 0) -> int:
    value = doc.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Tier0ConfigError(f"{key} 는 {minimum} 이상 정수다")
    return value


def _lifecycle(doc: Mapping[str, Any] | None) -> LifecyclePolicy:
    if not doc:
        return LifecyclePolicy()
    kwargs: dict[str, Any] = {}
    if "clear_hold_ms" in doc:
        kwargs["clear_hold_ms"] = _int(doc, "clear_hold_ms", 3000)
    if "persistence" in doc:
        pair = doc["persistence"]
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, int) for v in pair)):
            raise Tier0ConfigError("lifecycle.persistence 는 [min_frames, window_frames] 다")
        kwargs["persistence"] = Persistence(pair[0], pair[1])
    if "resume_window_ms" in doc:
        value = doc["resume_window_ms"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise Tier0ConfigError("lifecycle.resume_window_ms 는 0 이상 정수 또는 null 이다")
        kwargs["resume_window_ms"] = value
    try:
        return LifecyclePolicy(**kwargs)
    except ValueError as e:
        raise Tier0ConfigError(f"lifecycle: {e}") from None


def _control(env: Mapping[str, str]) -> ControlSettings | None:
    requested = (env.get("SIMAI_TIER0_CONTROL_PORT") or "").strip()
    if not requested:
        return None
    problems = []
    raw = (env.get(f"RUNPOD_TCP_PORT_{requested}") or "").strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 65535:
        problems.append(f"RUNPOD_TCP_PORT_{requested}")
    cert = (env.get("SIMAI_TIER0_TLS_CERT_FILE") or "").strip()
    key = (env.get("SIMAI_TIER0_TLS_KEY_FILE") or "").strip()
    tokens = (env.get("SIMAI_LAPTOP_TOKEN_SHA256") or "").strip()
    for name, value in (("SIMAI_TIER0_TLS_CERT_FILE", cert), ("SIMAI_TIER0_TLS_KEY_FILE", key),
                        ("SIMAI_LAPTOP_TOKEN_SHA256", tokens)):
        if not value:
            problems.append(name)
    if problems:
        raise Tier0ConfigError("I0 제어 채널 설정이 모자라다: " + ", ".join(problems))
    host = (env.get("SIMAI_TIER0_CONTROL_LISTEN_HOST") or "0.0.0.0").strip()
    return ControlSettings(host=host, port=int(raw), cert_file=cert, key_file=key, token_hashes=tokens)


def _i1(env: Mapping[str, str]) -> I1Settings | None:
    if not (env.get("SIMAI_EC2_I1_URL") or "").strip() and not (env.get("SIMAI_EC2_BASE_URL") or "").strip():
        return None
    try:
        return I1Settings.from_env(env)
    except I1ConfigError as e:
        raise Tier0ConfigError(f"I1: {e}") from None


def load_settings(config_path: Path | None, env: Mapping[str, str] | None = None) -> Tier0Settings:
    env = dict(os.environ if env is None else env)
    doc: dict[str, Any] = {}
    base = Path.cwd()
    if config_path is not None:
        try:
            doc = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Tier0ConfigError(f"설정 파일을 읽지 못했다: {config_path} ({type(e).__name__})") from None
        if not isinstance(doc, dict):
            raise Tier0ConfigError("설정 파일은 JSON 객체다")
        base = Path(config_path).resolve().parent
    gateway_id = (env.get("SIMAI_TIER0_ID") or doc.get("gateway_id") or DEFAULT_TIER0_ID).strip()
    root = Path(env.get("SIMAI_ROOT") or DEFAULT_ROOT)
    state_dir = _path(base, doc["state_dir"], "state_dir") if "state_dir" in doc else root / "state"
    scene_version = doc.get("scene_version", "scene-0")
    if not isinstance(scene_version, str) or not scene_version:
        raise Tier0ConfigError("scene_version 은 비어 있지 않은 문자열이다")

    input_doc = doc.get("input") or {"mode": "live"}
    mode = input_doc.get("mode")
    sim = None
    camera_ids: tuple[str, ...] = ()
    if mode == "sim":
        if "run_dir" not in input_doc:
            raise Tier0ConfigError("input.run_dir(SIM 출력 디렉터리)가 없다")
        anchor = input_doc.get("anchor_wall_ms")
        if anchor is not None and (isinstance(anchor, bool) or not isinstance(anchor, int) or anchor < 0):
            raise Tier0ConfigError("input.anchor_wall_ms 는 UTC ms 정수다")
        sim = SimInput(
            run_dir=_path(base, input_doc["run_dir"], "input.run_dir"),
            anchor_wall_ms=anchor,
            arrival_lag_ms=_int(input_doc, "arrival_lag_ms", 60),
            max_backlog_ms=_int(input_doc, "max_backlog_ms", 500, minimum=1),
        )
    elif mode == "live":
        ids = input_doc.get("camera_ids", ["cam1", "cam2", "cam3", "cam4"])
        if not (isinstance(ids, list) and ids and all(isinstance(c, str) and c for c in ids)):
            raise Tier0ConfigError("input.camera_ids 는 카메라 ID 목록이다")
        camera_ids = tuple(ids)
    else:
        raise Tier0ConfigError(f"input.mode 는 sim 또는 live 다: {mode!r}")

    if "calibration_file" in doc:
        calibration_file = _path(base, doc["calibration_file"], "calibration_file")
    elif sim is not None:
        calibration_file = sim.run_dir / "calibration.json"
    else:
        raise Tier0ConfigError("calibration_file 이 없다(LIVE 는 보정 문서 경로가 필요하다)")

    bootstrap = doc.get("bootstrap") or {}
    archive = _path(base, bootstrap["archive"], "bootstrap.archive") if "archive" in bootstrap else None
    version = bootstrap.get("config_version")
    if version is not None and (not isinstance(version, str) or not version):
        raise Tier0ConfigError("bootstrap.config_version 은 비어 있지 않은 문자열이다")
    if version is not None and archive is None:
        archive = state_dir / "config"

    checkpoint_interval_s = doc.get("checkpoint_interval_s", 1.0)
    drain_s = doc.get("drain_s", 5.0)
    for name, value in (("checkpoint_interval_s", checkpoint_interval_s), ("drain_s", drain_s)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise Tier0ConfigError(f"{name} 는 양수다")
    engine = (env.get("SIMAI_TIER0_ENGINE") or "").strip() or None
    return Tier0Settings(
        gateway_id=gateway_id,
        state_dir=state_dir,
        scene_version=scene_version,
        input_mode=mode,
        calibration_file=calibration_file,
        sim=sim,
        camera_ids=camera_ids,
        bootstrap_archive=archive,
        bootstrap_config_version=version,
        lifecycle=_lifecycle(doc.get("lifecycle")),
        tick_ms=_int(doc, "tick_ms", 100, minimum=1),
        checkpoint_interval_s=float(checkpoint_interval_s),
        drain_s=float(drain_s),
        engine_path=engine,
        i1=_i1({**env, "SIMAI_TIER0_ID": gateway_id}),
        control=_control(env),
        env=env,
    )
