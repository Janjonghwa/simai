"""R9·R11 입력: 안전모·조끼 요구(구성 묶음 `ppe_policy`)와 사람별 안전모·조끼 상태(I1 `ppe_state`) (명세 5.5, 7.2, 8.10, 12.3).

- 요구(PpePolicy)는 작업 기준이다. 진행 중 작업이 있으면 현장 전체에서 안전모 REQUIRED, 없으면 NOT_REQUIRED 다. 조끼(`vest`,
  ppe_policy 1.1.0, R11)는 선택이고 실으면 같은 규칙이며, 없으면 조끼 요구 없음이다(S15P31A101-548). 위치·구역은
  보지 않는다. 내용 형식의 정본은 `contracts/data/ppe_policy.schema.json` 이고, 구성 묶음 검증(config.verify)이 스키마로 먼저
  검사한다. 여기서는 판정에 쓰는 값만 옮긴다.
- 장비 상태(WearObservation, 안전모·조끼 각각)는 PPE 프로세스가 만든 `ppe_state` payload 의 사람 항목에서 옮긴다. 안전모는
  R9, 조끼는 R11 이 쓴다. 같은 World ID·`identity_revision` 의 최신 상태만 쓴다.
- 만료: 상태의 `expires_at_ms`(마지막 유효 근거 + 10초, 5.5)가 판정 기준 시각보다 앞이면 UNKNOWN 이다. PPE 프로세스가 멈춰 새
  메시지가 오지 않아도 Tier 0 가 스스로 만료시킨다. 판정 기준 시각은 World 의 `t_latest_global`(촬영 시각 축)이다. PPE 상태의
  시각도 같은 촬영 시각 축이라 REPLAY 에서도 같은 결과가 나온다.
- PpeStateStore 는 Tier 0 가 PPE outbox 에서 꺼낸 메시지를 넣고 판정 tick 마다 스냅샷을 꺼내는 곳이다. 스레드 하나가 넣고 다른
  스레드가 읽어서 잠금을 둔다. 메시지 순서는 (`stream_epoch`, `state_sequence`)로 보고 이미 본 것보다 앞선 메시지는 버린다.
  새 PPE 프로세스(`stream_epoch` 가 더 큼)의 메시지는 받는다.

risk 패키지 규칙대로 표준 라이브러리만 쓰고 현재 시각을 읽지 않는다(시각은 호출부가 넘긴다).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_enum,
    as_int,
    as_list,
    as_mapping,
    as_str,
    require,
)
from simai_ai.risk.world import WorldTrack


class HelmetRequirement(StrEnum):
    """`ppe_policy.helmet` (contracts/data/ppe_policy)."""

    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"


VestRequirement = HelmetRequirement
"""`ppe_policy.vest` (1.1.0). 값은 안전모 요구와 같다."""


class PpeItem(StrEnum):
    """`ppe_state` 사람 항목의 장비 키."""

    HELMET = "helmet"
    VEST = "vest"


class WearState(StrEnum):
    """`ppe_state` 장비 상태(contracts/messages/ppe_state)."""

    WORN = "WORN"
    NOT_WORN = "NOT_WORN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PpePolicy:
    """구성 묶음 구성 요소 `ppe_policy` 의 판정 값. 형식 검사(스키마)는 config.verify 가 먼저 한다."""

    version: str
    work_id: int | None
    effective_from_ms: int
    helmet: HelmetRequirement
    # 조끼 요구(1.1.0, 선택). None 은 문서에 vest 가 없다는 뜻이고 요구 없음으로 판정한다(R11).
    vest: VestRequirement | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "helmet", HelmetRequirement(self.helmet))
        if self.vest is not None:
            object.__setattr__(self, "vest", VestRequirement(self.vest))
        # 스키마의 allOf 와 같은 규칙(진행 중 작업이 있으면 REQUIRED, 없으면 NOT_REQUIRED, 명세 7.2·8.10). 두 번 막는다.
        for name, value in (("helmet", self.helmet), ("vest", self.vest)):
            if value is not None and (self.work_id is None) != (value is HelmetRequirement.NOT_REQUIRED):
                raise RiskInputError(
                    f"ppe_policy {self.version}: work_id {self.work_id!r} 와 {name} {value} 가 맞지 않는다"
                )

    @property
    def helmet_required(self) -> bool:
        return self.helmet is HelmetRequirement.REQUIRED

    @property
    def vest_required(self) -> bool:
        return self.vest is VestRequirement.REQUIRED

    @classmethod
    def from_json(cls, obj: Any, where: str = "ppe_policy") -> PpePolicy:
        obj = as_mapping(obj, where)
        work_id = require(obj, "work_id", where)
        if work_id is not None:
            work_id = as_int(work_id, f"{where}.work_id", minimum=1)
        vest = obj.get("vest")
        return cls(
            version=as_str(require(obj, "version", where), f"{where}.version"),
            work_id=work_id,
            effective_from_ms=as_int(require(obj, "effective_from_ms", where), f"{where}.effective_from_ms", minimum=0),
            helmet=as_enum(HelmetRequirement, require(obj, "helmet", where), f"{where}.helmet"),
            vest=None if vest is None else as_enum(VestRequirement, vest, f"{where}.vest"),
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "work_id": self.work_id,
            "effective_from_ms": self.effective_from_ms,
            "helmet": str(self.helmet),
        }
        if self.vest is not None:
            out["vest"] = str(self.vest)
        return out


@dataclass(frozen=True)
class WearObservation:
    """한 사람의 장비 하나 상태(ppe_state.people[].helmet 또는 .vest). expires_at_ms 는 근거가 없으면(UNKNOWN) None 이다."""

    world_id: str
    identity_revision: int
    capture_time_ms: int
    state: WearState
    expires_at_ms: int | None
    item: PpeItem = PpeItem.HELMET

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", WearState(self.state))
        object.__setattr__(self, "item", PpeItem(self.item))

    def state_at(self, t_ms: int) -> WearState:
        """t_ms(촬영 시각 축)에서의 상태. 만료됐거나 만료 시각이 없는 WORN·NOT_WORN 은 UNKNOWN 이다(모르면 비운다)."""
        if self.state is WearState.UNKNOWN:
            return WearState.UNKNOWN
        if self.expires_at_ms is None or t_ms > self.expires_at_ms:
            return WearState.UNKNOWN
        return self.state

    @classmethod
    def from_person(cls, person: Any, where: str, item: PpeItem = PpeItem.HELMET) -> WearObservation:
        person = as_mapping(person, where)
        key = str(PpeItem(item))
        state = as_mapping(require(person, key, where), f"{where}.{key}")
        expires = require(state, "expires_at_ms", f"{where}.{key}")
        return cls(
            world_id=as_str(require(person, "world_id", where), f"{where}.world_id"),
            identity_revision=as_int(require(person, "identity_revision", where), f"{where}.identity_revision", minimum=0),
            capture_time_ms=as_int(require(person, "capture_time_ms", where), f"{where}.capture_time_ms", minimum=0),
            state=as_enum(WearState, require(state, "state", f"{where}.{key}"), f"{where}.{key}.state"),
            expires_at_ms=None if expires is None else as_int(expires, f"{where}.{key}.expires_at_ms", minimum=0),
            item=PpeItem(item),
        )

    @classmethod
    def all_from_person(cls, person: Any, where: str) -> tuple[WearObservation, WearObservation]:
        """사람 항목 하나에서 안전모·조끼 상태를 함께 옮긴다(둘 다 계약상 필수)."""
        return cls.from_person(person, where, PpeItem.HELMET), cls.from_person(person, where, PpeItem.VEST)


HelmetObservation = WearObservation
"""이전 이름(R9, S15P31A101-519). 안전모만 옮기던 때의 이름이고 지금은 WearObservation 과 같다."""


def _newer(a: WearObservation, b: WearObservation) -> bool:
    """a 가 b 보다 새 상태인가. identity_revision 이 크면 새것이고, 같으면 촬영 시각이 늦거나 같은 쪽(나중에 받은 것)이다."""
    return (a.identity_revision, a.capture_time_ms) >= (b.identity_revision, b.capture_time_ms)


def _merge(target: dict[str, WearObservation], obs: WearObservation) -> None:
    seen = target.get(obs.world_id)
    if seen is None or _newer(obs, seen):
        target[obs.world_id] = obs


def _state(observations: Mapping[str, WearObservation], track: WorldTrack, t_ms: int) -> WearState:
    obs = observations.get(track.world_id)
    if obs is None or obs.identity_revision != track.identity_revision:
        return WearState.UNKNOWN
    return obs.state_at(t_ms)


@dataclass(frozen=True)
class PpeSnapshot:
    """판정 한 번에 쓰는 사람별 최신 안전모·조끼 상태(World ID → 상태). 읽기 전용."""

    helmets: Mapping[str, WearObservation] = field(default_factory=dict)
    vests: Mapping[str, WearObservation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "helmets", MappingProxyType(dict(sorted(self.helmets.items()))))
        object.__setattr__(self, "vests", MappingProxyType(dict(sorted(self.vests.items()))))

    def helmet_state(self, track: WorldTrack, t_ms: int) -> WearState:
        """이 트랙의 안전모 상태. 상태가 없거나 identity_revision 이 다르면(ID 교정 뒤 옛 상태) UNKNOWN 이다(7.2)."""
        return _state(self.helmets, track, t_ms)

    def vest_state(self, track: WorldTrack, t_ms: int) -> WearState:
        """이 트랙의 조끼 상태(R11). 규칙은 helmet_state 와 같다."""
        return _state(self.vests, track, t_ms)

    @classmethod
    def from_observations(cls, observations: Iterable[WearObservation]) -> PpeSnapshot:
        helmets: dict[str, WearObservation] = {}
        vests: dict[str, WearObservation] = {}
        for obs in observations:
            _merge(vests if obs.item is PpeItem.VEST else helmets, obs)
        return cls(helmets, vests)

    @classmethod
    def from_payloads(cls, payloads: Iterable[Any], where: str = "ppe_state") -> PpeSnapshot:
        """ppe_state payload 목록(오래된 것부터)에서 사람별 최신 상태를 만든다. 공통 벡터와 시험이 쓴다."""
        observations: list[WearObservation] = []
        for i, payload in enumerate(payloads):
            people = as_list(require(as_mapping(payload, f"{where}[{i}]"), "people", f"{where}[{i}]"), f"{where}[{i}].people")
            for j, p in enumerate(people):
                observations.extend(WearObservation.all_from_person(p, f"{where}[{i}].people[{j}]"))
        return cls.from_observations(observations)


EMPTY_PPE = PpeSnapshot()


class PpeStateStore:
    """PPE outbox 에서 꺼낸 `ppe_state` 메시지를 사람별 최신 안전모·조끼 상태로 모은다(Tier 0, 명세 5.5·7.2).

    - offer(message): 봉투 하나. msg_type 이 ppe_state 가 아니거나 형식이 틀리면 세고 버린다(판정 스레드를 막지 않는다).
      (stream_epoch, state_sequence) 가 이미 받은 것보다 앞서면 버린다(늦게 온 옛 메시지가 최신 상태를 덮지 않게).
    - snapshot(): 판정 tick 이 쓰는 읽기 전용 스냅샷.
    - 오래된 사람 항목은 max_people 을 넘을 때 촬영 시각이 오래된 것부터 지운다(상한 있는 메모리).
    """

    def __init__(self, *, max_people: int = 256) -> None:
        if max_people < 1:
            raise ValueError("max_people 은 1 이상이다")
        self._lock = threading.Lock()
        self._helmets: dict[str, WearObservation] = {}
        self._vests: dict[str, WearObservation] = {}
        self._last_seq: tuple[int, int] | None = None
        self._snapshot = EMPTY_PPE
        self._max_people = max_people
        self.accepted = 0
        self.stale = 0
        self.invalid = 0

    def offer(self, message: Mapping[str, Any]) -> bool:
        try:
            if message.get("msg_type") != "ppe_state":
                raise RiskInputError("msg_type 이 ppe_state 가 아니다")
            payload = as_mapping(require(message, "payload", "ppe_state"), "ppe_state.payload")
            seq = (
                as_int(require(payload, "stream_epoch", "ppe_state.payload"), "ppe_state.stream_epoch", minimum=0),
                as_int(require(payload, "state_sequence", "ppe_state.payload"), "ppe_state.state_sequence", minimum=0),
            )
            people = as_list(require(payload, "people", "ppe_state.payload"), "ppe_state.people")
            observations = [
                obs for i, p in enumerate(people) for obs in WearObservation.all_from_person(p, f"ppe_state.people[{i}]")
            ]
        except (RiskInputError, AttributeError):
            with self._lock:
                self.invalid += 1
            return False
        with self._lock:
            if self._last_seq is not None and seq <= self._last_seq:
                self.stale += 1
                return False
            self._last_seq = seq
            for obs in observations:
                _merge(self._vests if obs.item is PpeItem.VEST else self._helmets, obs)
            if len(self._helmets) > self._max_people:
                keep = sorted(self._helmets.values(), key=lambda o: o.capture_time_ms)[-self._max_people:]
                self._helmets = {o.world_id: o for o in keep}
            if len(self._vests) > self._max_people:
                keep = sorted(self._vests.values(), key=lambda o: o.capture_time_ms)[-self._max_people:]
                self._vests = {o.world_id: o for o in keep}
            self._snapshot = PpeSnapshot(self._helmets, self._vests)
            self.accepted += 1
        return True

    def snapshot(self) -> PpeSnapshot:
        with self._lock:
            return self._snapshot

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"accepted": self.accepted, "stale": self.stale, "invalid": self.invalid,
                    "people": len(self._helmets)}
