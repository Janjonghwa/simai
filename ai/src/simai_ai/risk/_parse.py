"""JSON 입력(dict) 을 Risk Engine 타입으로 옮길 때 쓰는 검사 도우미.

서드파티 검증기(jsonschema 등)를 쓰지 않는다(risk 는 표준 라이브러리만 쓴다).
값이 계약과 다르면 RiskInputError 를 낸다. 모르는 값을 기본값으로 채우지 않는다.
"""

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, TypeVar

E = TypeVar("E", bound=StrEnum)

_MISSING = object()


class RiskInputError(ValueError):
    """입력이 계약(contracts/ 스키마 또는 공통 벡터 형식)과 맞지 않는다."""


def require(obj: Mapping[str, Any], key: str, where: str) -> Any:
    """필수 키. 값이 null 이어도 키는 있어야 한다(스키마의 required)."""
    value = obj.get(key, _MISSING)
    if value is _MISSING:
        raise RiskInputError(f"{where}: 필수 필드 {key} 가 없다")
    return value


def as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RiskInputError(f"{where}: 객체여야 한다 (받은 값: {type(value).__name__})")
    return value


def as_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise RiskInputError(f"{where}: 배열이어야 한다 (받은 값: {type(value).__name__})")
    return value


def as_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RiskInputError(f"{where}: 빈 문자열이 아닌 문자열이어야 한다")
    return value


def as_opt_str(value: Any, where: str) -> str | None:
    return None if value is None else as_str(value, where)


def as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise RiskInputError(f"{where}: true/false 여야 한다")
    return value


def as_int(value: Any, where: str, minimum: int | None = None) -> int:
    # bool 은 int 의 하위 타입이므로 따로 막는다. 시각(UTC ms)은 정수만 받는다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RiskInputError(f"{where}: 정수여야 한다 (받은 값: {value!r})")
    if minimum is not None and value < minimum:
        raise RiskInputError(f"{where}: {minimum} 이상이어야 한다 (받은 값: {value})")
    return value


# UTC epoch ms 는 int64 (AGENTS.md 좌표 규약).
INT64_MAX = 2**63 - 1


def as_timestamp_ms(value: Any, where: str) -> int:
    value = as_int(value, where, minimum=0)
    if value > INT64_MAX:
        raise RiskInputError(f"{where}: int64 범위를 넘는다 (받은 값: {value})")
    return value


def as_opt_timestamp_ms(value: Any, where: str) -> int | None:
    return None if value is None else as_timestamp_ms(value, where)


def as_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RiskInputError(f"{where}: 숫자여야 한다 (받은 값: {value!r})")
    # Python json 은 표준 밖 NaN·Infinity 를 받아 준다. 좌표·불확실성에 그런 값이 들어오지 않게 막는다.
    try:
        number = float(value)
    except OverflowError:  # float 로 담을 수 없는 큰 정수
        number = math.inf
    if not math.isfinite(number):
        raise RiskInputError(f"{where}: 유한한 수여야 한다 (받은 값: {value!r})")
    return number


def as_opt_number(value: Any, where: str) -> float | None:
    return None if value is None else as_number(value, where)


def as_vec(value: Any, n: int, where: str) -> tuple[float, ...]:
    items = as_list(value, where)
    if len(items) != n:
        raise RiskInputError(f"{where}: 길이 {n} 인 배열이어야 한다 (받은 길이: {len(items)})")
    return tuple(as_number(v, f"{where}[{i}]") for i, v in enumerate(items))


def as_opt_vec(value: Any, n: int, where: str) -> tuple[float, ...] | None:
    return None if value is None else as_vec(value, n, where)


def as_enum(cls: type[E], value: Any, where: str) -> E:
    try:
        return cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in cls)
        raise RiskInputError(f"{where}: {allowed} 중 하나여야 한다 (받은 값: {value!r})") from None


def reject_unknown_keys(obj: Mapping[str, Any], allowed: set[str], where: str) -> None:
    """공통 벡터처럼 우리가 정한 형식에서 오타를 잡는다. 계약 메시지(world_state)에는 쓰지 않는다
    (명세 12.1: 수신 측은 모르는 필드를 무시한다)."""
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise RiskInputError(f"{where}: 모르는 필드 {unknown}")
