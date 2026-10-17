"""TIER0_LOG 설정과 "Tier 0 파일 입력 모드 실행 → 기록 → Provider" 연결 지점 (명세 20.4 규칙 5).

`TIER0_LOG` 는 Tier 0 서비스를 녹화 입력(파일 모드)으로 돌려 얻는다. 오프라인용 삼각측량·추적을 따로 만들지
않는다. Tier 0 조립(`services.tier0`)이 아직 골격이라 실행기는 인터페이스만 두고, 지금은 이미 있는 기록을
`log_paths` 로 준다.

설정(`world_tracks.tier0_log`) 키:

| 키 | 뜻 |
|---|---|
| `log_paths` | I1 메시지 JSONL 경로 목록(파일 또는 `*.jsonl` 이 든 디렉터리). 문자열 하나도 된다 |
| `file_mode` | Tier 0 파일 입력 모드 실행 명세 `{tier0_config, recording, output_dir}` (후속, 실행기 필요) |
| `tier0_build` | 기록을 만든 Tier 0 빌드(예: git 커밋). provider_version 에 붙는다. file_mode 는 실행기가 정한다 |
| `t_start_ms`, `t_end_ms` | 문서 시각 구간 [시작, 끝) |
| `max_obs_offset_ms` | per_camera 프레임 선택 허용 차이 (기본 50) |

`log_paths` 와 `file_mode` 중 정확히 하나를 쓴다. 모르는 키는 오류다(오타로 설정이 조용히 빠지지 않게).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_ai.providers.tier0_log.provider import DEFAULT_MAX_OBS_OFFSET_MS, Tier0LogWorldTrackProvider

CONFIG_KEYS = frozenset({"log_paths", "file_mode", "tier0_build", "t_start_ms", "t_end_ms", "max_obs_offset_ms"})
FILE_MODE_KEYS = frozenset({"tier0_config", "recording", "output_dir"})


@dataclass(frozen=True)
class Tier0FileModeSpec:
    """Tier 0 를 파일 입력 모드로 한 번 돌리는 명세.

    - tier0_config: `python -m services.tier0 --config` 에 줄 설정 파일
    - recording: 녹화 입력(FrameSource FILE 이 읽는 녹화 manifest 또는 디렉터리)
    - output_dir: Tier 0 가 I1 메시지(world_state, observation_batch 등)를 보낸 순서대로 JSONL 로 남길 곳
    """

    tier0_config: Path
    recording: Path
    output_dir: Path

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> Tier0FileModeSpec:
        _check_keys(cfg, FILE_MODE_KEYS, "tier0_log.file_mode", required=FILE_MODE_KEYS)
        return cls(Path(cfg["tier0_config"]), Path(cfg["recording"]), Path(cfg["output_dir"]))


@dataclass(frozen=True)
class Tier0FileModeResult:
    """파일 모드 실행 결과: 남긴 I1 기록 경로와 실행한 Tier 0 빌드."""

    log_paths: tuple[Path, ...]
    tier0_build: str


class Tier0FileModeRunner(ABC):
    """Tier 0 서비스를 파일 입력 모드로 실행한다. 구현은 Tier 0 조립(services.tier0) 뒤에 둔다.

    구현 조건: 실시간 경로와 같은 Tier 0 코드(검출·추적·삼각측량·접지)를 그대로 쓰고, 알림 채널로는 아무것도
    보내지 않는다(불변 규칙 5: LIVE 만 실제 알림). 기록 형식은 I1 메시지 JSONL 이다(`source.JsonlTier0LogSource`).
    """

    @abstractmethod
    def run(self, spec: Tier0FileModeSpec) -> Tier0FileModeResult:
        """spec 대로 Tier 0 를 돌리고 기록 경로를 돌려준다."""


def tier0_log_provider_from_config(
    cfg: Mapping[str, Any], runner: Tier0FileModeRunner | None = None
) -> Tier0LogWorldTrackProvider:
    """설정(`world_tracks.tier0_log`) → Tier0LogWorldTrackProvider."""
    _check_keys(cfg, CONFIG_KEYS, "tier0_log")
    if ("log_paths" in cfg) == ("file_mode" in cfg):
        raise ValueError("tier0_log: log_paths 와 file_mode 중 정확히 하나를 준다")
    kwargs: dict[str, Any] = {
        "t_start_ms": cfg.get("t_start_ms"),
        "t_end_ms": cfg.get("t_end_ms"),
        "max_obs_offset_ms": cfg.get("max_obs_offset_ms", DEFAULT_MAX_OBS_OFFSET_MS),
    }
    if "log_paths" in cfg:
        paths = cfg["log_paths"]
        paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
        return Tier0LogWorldTrackProvider.from_paths(paths, tier0_build=cfg.get("tier0_build"), **kwargs)

    spec = Tier0FileModeSpec.from_mapping(cfg["file_mode"])
    if "tier0_build" in cfg:
        raise ValueError("tier0_log: file_mode 에서는 tier0_build 를 실행기가 정한다 (설정에 쓰지 않는다)")
    if runner is None:
        raise NotImplementedError(
            "tier0_log.file_mode: Tier 0 파일 입력 모드 실행기가 아직 없다(services.tier0 조립 후속). "
            "지금은 이미 남긴 기록을 log_paths 로 준다"
        )
    result = runner.run(spec)
    return Tier0LogWorldTrackProvider.from_paths(list(result.log_paths), tier0_build=result.tier0_build, **kwargs)


def _check_keys(
    cfg: Mapping[str, Any], allowed: frozenset[str], where: str, required: frozenset[str] = frozenset()
) -> None:
    if not isinstance(cfg, Mapping):
        raise TypeError(f"{where}: 설정은 객체여야 한다")
    unknown = sorted(set(cfg) - allowed)
    if unknown:
        raise ValueError(f"{where}: 모르는 설정 키 {unknown} (쓸 수 있는 키: {sorted(allowed)})")
    missing = sorted(required - set(cfg))
    if missing:
        raise ValueError(f"{where}: 필수 설정 키 {missing} 가 없다")
