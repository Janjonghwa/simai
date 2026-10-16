"""Provider 출처를 소비 결과물의 input_provenance 로 옮긴다 (명세 20.4 규칙 2·3)."""

from __future__ import annotations

from typing import Any

from simai_ai.providers.base import (
    CalibrationProvenance,
    CalibrationProvider,
    FrameSource,
    FramesProvenance,
    TracksProvenance,
    WorldTrackProvider,
)

# 정답·시뮬레이션 입력. 하나라도 있으면 결과 지표는 "입력이 완벽할 때의 상한"이다 (명세 20.4 규칙 3).
UPPER_BOUND_FRAME_SOURCES = frozenset({"SIM"})
UPPER_BOUND_CALIBRATION_SOURCES = frozenset({"DATASET_GT", "SIM"})
UPPER_BOUND_TRACK_SOURCES = frozenset({"DATASET_GT", "SIM"})


def is_upper_bound(
    frames: FramesProvenance,
    calibration: CalibrationProvenance | None = None,
    tracks: TracksProvenance | None = None,
) -> bool:
    """입력 출처 중 하나라도 DATASET_GT 또는 SIM 이면 True. 공개 데이터 영상(frames DATASET)은 정답이 아니다."""
    return (
        frames["source"] in UPPER_BOUND_FRAME_SOURCES
        or (calibration is not None and calibration["source"] in UPPER_BOUND_CALIBRATION_SOURCES)
        or (tracks is not None and tracks["source"] in UPPER_BOUND_TRACK_SOURCES)
    )


def build_input_provenance(
    frames: FramesProvenance,
    calibration: CalibrationProvenance | None = None,
    tracks: TracksProvenance | None = None,
) -> dict[str, Any]:
    """`common/defs.schema.json#/$defs/input_provenance` 문서를 만든다.

    calibration 은 이미지만 넣어 모델이 카메라를 스스로 추정하는 Run 에서 생략한다.
    tracks 는 사람 관련 결과물에서 넣는다. 조각은 복사해 넣으므로 원본을 바꿔도 결과가 바뀌지 않는다.
    """
    provenance: dict[str, Any] = {"frames": dict(frames)}
    if calibration is not None:
        provenance["calibration"] = dict(calibration)
    if tracks is not None:
        provenance["tracks"] = dict(tracks)
    provenance["is_upper_bound"] = is_upper_bound(frames, calibration, tracks)
    return provenance


def provenance_from_providers(
    frame_source: FrameSource,
    calibration_provider: CalibrationProvider | None = None,
    track_provider: WorldTrackProvider | None = None,
) -> dict[str, Any]:
    """Provider 들의 provenance() 를 그대로 모아 input_provenance 를 만든다."""
    return build_input_provenance(
        frame_source.provenance(),
        calibration_provider.provenance() if calibration_provider is not None else None,
        track_provider.provenance() if track_provider is not None else None,
    )
