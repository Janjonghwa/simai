"""중계 설정(TOML)과 카메라 계정(.env) 읽기, 로그의 비밀 값 가리기.

설정 형식은 녹화 도구 세션 설정의 [capture_time]·[[cameras]] 와 같다(입력 만들기는 simai_recorder.config.build_source
를 그대로 쓴다). [session]·[consent] 같은 녹화 전용 절은 읽지 않는다(중계는 영상을 저장하지 않는다).
카메라 계정은 저장소 루트 .env 의 RTSP_USER / RTSP_PASS(카메라별 RTSP_USER_CAM_A …)에서만 읽는다.
"""

from __future__ import annotations

import logging
import tomllib
import urllib.parse
from collections.abc import Iterable, Mapping
from pathlib import Path

from simai_recorder.capture_time import CaptureTimeModel
from simai_recorder.clock import Clock
from simai_recorder.config import ConfigError, build_source, load_env
from simai_recorder.recorder import CameraSpec

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

__all__ = ["DEFAULT_ENV_FILE", "ConfigError", "RedactSecrets", "load_cameras", "load_env", "secret_values"]


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
        model = CaptureTimeModel(**{**base, **(c.get("capture_time") or {})})
        specs.append(CameraSpec(camera_id=c["camera_id"], source=build_source(c, clock, env, path.parent),
                                capture_time=model, firmware=c.get("firmware") or None))
    ids = [s.camera_id for s in specs]
    if len(set(ids)) != len(ids):
        raise ConfigError("camera_id 가 겹친다")
    return specs


def secret_values(env: Mapping[str, str]) -> list[str]:
    """로그에서 가릴 값: .env 의 RTSP 계정(사용자·비밀번호)."""
    return [v for k, v in env.items() if k.startswith(("RTSP_USER", "RTSP_PASS")) and v]


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
