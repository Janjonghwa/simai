"""설정으로 WorldTrackProvider 를 고른다 (명세 20.4: 상류가 준비되면 설정만 바꿔 실제 구현으로 교체).

소비하는 코드(EXP 파이프라인)는 `world_track_provider_from_config(cfg)` 만 부르고, 출처는 설정의 `source` 로 바꾼다.

    {"source": "SIM",        "sim":        {"path": "<sim 출력 디렉터리 또는 world_tracks.jsonl>"}}
    {"source": "DATASET_GT", "dataset_gt": {"dataset": "panoptic", "root": "...", "camera_ids": [...]}}
    {"source": "DATASET_GT", "dataset_gt": {"dataset": "file", "path": "<변환해 둔 world_tracks.jsonl>"}}
    {"source": "TIER0_LOG",  "tier0_log":  {"log_paths": ["..."]}}   # 키는 tier0_log.config

파일을 읽는 출처(SIM, DATASET_GT file)는 파일 문서의 provider_source 가 설정의 source 와 같은지 확인한다.
정답·시뮬레이션 입력이 다른 출처로 표시되면 input_provenance.is_upper_bound 가 틀어지기 때문이다(규칙 3).
TIER0_LIVE 는 실시간 경로(I1 수신)라 이 선택 대상이 아니다.
무거운 의존성(geometry 등)을 쓰는 구현은 고를 때만 import 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simai_ai.providers.base import WorldTrackProvider
from simai_ai.providers.files import FileWorldTrackProvider
from simai_ai.providers.tier0_log.config import Tier0FileModeRunner, tier0_log_provider_from_config

SELECTABLE_TRACK_SOURCES = ("SIM", "DATASET_GT", "TIER0_LOG")
SIM_WORLD_TRACKS = "world_tracks.jsonl"
PANOPTIC_KEYS = frozenset(
    {
        "dataset",
        "root",
        "camera_ids",
        "sequence",
        "floor_y_cm",
        "epoch_ms",
        "pose_dir",
        "min_joint_confidence",
        "video_uri_root",
    }
)


def world_track_provider_from_config(
    cfg: Mapping[str, Any], *, tier0_runner: Tier0FileModeRunner | None = None
) -> WorldTrackProvider:
    """설정 → WorldTrackProvider. tier0_runner 는 TIER0_LOG file_mode 에서만 쓴다(`Tier0FileModeRunner`)."""
    if not isinstance(cfg, Mapping):
        raise TypeError("world_tracks 설정은 객체여야 한다")
    source = cfg.get("source")
    if source not in SELECTABLE_TRACK_SOURCES:
        raise ValueError(f"world_tracks.source {source!r} 는 {SELECTABLE_TRACK_SOURCES} 중 하나여야 한다")
    key = source.lower()
    unknown = sorted(set(cfg) - {"source", key})
    if unknown or key not in cfg:
        raise ValueError(f"world_tracks: source={source} 는 '{key}' 설정 하나만 쓴다 (모르는 키 {unknown})")
    sub = cfg[key]
    if not isinstance(sub, Mapping):
        raise TypeError(f"world_tracks.{key} 설정은 객체여야 한다")

    if source == "TIER0_LOG":
        return tier0_log_provider_from_config(sub, runner=tier0_runner)
    if source == "SIM":
        _only(sub, {"path"}, "sim")
        path = Path(sub["path"])
        return _file_provider(path / SIM_WORLD_TRACKS if path.is_dir() else path, "SIM")
    return _dataset_gt(sub)


def _dataset_gt(sub: Mapping[str, Any]) -> WorldTrackProvider:
    dataset = sub.get("dataset")
    if dataset == "file":
        _only(sub, {"dataset", "path"}, "dataset_gt")
        return _file_provider(Path(sub["path"]), "DATASET_GT")
    if dataset == "panoptic":
        _only(sub, PANOPTIC_KEYS, "dataset_gt", required={"dataset", "root"})
        from simai_ai.providers.dataset_gt import PanopticCalibrationProvider, PanopticWorldTrackProvider

        cal_kw = {k: sub[k] for k in ("sequence", "floor_y_cm", "epoch_ms") if k in sub}
        calibration = PanopticCalibrationProvider(sub["root"], sub.get("camera_ids"), **cal_kw)
        track_kw = {k: sub[k] for k in ("pose_dir", "min_joint_confidence", "video_uri_root") if k in sub}
        return PanopticWorldTrackProvider(calibration, **track_kw)
    raise ValueError(f"world_tracks.dataset_gt.dataset {dataset!r} 는 'panoptic' 또는 'file' 이어야 한다")


def _file_provider(path: Path, expected: str) -> FileWorldTrackProvider:
    provider = FileWorldTrackProvider(path)
    if provider.source != expected:
        raise ValueError(
            f"{path}: 문서 provider_source {provider.source} 가 설정한 출처 {expected} 와 다르다 "
            "(출처가 틀리면 is_upper_bound 가 틀어진다)"
        )
    return provider


def _only(
    sub: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    where: str,
    required: set[str] | frozenset[str] | None = None,
) -> None:
    unknown = sorted(set(sub) - set(allowed))
    if unknown:
        raise ValueError(f"world_tracks.{where}: 모르는 설정 키 {unknown} (쓸 수 있는 키: {sorted(allowed)})")
    missing = sorted(set(required if required is not None else allowed) - set(sub))
    if missing:
        raise ValueError(f"world_tracks.{where}: 필수 설정 키 {missing} 가 없다")
