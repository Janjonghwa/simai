"""Tier 0 서비스 설정: 설정 파일(JSON, --config) + 환경 변수(.env, 배포·비밀 값).

설정 파일 (모든 키 선택, 경로는 설정 파일 위치 기준 상대 경로도 된다):

    {
      "gateway_id": "tier0-01",               봉투 source_id·이벤트 gateway_id (환경 변수 SIMAI_TIER0_ID 가 우선)
      "state_dir": "/workspace/simai/state",   Network Volume 상태 루트 (기본 $SIMAI_ROOT/state)
      "scene_version": "scene-0",              world_state 초기 Scene 참조 (I1 scene_reference 수신 뒤 EC2 현재 Scene을 따름)
      "calibration_file": "calibration.json",  보정 문서(contracts/providers/calibration). SIM 은 기본 run_dir/calibration.json,
                                               LIVE·file 은 필수(외부 파라미터 포함, 없으면 시작하지 않는다)
      "input": {"mode": "sim", "run_dir": "...", "anchor_wall_ms": 1760342400000, "arrival_lag_ms": 60},
               {"mode": "live", "camera_ids": ["cam1", "cam2", "cam3", "cam4"],
                "decoder": "cpu",                    cpu(기본, PyAV CPU) | nvdec(선택). nvdec 을 골랐는데 NVDEC 이 없으면 시작하지 않는다
                "max_queue_wait_ms": 150, "queue_depth": 1}   프레임 최신성 정책(명세 5.1 초기값)
               {"mode": "file", "session_dir": "recordings/rec-...",   녹화 재생(gateway/recorder 세션, S15P31A101-215)
                "camera_ids": [...],                 생략하면 manifest 의 모든 카메라
                "speed": "max",                      "max"(기본, 판정 tick 마다 tick_ms 씩, 결과가 매번 같다) | 배속(1.0 = 실시간)
                "jsonl_dir": "out/run-1",            있으면 world_state.jsonl·risk_event.jsonl 을 쓴다(있는 파일은 덮어쓰지 않음)
                "decoder": "cpu", "max_queue_wait_ms": 150, "queue_depth": 1}   LIVE 와 같다
               file 입력은 판정 출처가 REPLAY 이고 I1·I0 제어 채널을 열지 않는다(환경 변수가 있어도). 상태(저널·체크포인트·
               표시 파일)는 state_dir/replay/<실행 이름>/ 에 따로 둔다(LIVE 상태와 섞이지 않음). 구조는 docs/design/tier0-recording-replay.md
               sim 입력은 합성 장면인데 판정 출처가 LIVE 다(로컬 e2e·G3 시험이 이 경로를 쓴다). 그래서 I1 주소와 제어 채널 듣는
               주소가 루프백(127.0.0.0/8, ::1, localhost)일 때만 연다. 운영 .env 가 남아 EC2·노트북 경보로 나갈 주소면 시작하지
               않는다(AGENTS 규칙 5, S15P31A101-599, docs/design/S15P31A101-599-sim-origin.md)
               모든 입력에 "roi_filter": {"enabled": true, "margin_m": 3.0, "min_people": 4} 를 둘 수 있다(활성 구역이 없거나 한 카메라 최대 인원이 min_people 미만이면 통과).
      "bootstrap": {"archive": "...", "config_version": "cfg-1"},   체크포인트의 묶음을 보관소에서 찾지 못할 때 쓸 시작 묶음
      "lifecycle": {"clear_hold_ms": 3000, "persistence": [2, 3], "resume_window_ms": 10000},
      "tick_ms": 100,                          판정 주기 (10 Hz, 명세 12.2 world_state)
      "detect_tick_ms": 50,                    LIVE 검출·Local Tracker 루프 목표 주기 (20 Hz, 명세 5.1 카메라당 12 FPS 이상, -383)
      "detect_queue_depth": 8,                 검출 루프 → 판정 루프 사이 카메라별 최대 관측 대기 수 (초과 시 오래된 관측 버림, -383)
      "drift_watch_enabled": true,             사람 관절 보정 활성 시 틀어짐 감시(기본 true)
      "event_frames_enabled": true,            CRITICAL R1~R4 발생 프레임 비동기 업로드(기본 켜짐, LIVE 입력·I1 필요)
      "checkpoint_interval_s": 1.0,            체크포인트 주기 (명세 3.2 초기값 1초)
      "drain_s": 5.0,                          종료 때 저널 쓰기·스레드 정리를 기다리는 상한
      "gpu": {"reserve_mb": 8192, "device": 0, "lag_budget_ms": 600}
                                               GPU 공유(명세 3.2, simai_ai.gpu): torch 선확보 크기(0 이면 끔, torch CUDA 가
                                               없으면 건너뜀), 장치 번호, 분석 워커 접수 문에 싣는 지연 예산(6.6 capture_to_risk)
    }

환경 변수:

    SIMAI_ROOT                       Network Volume 루트 (기본 /workspace/simai). state_dir 기본값의 부모
    SIMAI_TIER0_HUMAN_CALIBRATION    사람 관절 보정 감시·후보 경로 (기본 0, 1이면 켬)
    SIMAI_TIER0_ID                   Tier 0 인스턴스 ID (기본 tier0-01)
    SIMAI_TIER0_ENGINE               YOLO26x-pose TensorRT 엔진 경로 (deploy/tier0/entrypoint.sh 가 넣는다, LIVE 입력)
    SIMAI_TIER0_WEIGHTS              엔진이 없을 때 ultralytics 로 여는 가중치(.pt) 경로 (느리다, 경고를 남긴다)
    SIMAI_TIER0_IMGSZ                검출 입력 크기 (기본 1280, 엔진을 만든 값과 같아야 한다)
    SIMAI_TIER0_VIDEO_PORTS, SIMAI_PUBLIC_PORT_*·RUNPOD_TCP_PORT_*, SIMAI_TIER0_VIDEO_LISTEN_HOST
                                     LIVE I0 영상 포트 (video_ingest.listen_specs_from_env). TLS·토큰은 제어 채널과 같은 값
    SIMAI_TIER0_GPU_RESERVE_MB       GPU 선확보 크기 (설정 파일 gpu.reserve_mb 보다 우선, 0 이면 끔)
    SIMAI_EC2_I1_URL / SIMAI_EC2_BASE_URL (+ SIMAI_EC2_I1_PATH), SIMAI_TIER0_TOKEN, SIMAI_EC2_CA_FILE
                                     I1 접속 (i1_client.I1Settings). 둘 다 없으면 EC2 없이 돈다(오프라인, health EC2_LINK_LOST)
    SIMAI_TIER0_CONTROL_PORT         I0 제어 채널(wss) 대칭 매핑 요청 번호. 실제 포트는 SIMAI_PUBLIC_PORT_<요청 번호>(없으면 RUNPOD_TCP_PORT_<요청 번호>).
                                     없으면 노트북 없이 돈다(health GATEWAY_LINK_LOST)
    SIMAI_TIER0_CONTROL_LISTEN_HOST  제어 채널 듣는 주소 (기본 0.0.0.0)
    SIMAI_TIER0_TLS_CERT_FILE, SIMAI_TIER0_TLS_KEY_FILE   Tier 0 TLS 서버 인증서·키 (I0 영상·제어 공용)
    SIMAI_LAPTOP_TOKEN_SHA256        노트북 기기 토큰 sha256 지문 목록
    엔드포인트 등록(endpoint_registration): SIMAI_PUBLIC_HOST·RUNPOD_PUBLIC_IP, SIMAI_TIER0_VIDEO_PORTS, RUNPOD_TCP_PORT_* 등

값을 모르면 지어내지 않는다. 필요한 값이 빠지면 Tier0ConfigError 로 시작하지 않는다(어떤 값이 없는지 이름만 적는다).
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.tier0.endpoint_registration import DEFAULT_TIER0_ID, public_port_env
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


DECODERS = ("cpu", "nvdec")
DEFAULT_DECODER = "cpu"  # Pod 실측 CPU 가 NVDEC 보다 빠르고 GPU 를 검출·분석과 나누지 않는다(docs/design/tier0-nvdec-throughput.md)


@dataclass(frozen=True)
class LiveInputSettings:
    decoder: str = DEFAULT_DECODER
    max_queue_wait_ms: int = 150
    queue_depth: int = 1


@dataclass(frozen=True)
class FileInputSettings:
    """녹화 재생 입력(file). speed None = 최대 속도."""

    session_dir: Path
    camera_ids: tuple[str, ...] | None = None
    speed: float | None = None
    jsonl_dir: Path | None = None
    decoder: str = DEFAULT_DECODER
    max_queue_wait_ms: int = 150
    queue_depth: int = 1


@dataclass(frozen=True)
class ControlSettings:
    host: str
    port: int
    cert_file: str
    key_file: str
    token_hashes: str = field(repr=False)


@dataclass(frozen=True)
class LiveCalibrationSettings:
    enabled: bool = True
    interval_ms: int = 60_000
    timeout_s: float = 180.0
    buffer_duration_ms: int = 300_000
    measured_camera_heights: Mapping[str, float] = field(default_factory=dict)
    safety_enabled: bool = True


@dataclass(frozen=True)
class CleanBackgroundSettings:
    enabled: bool = True
    offload: bool = True
    output_dir: Path | None = None  # None: state_dir/clean-background
    sample_interval_ms: int = 5_000
    min_samples_per_camera: int = 20
    max_samples_per_camera: int = 60
    max_bytes: int = 2_200 * 1024 * 1024
    queue_depth: int = 4
    max_pending_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class RotationRecoverySettings:
    enabled: bool = True
    safety_enabled: bool = True
    interval_ms: int = 15_000
    cpu_budget_ms: int = 500
    thresholds: Mapping[str, float | int] = field(default_factory=dict)

    @property
    def config(self):
        from simai_ai.calibration.rotation_recovery import RotationConfig

        return RotationConfig(**self.thresholds)


@dataclass(frozen=True)
class ReidSettings:
    """Tier 0 비동기 Re-ID 보조 경로 설정 (S15P31A101-463)."""

    enabled: bool = True
    batch_size: int = 8
    update_interval_ms: int = 750
    stable_update_interval_ms: int = 3000
    max_crops_per_second: int = 8
    max_queue_size: int = 32
    cope_root: Path | None = None
    weights_path: Path | None = None
    clip_model_path: Path | None = None
    use_face_aux: bool = False
    """얼굴 보조 점수(buffalo_l, SIMAI_FACE_WEIGHTS_DIR). 기본 끔 — 12차 켬/끔 비교 뒤 결정(S15P31A101-471)"""


@dataclass(frozen=True)
class RoiFilterSettings:
    enabled: bool = True
    margin_m: float = 3.0
    min_people: int = 4


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
    detect_tick_ms: int = 50
    detect_queue_depth: int = 8
    checkpoint_interval_s: float = 1.0
    drain_s: float = 5.0
    engine_path: str | None = None
    weights_path: str | None = None
    imgsz: int = 1280
    live: LiveInputSettings | None = None
    file: FileInputSettings | None = None
    gpu_reserve_mb: int = 8192  # G0 실측 전 초기값 (simai_ai.gpu.budget 예산표)
    gpu_device: int = 0
    lag_budget_ms: int = 600
    i1: I1Settings | None = None
    control: ControlSettings | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    human_calibration_enabled: bool = False
    drift_watch_enabled: bool = True
    event_frames_enabled: bool = True
    live_calibration: LiveCalibrationSettings = field(default_factory=LiveCalibrationSettings)
    clean_background: CleanBackgroundSettings = field(default_factory=CleanBackgroundSettings)
    rotation_recovery: RotationRecoverySettings = field(default_factory=RotationRecoverySettings)
    reid: ReidSettings = field(default_factory=ReidSettings)
    roi_filter: RoiFilterSettings = field(default_factory=RoiFilterSettings)

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


def _live_calibration(doc: Any) -> LiveCalibrationSettings:
    import math

    if not isinstance(doc, dict):
        raise Tier0ConfigError("live_calibration 는 객체다")
    enabled = doc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise Tier0ConfigError("live_calibration.enabled 는 bool이다")
    safety_enabled = doc.get("safety_enabled", True)
    if not isinstance(safety_enabled, bool):
        raise Tier0ConfigError("live_calibration.safety_enabled 는 bool이다")
    timeout = doc.get("timeout_s", 180.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise Tier0ConfigError("live_calibration.timeout_s 는 유한한 양수다")
    heights = doc.get("measured_camera_heights", {})
    if not isinstance(heights, dict) or any(
        not isinstance(cid, str) or not cid or isinstance(h, bool) or not isinstance(h, (int, float))
        or not math.isfinite(h) or h <= 0 for cid, h in heights.items()
    ):
        raise Tier0ConfigError("live_calibration.measured_camera_heights 는 카메라 ID별 실측 높이(m)다")
    return LiveCalibrationSettings(enabled, _int(doc, "interval_ms", 60_000, minimum=1), float(timeout),
                                   _int(doc, "buffer_duration_ms", 300_000, minimum=1_000), dict(heights), safety_enabled)


def _clean_background(doc: Any, base: Path) -> CleanBackgroundSettings:
    if not isinstance(doc, dict):
        raise Tier0ConfigError("clean_background 는 객체다")
    enabled = doc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise Tier0ConfigError("clean_background.enabled 는 bool이다")
    offload = doc.get("offload", True)
    if not isinstance(offload, bool):
        raise Tier0ConfigError("clean_background.offload 는 bool이다")
    values = {key: _int(doc, key, default, minimum=1) for key, default in (
        ("sample_interval_ms", 5_000), ("min_samples_per_camera", 20),
        ("max_samples_per_camera", 60), ("max_bytes", 2_200 * 1024 * 1024),
        ("queue_depth", 4), ("max_pending_bytes", 64 * 1024 * 1024))}
    if values["min_samples_per_camera"] > values["max_samples_per_camera"]:
        raise Tier0ConfigError("clean_background 최소 표본 수가 최대 표본 수보다 크다")
    output = _path(base, doc["output_dir"], "clean_background.output_dir") if "output_dir" in doc else None
    return CleanBackgroundSettings(enabled=enabled, offload=offload, output_dir=output, **values)


def _rotation_recovery(doc: Any) -> RotationRecoverySettings:
    from simai_ai.calibration.rotation_recovery import RotationConfig

    if not isinstance(doc, dict):
        raise Tier0ConfigError("rotation_recovery 는 객체다")
    enabled = doc.get("enabled", True)
    safety_enabled = doc.get("safety_enabled", True)
    if not isinstance(enabled, bool) or not isinstance(safety_enabled, bool):
        raise Tier0ConfigError("rotation_recovery.enabled/safety_enabled 는 bool이다")
    nested = doc.get("thresholds", {})
    if not isinstance(nested, dict):
        raise Tier0ConfigError("rotation_recovery.thresholds 는 객체다")
    fields = set(RotationConfig.__dataclass_fields__)
    unknown = (set(doc) - {"enabled", "safety_enabled", "interval_ms", "cpu_budget_ms", "thresholds"} - fields) | (
        set(nested) - fields
    )
    if unknown:
        raise Tier0ConfigError(f"rotation_recovery 알 수 없는 설정: {sorted(unknown)}")
    thresholds = {k: doc[k] for k in fields if k in doc} | dict(nested)
    try:
        RotationConfig(**thresholds)
    except (TypeError, ValueError) as e:
        raise Tier0ConfigError(f"rotation_recovery: {e}") from None
    return RotationRecoverySettings(
        enabled=enabled,
        safety_enabled=safety_enabled,
        interval_ms=_int(doc, "interval_ms", 15_000, minimum=1),
        cpu_budget_ms=_int(doc, "cpu_budget_ms", 500, minimum=1),
        thresholds=thresholds,
    )


def _reid(doc: Any, base: Path, env: Mapping[str, str]) -> ReidSettings:
    if not isinstance(doc, dict):
        raise Tier0ConfigError("reid 는 객체다")
    enabled = doc.get("enabled", True)
    if not isinstance(enabled, bool):
        raise Tier0ConfigError("reid.enabled 는 bool이다")
    env_enabled = (env.get("SIMAI_TIER0_REID_ENABLED") or "").strip()
    if env_enabled:
        if env_enabled not in ("0", "1"):
            raise Tier0ConfigError("SIMAI_TIER0_REID_ENABLED 은 0 또는 1이다")
        enabled = env_enabled == "1"
    batch_size = _int(doc, "batch_size", 8, minimum=1)
    update_interval_ms = _int(doc, "update_interval_ms", 750, minimum=0)
    stable_update_interval_ms = _int(doc, "stable_update_interval_ms", 3000, minimum=update_interval_ms)
    max_crops_per_second = _int(doc, "max_crops_per_second", 8, minimum=1)
    max_queue_size = _int(doc, "max_queue_size", 32, minimum=1)
    # 설정 파일에 없으면 환경변수(Pod 템플릿·세션 준비 스크립트가 넣는다). 둘 다 없으면 None(임베더 기본값, S15P31A101-471)
    def _reid_path(key: str, env_key: str) -> Path | None:
        if key in doc:
            return _path(base, doc[key], f"reid.{key}")
        raw = (env.get(env_key) or "").strip()
        return _path(base, raw, env_key) if raw else None

    use_face_aux = doc.get("use_face_aux", False)
    if not isinstance(use_face_aux, bool):
        raise Tier0ConfigError("reid.use_face_aux 는 bool이다")
    env_face = (env.get("SIMAI_TIER0_REID_FACE") or "").strip()
    if env_face:
        if env_face not in ("0", "1"):
            raise Tier0ConfigError("SIMAI_TIER0_REID_FACE 은 0 또는 1이다")
        use_face_aux = env_face == "1"
    cope_root = _reid_path("cope_root", "SIMAI_COPE_ROOT")
    weights_path = _reid_path("weights_path", "SIMAI_COPE_WEIGHTS")
    clip_model_path = _reid_path("clip_model_path", "SIMAI_COPE_CLIP_PATH")
    return ReidSettings(
        enabled=enabled,
        batch_size=batch_size,
        update_interval_ms=update_interval_ms,
        stable_update_interval_ms=stable_update_interval_ms,
        max_crops_per_second=max_crops_per_second,
        max_queue_size=max_queue_size,
        cope_root=cope_root,
        weights_path=weights_path,
        clip_model_path=clip_model_path,
        use_face_aux=use_face_aux,
    )


def _control(env: Mapping[str, str]) -> ControlSettings | None:
    requested = (env.get("SIMAI_TIER0_CONTROL_PORT") or "").strip()
    if not requested:
        return None
    problems = []
    port_name, raw = public_port_env(env, requested)
    if not raw.isdigit() or not 0 <= int(raw) <= 65535:
        problems.append(port_name)
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


def _is_loopback(host: str | None) -> bool:
    """루프백 주소(127.0.0.0/8, ::1) 또는 localhost 인가. 0.0.0.0·빈 값·다른 이름은 루프백이 아니다."""
    if not host:
        return False
    host = host.strip().strip("[]").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sim_channels_local_only(i1: I1Settings | None, control: ControlSettings | None) -> None:
    """SIM 입력은 판정 출처가 LIVE 라(로컬 시험) 실제 알림 채널로 새지 않게 I1·제어 채널을 루프백으로만 연다(AGENTS 규칙 5).

    운영 .env(EC2 I1 주소, 제어 채널 기본 0.0.0.0)가 남은 채 SIM 을 띄우면 합성 이벤트가 노트북 현장 경보·EC2·워치로
    LIVE 로 나간다. 그런 설정은 시작하지 않는다(S15P31A101-599).
    """
    problems = []
    if i1 is not None and not _is_loopback(urllib.parse.urlsplit(i1.url).hostname):
        problems.append("I1 주소(SIMAI_EC2_I1_URL·SIMAI_EC2_BASE_URL)")
    if control is not None and not _is_loopback(control.host):
        problems.append(f"제어 채널 듣는 주소(SIMAI_TIER0_CONTROL_LISTEN_HOST={control.host})")
    if problems:
        raise Tier0ConfigError(
            "SIM 입력은 합성 이벤트를 LIVE 출처로 만들므로 I1·노트북 제어 채널을 루프백(127.0.0.1·::1·localhost)으로만 연다: "
            + ", ".join(problems) + " 가 루프백이 아니다. 운영 .env 의 EC2·제어 채널 값을 빼거나 루프백 시험 상대를 쓴다"
            " (AGENTS 규칙 5, docs/design/S15P31A101-599-sim-origin.md)")


def _camera_ids(input_doc: Mapping[str, Any], default: list[str] | None) -> tuple[str, ...] | None:
    ids = input_doc.get("camera_ids", default)
    if ids is None:
        return None
    if not (isinstance(ids, list) and ids and all(isinstance(c, str) and c for c in ids)):
        raise Tier0ConfigError("input.camera_ids 는 카메라 ID 목록이다")
    if len(set(ids)) != len(ids):
        raise Tier0ConfigError("input.camera_ids 에 같은 카메라가 두 번 있다")
    return tuple(ids)


def _freshness(input_doc: Mapping[str, Any]) -> tuple[str, int, int]:
    decoder = input_doc.get("decoder", DEFAULT_DECODER)
    if decoder not in DECODERS:
        raise Tier0ConfigError(f"input.decoder 는 {' 또는 '.join(DECODERS)} 다")
    depth = _int(input_doc, "queue_depth", 1, minimum=1)
    if depth > 2:
        raise Tier0ConfigError("input.queue_depth 는 1 또는 2 다 (명세 5.1)")
    return decoder, _int(input_doc, "max_queue_wait_ms", 150), depth


def _file_input(base: Path, input_doc: Mapping[str, Any]) -> FileInputSettings:
    if "session_dir" not in input_doc:
        raise Tier0ConfigError("input.session_dir(녹화 세션 디렉터리)가 없다")
    speed = input_doc.get("speed", "max")
    if speed == "max":
        speed = None
    elif isinstance(speed, bool) or not isinstance(speed, (int, float)) or not speed > 0:
        raise Tier0ConfigError('input.speed 는 "max" 또는 양수 배속이다')
    jsonl = input_doc.get("jsonl_dir")
    decoder, wait, depth = _freshness(input_doc)
    return FileInputSettings(
        session_dir=_path(base, input_doc["session_dir"], "input.session_dir"),
        camera_ids=_camera_ids(input_doc, None),
        speed=None if speed is None else float(speed),
        jsonl_dir=None if jsonl is None else _path(base, jsonl, "input.jsonl_dir"),
        decoder=decoder, max_queue_wait_ms=wait, queue_depth=depth,
    )


def _replay_run_name() -> str:
    """재생 상태 디렉터리 이름. 실행마다 새로 만들어 이전 재생의 체크포인트(Track)를 되살리지 않는다(결과가 매번 같게)."""
    return f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


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
    if not isinstance(input_doc, dict):
        raise Tier0ConfigError("input 은 객체다")
    roi = input_doc.get("roi_filter", {})
    if not isinstance(roi, dict) or set(roi) - {"enabled", "margin_m", "min_people"}:
        raise Tier0ConfigError("input.roi_filter 는 enabled, margin_m, min_people 객체다")
    enabled, margin = roi.get("enabled", True), roi.get("margin_m", 3.0)
    min_people = roi.get("min_people", 4)
    if isinstance(min_people, bool) or not isinstance(min_people, int) or min_people < 0:
        raise Tier0ConfigError("input.roi_filter.min_people 은 0 이상 정수다")
    if not isinstance(enabled, bool):
        raise Tier0ConfigError("input.roi_filter.enabled 는 bool이다")
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < 0:
        raise Tier0ConfigError("input.roi_filter.margin_m 은 유한한 0 이상 수다")
    mode = input_doc.get("mode")
    sim = None
    camera_ids: tuple[str, ...] = ()
    live: LiveInputSettings | None = None
    file: FileInputSettings | None = None
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
        camera_ids = _camera_ids(input_doc, ["cam1", "cam2", "cam3", "cam4"])  # type: ignore[assignment]
        decoder, wait, depth = _freshness(input_doc)
        live = LiveInputSettings(decoder=decoder, max_queue_wait_ms=wait, queue_depth=depth)
    elif mode == "file":
        file = _file_input(base, input_doc)
        state_dir = state_dir / "replay" / _replay_run_name()  # LIVE 저널·체크포인트·표시 파일과 섞이지 않게
    else:
        raise Tier0ConfigError(f"input.mode 는 sim, live, file 중 하나다: {mode!r}")

    if "calibration_file" in doc:
        calibration_file = _path(base, doc["calibration_file"], "calibration_file")
    elif sim is not None:
        calibration_file = sim.run_dir / "calibration.json"
    else:
        raise Tier0ConfigError(f"calibration_file 이 없다({mode} 입력은 보정 문서 경로가 필요하다)")

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
    weights = (env.get("SIMAI_TIER0_WEIGHTS") or "").strip() or None
    imgsz_raw = (env.get("SIMAI_TIER0_IMGSZ") or "1280").strip()
    if not imgsz_raw.isdigit() or int(imgsz_raw) < 32:
        raise Tier0ConfigError("SIMAI_TIER0_IMGSZ 는 32 이상 정수다")
    gpu = doc.get("gpu") or {}
    if not isinstance(gpu, dict):
        raise Tier0ConfigError("gpu 는 객체다")
    reserve_env = (env.get("SIMAI_TIER0_GPU_RESERVE_MB") or "").strip()
    if reserve_env:
        if not reserve_env.isdigit():
            raise Tier0ConfigError("SIMAI_TIER0_GPU_RESERVE_MB 는 0 이상 정수다")
        gpu = {**gpu, "reserve_mb": int(reserve_env)}
    human_calibration = (env.get("SIMAI_TIER0_HUMAN_CALIBRATION") or "0").strip()
    if human_calibration not in ("0", "1"):
        raise Tier0ConfigError("SIMAI_TIER0_HUMAN_CALIBRATION 은 0 또는 1이다")
    drift_watch_enabled = doc.get("drift_watch_enabled", True)
    if not isinstance(drift_watch_enabled, bool):
        raise Tier0ConfigError("drift_watch_enabled 는 bool이다")
    event_frames_enabled = doc.get("event_frames_enabled", True)
    if not isinstance(event_frames_enabled, bool):
        raise Tier0ConfigError("event_frames_enabled 는 bool이다")
    i1 = None if file is not None else _i1({**env, "SIMAI_TIER0_ID": gateway_id})
    control = None if file is not None else _control(env)
    if sim is not None:
        _sim_channels_local_only(i1, control)
    return Tier0Settings(
        gateway_id=gateway_id,
        state_dir=state_dir,
        scene_version=scene_version,
        input_mode=mode,
        calibration_file=calibration_file,
        human_calibration_enabled=human_calibration == "1",
        drift_watch_enabled=drift_watch_enabled,
        event_frames_enabled=event_frames_enabled,
        live_calibration=_live_calibration(doc.get("live_calibration", {})),
        clean_background=_clean_background(doc.get("clean_background", {}), base),
        rotation_recovery=_rotation_recovery(doc.get("rotation_recovery", {})),
        reid=_reid(doc.get("reid", {}), base, env),
        roi_filter=RoiFilterSettings(enabled, float(margin), min_people),
        sim=sim,
        camera_ids=camera_ids,
        bootstrap_archive=archive,
        bootstrap_config_version=version,
        lifecycle=_lifecycle(doc.get("lifecycle")),
        tick_ms=_int(doc, "tick_ms", 100, minimum=1),
        detect_tick_ms=_int(doc, "detect_tick_ms", 50, minimum=1),
        detect_queue_depth=_int(doc, "detect_queue_depth", 8, minimum=1),
        checkpoint_interval_s=float(checkpoint_interval_s),
        drain_s=float(drain_s),
        engine_path=engine,
        weights_path=weights,
        imgsz=int(imgsz_raw),
        live=live,
        file=file,
        gpu_reserve_mb=_int(gpu, "reserve_mb", 8192),
        gpu_device=_int(gpu, "device", 0),
        lag_budget_ms=_int(gpu, "lag_budget_ms", 600, minimum=1),
        # file 입력(REPLAY)은 실제 알림 채널(I1·노트북 제어 채널)을 열지 않는다(AGENTS 규칙 5). SIM 은 루프백만 연다(-599)
        i1=i1,
        control=control,
        env=env,
    )
