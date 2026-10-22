"""감시 상태 입력: 카메라 상태와 구역별 관측 카메라 (명세 6.2, 6.5, 7.2 R7, 12.3 health).

R7(구역 감시 능력 저하)이 읽는 입력이다. world_state 에는 카메라 상태가 없으므로 Tier 0 가 같은 판정 시각의
조각으로 함께 넘긴다(RiskEngine.evaluate 의 monitoring). risk 는 tracking 패키지를 import 하지 않는다.

- cameras: health 메시지(contracts/messages/health.schema.json $defs.camera_health)의 카메라 항목과 같은 이름을
  쓴다(id, status, last_capture). health 의 나머지 필드(fps 등)는 무시한다(명세 12.1 수신 측 규칙).
- zone_cameras: 구역 → 그 구역을 시야에 담는 카메라. 보정·구역 버전으로 상류(Tier 0)가 계산해 넘긴다.
  아직 계약이 없어 내부 형식이다(구성 묶음·보정 계약이 정해지면 그 형식으로 바꾼다).
- 모르는 값은 비운다. 목록에 없는 구역·카메라는 '관측한다'로 채우지 않는다(R7 이 저하로 본다).

정상 카메라 판단은 System Health(tracking/system_health)와 같은 기준을 쓴다. 동기화 유효(SYNC_INVALID 아님)이고
t_latest_global - last_capture ≤ camera_stall_ms(초기값 500ms)면 수신 중. 여기에 더해 상태가 OK 가 아니면
(NO_SIGNAL·WAITING_KEYFRAME) 바로 정상에서 뺀다. 그래서 R7 의 정상 카메라는 System Health 의 수신 중 카메라의
부분집합이다(System Health 가 수신 중단으로 본 카메라를 R7 이 정상으로 세지 않는다).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_enum,
    as_list,
    as_mapping,
    as_opt_timestamp_ms,
    as_str,
    reject_unknown_keys,
    require,
)


class CameraStatus(StrEnum):
    """health camera_health.status (명세 12.3, 12.7, 6.5)."""

    OK = "OK"
    NO_SIGNAL = "NO_SIGNAL"  # 수신 중단
    WAITING_KEYFRAME = "WAITING_KEYFRAME"  # 재접속 후 코덱 정보·키프레임 대기(관측 없음)
    SYNC_INVALID = "SYNC_INVALID"  # 시간 동기화 무효로 융합 제외


@dataclass(frozen=True)
class CameraMonitor:
    """카메라 한 대의 상태. last_capture 는 마지막으로 처리한 프레임의 촬영 시각(UTC ms), 받은 적 없으면 None.

    fusion_excluded: 수신 중이지만 그 판정 시각의 융합(다시점 관측)에 쓰지 못한 카메라(Tier 0 가 늦어서 융합 시각 계산에서
        뺀 카메라, S15P31A101-219). health 에는 없는 Tier 0 내부 값이라 from_json(health 형식)은 False 로 둔다.
    """

    camera_id: str
    status: CameraStatus
    last_capture: int | None
    fusion_excluded: bool = False

    @classmethod
    def from_json(cls, obj: Any, where: str = "camera") -> "CameraMonitor":
        obj = as_mapping(obj, where)
        return cls(
            camera_id=as_str(require(obj, "id", where), f"{where}.id"),
            status=as_enum(CameraStatus, require(obj, "status", where), f"{where}.status"),
            last_capture=as_opt_timestamp_ms(require(obj, "last_capture", where), f"{where}.last_capture"),
        )


@dataclass(frozen=True)
class MonitoringState:
    """판정 시각의 카메라 상태와 구역별 관측 카메라."""

    cameras: tuple[CameraMonitor, ...]
    zone_cameras: Mapping[str, tuple[str, ...]] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        ids = [c.camera_id for c in self.cameras]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise RiskInputError(f"monitoring: 카메라 id 가 중복된다 {duplicated}")
        coverage: dict[str, tuple[str, ...]] = {}
        for zone_id, cameras in sorted(self.zone_cameras.items()):
            cameras = tuple(cameras)
            if len(set(cameras)) != len(cameras):
                raise RiskInputError(f"monitoring.zone_cameras.{zone_id}: 카메라가 중복된다")
            coverage[zone_id] = tuple(sorted(cameras))
        object.__setattr__(self, "cameras", tuple(sorted(self.cameras, key=lambda c: c.camera_id)))
        object.__setattr__(self, "zone_cameras", MappingProxyType(coverage))

    def camera(self, camera_id: str) -> CameraMonitor | None:
        return next((c for c in self.cameras if c.camera_id == camera_id), None)

    @classmethod
    def from_json(cls, obj: Any, where: str = "monitoring") -> "MonitoringState":
        obj = as_mapping(obj, where)
        reject_unknown_keys(obj, {"cameras", "zone_cameras"}, where)
        cameras = as_list(require(obj, "cameras", where), f"{where}.cameras")
        coverage = as_mapping(require(obj, "zone_cameras", where), f"{where}.zone_cameras")
        return cls(
            cameras=tuple(CameraMonitor.from_json(c, f"{where}.cameras[{i}]") for i, c in enumerate(cameras)),
            zone_cameras={
                as_str(zone_id, f"{where}.zone_cameras"): tuple(
                    as_str(c, f"{where}.zone_cameras.{zone_id}[{i}]")
                    for i, c in enumerate(as_list(ids, f"{where}.zone_cameras.{zone_id}"))
                )
                for zone_id, ids in coverage.items()
            },
        )
