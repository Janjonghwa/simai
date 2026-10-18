"""단말 설정(.env). 비밀 값(기기 토큰)은 .env 에서만 읽고 로그·화면에 넣지 않는다(커밋 금지).

    SIMAI_LAPTOP_ID                  노트북 기기 ID (봉투 source_id, site_ack actor_id). 기본 laptop-01
    SIMAI_EC2_BASE_URL               EC2 주소 https://... (Tier 0 엔드포인트 조회 GET /api/endpoints/tier0)
    SIMAI_DEVICE_TOKEN               노트북 기기 토큰 (비밀. EC2 조회와 Tier 0 제어 채널 인증에 쓴다)
    SIMAI_TIER0_PINNED_SPKI_SHA256   미리 고정한 Tier 0 TLS 공개키 지문 sha256:<hex 64> (키 교체 기간에는 쉼표로 여러 개).
                                     비밀 값은 아니지만 EC2 조회 값으로 대신하지 않는다. 없으면 접속하지 않는다
    SIMAI_TIER0_CONTROL_PATH         Tier 0 I0 제어 WebSocket 경로 (기본 /control)
    SIMAI_TIER0_CA_FILE              팀 사설 CA 인증서 파일(선택. 쓰면 체인 검증도 함께 한다)
    SIMAI_RELAY_STATUS_PORT          영상 중계 상태 보고를 받는 127.0.0.1 UDP 포트 (기본 47031, 중계와 같은 값)

EC2 하트비트(POST /api/devices/heartbeat)는 SIMAI_EC2_BASE_URL(https 만)·SIMAI_DEVICE_TOKEN 을 쓴다.

Tier 0 주소·포트는 설정에 두지 않는다. RunPod 는 Pod 가 재설정될 때마다 포트가 바뀌므로 EC2 에서 조회한다(명세 3.12).
환경 변수가 있으면 .env 보다 먼저다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from simai_site_alert.endpoint import DEFAULT_CONTROL_PATH
from simai_site_alert.relay_status import DEFAULT_PORT as DEFAULT_RELAY_STATUS_PORT

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    laptop_id: str
    ec2_base_url: str | None
    device_token: str | None
    pinned_spki: str | None
    control_path: str
    ca_file: str | None
    relay_status_port: int = DEFAULT_RELAY_STATUS_PORT

    def __repr__(self) -> str:  # 토큰을 찍지 않는다
        return (
            f"Settings(laptop_id={self.laptop_id!r}, ec2_base_url={self.ec2_base_url!r}, "
            f"device_token={'***' if self.device_token else None}, pinned_spki={self.pinned_spki!r}, "
            f"control_path={self.control_path!r}, ca_file={self.ca_file!r}, relay_status_port={self.relay_status_port})"
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


def _port(raw: str | None, default: int) -> int:
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("SIMAI_RELAY_STATUS_PORT 는 1~65535 정수다")
    return int(raw)


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    env = load_env(env_file)
    return Settings(
        laptop_id=env.get("SIMAI_LAPTOP_ID") or "laptop-01",
        ec2_base_url=env.get("SIMAI_EC2_BASE_URL") or None,
        device_token=env.get("SIMAI_DEVICE_TOKEN") or None,
        pinned_spki=env.get("SIMAI_TIER0_PINNED_SPKI_SHA256") or None,
        control_path=env.get("SIMAI_TIER0_CONTROL_PATH") or DEFAULT_CONTROL_PATH,
        ca_file=env.get("SIMAI_TIER0_CA_FILE") or None,
        relay_status_port=_port(env.get("SIMAI_RELAY_STATUS_PORT"), DEFAULT_RELAY_STATUS_PORT),
    )
