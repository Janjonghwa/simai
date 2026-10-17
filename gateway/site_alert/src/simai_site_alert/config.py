"""단말 설정(.env). 비밀 값(기기 토큰)은 .env 에서만 읽고 로그·화면에 넣지 않는다(커밋 금지).

    SIMAI_LAPTOP_ID            노트북 기기 ID (봉투 source_id, site_ack actor_id). 기본 laptop-01
    SIMAI_TIER0_CONTROL_URL    Tier 0 I0 제어 채널 주소 wss://<host>:<port>/<path>
                               (S15P31A101-159 뒤에는 EC2 엔드포인트 조회로 바뀐다)
    SIMAI_DEVICE_TOKEN         노트북 기기 토큰 (비밀)
    SIMAI_TIER0_CA_FILE        팀 사설 CA 인증서 파일(선택)

환경 변수가 있으면 .env 보다 먼저다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    laptop_id: str
    control_url: str | None
    device_token: str | None
    ca_file: str | None

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return (
            f"Settings(laptop_id={self.laptop_id!r}, control_url={self.control_url!r}, "
            f"device_token={'***' if self.device_token else None}, ca_file={self.ca_file!r})"
        )


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("SIMAI_")})
    return env


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    env = load_env(env_file)
    return Settings(
        laptop_id=env.get("SIMAI_LAPTOP_ID") or "laptop-01",
        control_url=env.get("SIMAI_TIER0_CONTROL_URL") or None,
        device_token=env.get("SIMAI_DEVICE_TOKEN") or None,
        ca_file=env.get("SIMAI_TIER0_CA_FILE") or None,
    )
