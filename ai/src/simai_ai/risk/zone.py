"""구역 (명세 8.2, 11.1 zone).

구역은 구성 묶음(config bundle)으로 들어오며 아직 contracts/ 스키마가 없다. 이 타입은 명세 11.1 zone 의
주요 항목만 담은 임시 형식이고, 구성 묶음 계약이 생기면 그 생성 타입으로 교체한다.

- 다각형은 World XY(m), 높이 범위는 World Z(m).
- 구역은 덮어쓰지 않고 새 버전을 추가한다(규칙 6). 판정은 판정 시각에 유효한 버전만 쓴다.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_enum,
    as_list,
    as_mapping,
    as_opt_str,
    as_opt_timestamp_ms,
    as_str,
    as_vec,
    reject_unknown_keys,
    require,
)
from simai_ai.risk.common import Severity


class ZoneJudgeType(StrEnum):
    """명세 8.2 구역 판정 유형."""

    GROUND_FOOTPRINT = "GROUND_FOOTPRINT"
    VOLUME_3D = "VOLUME_3D"


@dataclass(frozen=True)
class Zone:
    zone_id: str
    zone_version: str
    judge_type: ZoneJudgeType
    polygon_xy: tuple[tuple[float, ...], ...]
    z_range: tuple[float, ...] | None  # [최저, 최고]
    severity: Severity
    zone_type: str | None = None
    valid_from: int | None = None
    valid_to: int | None = None

    def __post_init__(self) -> None:
        where = f"zone {self.zone_id}"
        if len(self.polygon_xy) < 3:
            raise RiskInputError(f"{where}: polygon_xy 는 점이 3개 이상이어야 한다")
        if self.judge_type is ZoneJudgeType.VOLUME_3D and self.z_range is None:
            raise RiskInputError(f"{where}: VOLUME_3D 구역은 z_range 가 있어야 한다")
        if self.z_range is not None and not self.z_range[0] < self.z_range[1]:
            raise RiskInputError(f"{where}: z_range 는 [최저, 최고] 이고 최저 < 최고 여야 한다")
        if self.valid_from is not None and self.valid_to is not None and not self.valid_from < self.valid_to:
            raise RiskInputError(f"{where}: valid_from < valid_to 여야 한다")

    def is_active(self, t_ms: int) -> bool:
        """t_ms 에 유효한지. 구간은 [valid_from, valid_to) 이고 비어 있으면 그쪽 경계가 없다."""
        if self.valid_from is not None and t_ms < self.valid_from:
            return False
        return self.valid_to is None or t_ms < self.valid_to

    @classmethod
    def from_json(cls, obj: Any, where: str = "zone") -> "Zone":
        obj = as_mapping(obj, where)
        keys = {"zone_id", "zone_version", "judge_type", "polygon_xy", "z_range", "severity", "zone_type"}
        reject_unknown_keys(obj, keys | {"valid_from", "valid_to"}, where)
        polygon = as_list(require(obj, "polygon_xy", where), f"{where}.polygon_xy")
        z_range = require(obj, "z_range", where)
        return cls(
            zone_id=as_str(require(obj, "zone_id", where), f"{where}.zone_id"),
            zone_version=as_str(require(obj, "zone_version", where), f"{where}.zone_version"),
            judge_type=as_enum(ZoneJudgeType, require(obj, "judge_type", where), f"{where}.judge_type"),
            polygon_xy=tuple(as_vec(p, 2, f"{where}.polygon_xy[{i}]") for i, p in enumerate(polygon)),
            z_range=None if z_range is None else as_vec(z_range, 2, f"{where}.z_range"),
            severity=as_enum(Severity, require(obj, "severity", where), f"{where}.severity"),
            zone_type=as_opt_str(obj.get("zone_type"), f"{where}.zone_type"),
            valid_from=as_opt_timestamp_ms(obj.get("valid_from"), f"{where}.valid_from"),
            valid_to=as_opt_timestamp_ms(obj.get("valid_to"), f"{where}.valid_to"),
        )
