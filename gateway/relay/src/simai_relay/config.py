"""중계 설정(TOML)과 카메라 계정(.env) 읽기, 로그의 비밀 값 가리기.

설정 형식은 녹화 도구 세션 설정의 [capture_time]·[[cameras]] 와 같다(입력 만들기는 simai_recorder.config.build_source
를 그대로 쓴다). [session]·[consent] 같은 녹화 전용 절은 읽지 않는다(중계는 영상을 저장하지 않는다).
카메라 계정은 저장소 루트 .env 의 RTSP_USER / RTSP_PASS(카메라별 RTSP_USER_CAM_A …)에서만 읽는다.

Tier 0 전송(S15P31A101-83) 설정도 같은 .env 에서 읽는다(현장 경보 단말 gateway/site_alert 와 같은 이름):

    SIMAI_LAPTOP_ID                  노트북 기기 ID (HELLO laptop_id). 기본 laptop-01
    SIMAI_EC2_BASE_URL               EC2 주소. Tier 0 공개 IP·카메라별 영상 포트는 여기서 조회한다(GET /api/endpoints/tier0)
    SIMAI_DEVICE_TOKEN               노트북 기기 토큰 (비밀. EC2 조회와 Tier 0 영상 연결 인증)
    SIMAI_TIER0_PINNED_SPKI_SHA256   미리 고정한 Tier 0 TLS 공개키 지문 sha256:<hex 64> (키 교체 기간에는 쉼표로 여러 개)
    SIMAI_TIER0_CA_FILE              팀 사설 CA 인증서 파일(선택)
"""

from __future__ import annotations

import logging
import os
import tomllib
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import Clock
from simai_recorder.config import ConfigError, build_source, load_env
from simai_recorder.recorder import CameraSpec
from simai_recorder.segment import SegmentPaths, camera_segments
from simai_recorder.sources import FileSource

from simai_relay.replay_sidecar import (
    TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS,
    ReplayAnchor,
    Sidecar,
    SidecarError,
    SidecarFileSource,
    default_sidecar_paths,
    load_sidecar,
    replay_offsets,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

log = logging.getLogger("simai_relay")

__all__ = ["DEFAULT_ENV_FILE", "ConfigError", "RedactSecrets", "RemoteCctvSettings", "Tier0Settings", "load_cameras",
           "load_env", "load_remote_cctv_settings", "load_tier0_settings", "secret_values"]

SECRET_ENV_KEYS = ("SIMAI_DEVICE_TOKEN",)


def _resolve_camera_entry(c: Mapping[str, Any], config_dir: Path) -> Mapping[str, Any]:
    """kind = 'file' 의 file 이 녹화 세션 폴더나 카메라 폴더면 그 카메라의 세그먼트(.h264)로 풀어 준다."""
    if c.get("kind") != "file":
        return c
    file_val = c.get("file") or c.get("path")
    if not file_val:
        return c
    raw = Path(str(file_val))
    target = raw if raw.is_absolute() else config_dir / raw
    if not target.is_dir():
        return c if "file" in c else {**c, "file": str(file_val)}
    cid = str(c["camera_id"])
    cam_dir = target / cid if (target / cid).is_dir() else target
    segs = camera_segments(cam_dir)
    if "stream_epoch" in c:
        epoch = int(c["stream_epoch"])
        cand = SegmentPaths.of(cam_dir, epoch).h264
        if not cand.is_file():
            raise ConfigError(f"{cid}: stream_epoch {epoch} 세그먼트가 없다 ({cam_dir})")
        chosen = cand
    elif segs:
        chosen = segs[0].h264
    else:
        h264s = sorted(cam_dir.glob("epoch_*.h264"))
        if not h264s:
            raise ConfigError(f"{cid}: 녹화 세그먼트(.h264)가 없다 ({cam_dir})")
        chosen = h264s[0]
    return {**c, "file": str(chosen)}


def _file_path(resolved: Mapping[str, Any], config_dir: Path) -> Path:
    p = Path(str(resolved["file"]))
    return p if p.is_absolute() else config_dir / p


def _check_tier0_uncertainty(cid: str, resolved: Mapping[str, Any], model: CaptureTimeModel, config_dir: Path,
                             allow_over: bool) -> None:
    """파일 재생(fps 시각축)의 헤더 불확실성이 Tier 0 보간 허용치를 넘으면 기동을 멈춘다(-721). 넘으면 모든 view 가 버려져 사람
    0명이 조용히 나온다(17차). raw H.264 는 PTS 가 없어 uncertainty_no_pts_ms, 컨테이너(MP4·MKV)는 PTS 의 uncertainty_ms 를 본다."""
    raw = _file_path(resolved, config_dir).suffix.lower() in (".h264", ".264")
    name = "uncertainty_no_pts_ms" if raw else "uncertainty_ms"
    value = getattr(model, name)
    if value <= TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS:
        return
    msg = (f"{cid}: 파일 재생 {name} = {value:g} ms 가 Tier 0 보간 허용 {TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS:g} ms 를 넘는다. "
           "Tier 0 가 이 카메라의 모든 view 를 버려 사람이 0명으로 나온다. 공통 [capture_time] 의 RTSP 값(45)을 지우거나 "
           "timing = \"sidecar\" 를 쓴다. 일부러 넘기려면 [replay] allow_uncertainty_over_tier0 = true")
    if not allow_over:
        raise ConfigError(msg)
    log.warning("!!!!!!!! %s (allow_uncertainty_over_tier0 로 계속한다) !!!!!!!!", msg)


def load_cameras(path: Path, clock: Clock, env: Mapping[str, str]) -> list[CameraSpec]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cams = data.get("cameras", [])
    if not cams:
        raise ConfigError("[[cameras]] 가 없다")
    base = data.get("capture_time", {})
    replay = data.get("replay") or {}
    allow_over = bool(replay.get("allow_uncertainty_over_tier0", False))
    specs: list[CameraSpec | None] = []
    pending: dict[str, tuple[int, Mapping[str, Any], CaptureTimeModel, Sidecar]] = {}
    seen: set[str] = set()
    for c in cams:
        if not c.get("camera_id"):
            raise ConfigError("[[cameras]] 에 camera_id 가 없다")
        resolved = _resolve_camera_entry(c, path.parent)
        cid = str(resolved["camera_id"])
        if cid in seen:
            raise ConfigError("camera_id 가 겹친다")
        seen.add(cid)
        defaults = {}
        timing = None
        if resolved.get("kind") == "file":
            defaults["timestamp_source"] = "FILE"
            if resolved.get("realtime", True):
                # FileSource.read() 는 _pace() 뒤 반환한다. PTS 없음 시 수신 시각은 파일 페이싱 시각이고
                # RTSP 의 Wi-Fi 지터를 위한 45 ms 대신 정상 PTS 와 같은 잠정 20 ms 를 쓴다(-353).
                # 근거·한계: README 의 파일 재생 절. 명시 설정은 공통 → 카메라 순으로 우선한다.
                defaults["uncertainty_no_pts_ms"] = 20.0
            timing = resolved.get("timing", replay.get("timing", "fps"))
            if timing not in ("fps", "sidecar"):
                raise ConfigError(f"{cid}: timing 은 fps / sidecar 중 하나다 (받은 값 {timing!r})")
        model = CaptureTimeModel(**{**defaults, **base, **(resolved.get("capture_time") or {})})
        if timing == "sidecar":
            sidecar = _load_camera_sidecar(cid, resolved, clock, path.parent)
            if sidecar is not None:
                if "faults" in resolved:
                    raise ConfigError(f"{cid}: timing = \"sidecar\" 에는 faults 를 쓰지 않는다")
                pending[cid] = (len(specs), resolved, model, sidecar)
                specs.append(None)
                continue
        if timing is not None and resolved.get("realtime", True):
            _check_tier0_uncertainty(cid, resolved, model, path.parent, allow_over)
        specs.append(CameraSpec(camera_id=cid, source=build_source(resolved, clock, env, path.parent),
                                capture_time=model, firmware=resolved.get("firmware") or None))
    if pending:
        _build_sidecar_specs(pending, specs, clock, path.parent)
    return [s for s in specs if s is not None]


def _load_camera_sidecar(cid: str, resolved: Mapping[str, Any], clock: Clock, config_dir: Path) -> Sidecar | None:
    """timing = "sidecar" 카메라의 사이드카. 파일이 없으면 WARNING 후 None(기존 fps 재생으로 돈다). 깨졌으면 ConfigError."""
    if resolved.get("sidecar"):
        p = Path(str(resolved["sidecar"]))
        paths = [p if p.is_absolute() else config_dir / p]
    else:
        paths = default_sidecar_paths(FileSource(_file_path(resolved, config_dir), clock)._parts)
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        log.warning("!!!!!!!! %s sidecar_missing %s — 설정 fps 로 고르게 재생한다. 카메라 사이 촬영 시각이 분당 수십 ms 씩 "
                    "어긋날 수 있다 !!!!!!!!", cid, ", ".join(missing))
        return None
    try:
        return load_sidecar(paths)
    except SidecarError as e:
        raise ConfigError(f"{cid}: {e}") from None


def _build_sidecar_specs(pending: Mapping[str, tuple[int, Mapping[str, Any], CaptureTimeModel, Sidecar]],
                         specs: list[CameraSpec | None], clock: Clock, config_dir: Path) -> None:
    """사이드카 카메라들을 하나의 재생 시각축(공통 기준·순환 길이·원점)에 묶는다(replay_sidecar 모듈 설명)."""
    try:
        offsets, period, ref = replay_offsets({cid: p[3] for cid, p in pending.items()})
    except SidecarError as e:
        raise ConfigError(str(e)) from None
    anchor = ReplayAnchor(clock, period, ref)
    for cid, (index, resolved, model, sidecar) in pending.items():
        over = sum(1 for u in sidecar.uncertainty_ms if u > TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS)
        if over:
            share = over / len(sidecar)
            log.log(logging.WARNING if share > 0.01 else logging.INFO,
                    "%s sidecar_uncertainty_over_tier0 frames=%d share=%.4f (Tier 0 가 그 프레임 view 를 버린다, 녹화 원본 값 그대로)",
                    cid, over, share)
        log.info("%s replay_timing=sidecar frames=%d first_offset_ms=%d last_offset_ms=%d", cid, len(sidecar),
                 offsets[cid][0], offsets[cid][-1])
        source = SidecarFileSource(_file_path(resolved, config_dir), clock, sidecar, offsets[cid], anchor,
                                   realtime=bool(resolved.get("realtime", True)), loop=bool(resolved.get("loop", False)),
                                   fps=resolved.get("fps"), camera_id=cid)
        specs[index] = CameraSpec(camera_id=cid, source=source, capture_time=model, firmware=resolved.get("firmware") or None)


def secret_values(env: Mapping[str, str]) -> list[str]:
    """로그에서 가릴 값: .env 의 RTSP 계정(사용자·비밀번호)과 기기 토큰."""
    return [v for k, v in env.items() if (k.startswith(("RTSP_USER", "RTSP_PASS")) or k in SECRET_ENV_KEYS) and v]


@dataclass(frozen=True)
class Tier0Settings:
    laptop_id: str
    ec2_base_url: str
    device_token: str
    pinned_spki: str
    ca_file: str | None

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return (f"Tier0Settings(laptop_id={self.laptop_id!r}, ec2_base_url={self.ec2_base_url!r}, device_token=***, "
                f"pinned_spki={self.pinned_spki!r}, ca_file={self.ca_file!r})")


def load_tier0_settings(env: Mapping[str, str]) -> Tier0Settings:
    """Tier 0 전송 설정. 환경 변수(SIMAI_*)가 .env 보다 먼저다. 빠진 값은 이름만 모아 ConfigError(값은 넣지 않는다)."""
    merged = {**env, **{k: v for k, v in os.environ.items() if k.startswith("SIMAI_")}}
    missing = [k for k in ("SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN", "SIMAI_TIER0_PINNED_SPKI_SHA256")
               if not (merged.get(k) or "").strip()]
    if missing:
        raise ConfigError("Tier 0 전송에 필요한 .env 값이 없다: " + ", ".join(missing))
    return Tier0Settings(
        laptop_id=(merged.get("SIMAI_LAPTOP_ID") or "laptop-01").strip(),
        ec2_base_url=merged["SIMAI_EC2_BASE_URL"].strip(),
        device_token=merged["SIMAI_DEVICE_TOKEN"].strip(),
        pinned_spki=merged["SIMAI_TIER0_PINNED_SPKI_SHA256"].strip(),
        ca_file=(merged.get("SIMAI_TIER0_CA_FILE") or "").strip() or None,
    )


@dataclass(frozen=True)
class RemoteCctvSettings:
    ec2_base_url: str
    device_token: str

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return f"RemoteCctvSettings(ec2_base_url={self.ec2_base_url!r}, device_token=***)"


def load_remote_cctv_settings(env: Mapping[str, str]) -> RemoteCctvSettings:
    """CCTV 원격 시청 업로드(--remote-cctv, S15P31A101-400) 설정. Tier 0 전송과 같은 EC2 주소·기기 토큰을 쓴다."""
    merged = {**env, **{k: v for k, v in os.environ.items() if k.startswith("SIMAI_")}}
    missing = [k for k in ("SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN") if not (merged.get(k) or "").strip()]
    if missing:
        raise ConfigError("원격 CCTV 업로드에 필요한 .env 값이 없다: " + ", ".join(missing))
    return RemoteCctvSettings(ec2_base_url=merged["SIMAI_EC2_BASE_URL"].strip(),
                              device_token=merged["SIMAI_DEVICE_TOKEN"].strip())


class RedactSecrets(logging.Filter):
    """로그 문장에 비밀 값(그대로·URL 인코딩)이 섞이면 *** 로 바꾼다. 코드가 계정을 로그에 넣지 않는 것에 더한 안전망이다."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        forms = set()
        for s in secrets:
            if s:
                forms.update({s, urllib.parse.quote(s, safe=""), urllib.parse.quote(s)})
        self._forms = sorted(forms, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._forms:
            msg = record.getMessage()
            for s in self._forms:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True
