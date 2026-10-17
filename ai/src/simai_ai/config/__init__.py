"""구성 묶음 검증·원자적 전환과 Config Sync 상태 (명세 3.2 구성 묶음, 6.2, FR-17·FR-83, 부록 D-G3-06).

Tier 0 쪽 기능이다. 판정 로직은 risk 에 두고, 여기서는 어떤 구역·규칙·보정 버전·작업면으로 판정할지(활성 묶음)를 정한다.
risk 는 이 패키지를 import 하지 않는다(EC2 컨테이너는 Risk Engine 만 설치한다).

| 모듈 | 내용 |
|---|---|
| bundle | 알림(BundleNotice)·검증 묶음(ConfigBundle) 형식, 구성 요소 내용 임시 형식, 내용 해시 |
| verify | BundleVerifier: 스키마·내용 해시·내용 형식·참조 무결성 검증, 거부 사유 RejectReason |
| sync | ConfigSync: 판정 tick 경계에서만 전환(ActiveConfig), config_ack, CURRENT/PENDING/UNVERIFIED/STALE |
| archive | BundleArchive: 검증 묶음 보관(덮어쓰지 않음)과 재시작 복구 |
"""

from simai_ai.config.archive import BundleArchive
from simai_ai.config.bundle import BundleNotice, ComponentRef, ConfigBundle, sha256_of
from simai_ai.config.sync import (
    ActiveConfig,
    AppliedRecord,
    ConfigSync,
    ConfigSyncState,
    ReceiveResult,
    SyncTransition,
)
from simai_ai.config.verify import (
    BundleRejected,
    BundleVerifier,
    Fetcher,
    FetchError,
    RejectReason,
    calibration_known_from,
)

__all__ = [
    "ActiveConfig",
    "AppliedRecord",
    "BundleArchive",
    "BundleNotice",
    "BundleRejected",
    "BundleVerifier",
    "ComponentRef",
    "ConfigBundle",
    "ConfigSync",
    "ConfigSyncState",
    "FetchError",
    "Fetcher",
    "ReceiveResult",
    "RejectReason",
    "SyncTransition",
    "calibration_known_from",
    "sha256_of",
]
