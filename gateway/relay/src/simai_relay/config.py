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

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

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


def load_cameras(path: Path, clock: Clock, env: Mapping[str, str]) -> list[CameraSpec]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cams = data.get("cameras", [])
    if not cams:
        raise ConfigError("[[cameras]] 가 없다")
    base = data.get("capture_time", {})
    specs = []
    for c in cams:
        if not c.get("camera_id"):
            raise ConfigError("[[cameras]] 에 camera_id 가 없다")
        resolved = _resolve_camera_entry(c, path.parent)
        defaults = {}
        if resolved.get("kind") == "file":
            defaults["timestamp_source"] = "FILE"
            if resolved.get("realtime", True):
                # FileSource.read() 는 _pace() 뒤 반환한다. PTS 없음 시 수신 시각은 파일 페이싱 시각이고
                # RTSP 의 Wi-Fi 지터를 위한 45 ms 대신 정상 PTS 와 같은 잠정 20 ms 를 쓴다(-353).
                # 근거·한계: README 의 파일 재생 절. 명시 설정은 공통 → 카메라 순으로 우선한다.
                defaults["uncertainty_no_pts_ms"] = 20.0
        model = CaptureTimeModel(**{**defaults, **base, **(resolved.get("capture_time") or {})})
        specs.append(CameraSpec(camera_id=resolved["camera_id"], source=build_source(resolved, clock, env, path.parent),
                                capture_time=model, firmware=resolved.get("firmware") or None))
    ids = [s.camera_id for s in specs]
    if len(set(ids)) != len(ids):
        raise ConfigError("camera_id 가 겹친다")
    return specs


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
