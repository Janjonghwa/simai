"""Tier 0 기록을 읽는 쪽의 인터페이스와 JSONL 구현 (명세 20.4 TIER0_LOG, 11.4 리플레이 질의, 12.2 I1).

기록 레코드 형식은 **I1 메시지 그대로**(공통 봉투 + payload)다. `msg_type` 이 `world_state`·`observation_batch`
인 레코드만 쓰고 나머지(health, risk_event_transition 등)는 건너뛴다. 저장 위치가 달라도 같은 레코드를 내면
같은 Provider 가 읽는다.

| 구현 | 저장 위치 | 상태 |
|---|---|---|
| `JsonlTier0LogSource` | I1 메시지 JSONL (한 줄에 봉투 하나, Tier 0 파일 입력 모드 출력·I1 수신 기록) | 이번 구현 |
| Spatial DB 리더 | `world_track_state`·`skeleton_3d` 행 → `world_state` 레코드로 다시 묶음 | 후속 (DB 스키마 S15P31A101-55 뒤) |
| 2D 관측 리더 | `observation_2d` → `observation_batch` 레코드 | 후속 (v1.3 결정: RunPod Network Volume Parquet) |

DB·Parquet 리더는 `Tier0LogSource` 를 구현하고, 행을 I1 메시지와 같은 키 이름으로 되돌려 낸다.
`msg_id` 가 없는 행은 리더가 행의 기본키로 안정된 값을 만들어 넣는다(중복 제거에 쓴다).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORLD_STATE = "world_state"
OBSERVATION_BATCH = "observation_batch"
# 이 리더가 의미를 아는 I1 메시지 스키마의 major 버전 (contracts/README.md 버전 규칙: 삭제·의미 변경은 major)
SUPPORTED_MESSAGE_MAJOR = 1


class Tier0LogError(ValueError):
    """Tier 0 기록을 계약대로 읽을 수 없다. 메시지에 기록 위치(파일:줄 등)를 담는다."""


@dataclass(frozen=True)
class Tier0LogRecord:
    """기록 레코드 하나: I1 메시지(봉투 + payload)와 오류 보고용 위치."""

    message: Mapping[str, Any]
    where: str


class Tier0LogSource(ABC):
    """Tier 0 기록 레코드를 낸다. 순서는 보장하지 않는다(Provider 가 시각 순서로 다시 정렬한다).

    t_start_ms·t_end_ms 는 저장소가 질의를 좁힐 수 있게 넘기는 힌트다(명세 11.4 리플레이 질의:
    시각 구간의 world_track_state). 구현은 무시해도 되고, Provider 가 같은 구간으로 다시 거른다.
    """

    @abstractmethod
    def records(self, t_start_ms: int | None = None, t_end_ms: int | None = None) -> Iterator[Tier0LogRecord]:
        """구간 [t_start_ms, t_end_ms) 를 포함하는 레코드를 낸다."""

    @abstractmethod
    def describe(self) -> str:
        """오류·로그에 쓸 짧은 설명 (예: 파일 경로)."""


class JsonlTier0LogSource(Tier0LogSource):
    """I1 메시지 JSONL 파일을 읽는다. 빈 줄은 건너뛰고, JSON 이 아니거나 객체가 아닌 줄은 오류다.

    잘린 마지막 줄(기록 중 중단)도 오류로 본다. 고쳐 쓰지 않고 원본 기록을 바로잡거나 구간을 좁힌다.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"{self.path}: Tier 0 기록 파일이 없다")

    def describe(self) -> str:
        return str(self.path)

    def records(self, t_start_ms: int | None = None, t_end_ms: int | None = None) -> Iterator[Tier0LogRecord]:
        with self.path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                where = f"{self.path}:{lineno}"
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as e:
                    raise Tier0LogError(f"{where}: JSON 이 아니다 ({e.msg})") from e
                if not isinstance(message, dict):
                    raise Tier0LogError(f"{where}: I1 메시지(JSON 객체)가 아니다")
                yield Tier0LogRecord(message, where)


def jsonl_sources(paths: Sequence[str | Path]) -> list[Tier0LogSource]:
    """경로 목록 → JSONL 리더 목록. 디렉터리를 주면 그 안의 `*.jsonl` 을 이름 순서로 읽는다."""
    out: list[Tier0LogSource] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = sorted(path.glob("*.jsonl"))
            if not files:
                raise FileNotFoundError(f"{path}: *.jsonl 기록이 없다")
            out.extend(JsonlTier0LogSource(f) for f in files)
        else:
            out.append(JsonlTier0LogSource(path))
    if not out:
        raise ValueError("Tier 0 기록 경로가 비어 있다")
    return out
