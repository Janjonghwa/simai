"""keypoint 시퀀스 파일(`simai.keypoint_sequences`)과 번들 조정 관측 만들기 (명세 4.3 3단계 입력).

파일은 JSONL 이다. 첫 줄은 머리(형식·출처·검출기·카메라별 집계), 다음 줄부터 프레임 한 장씩이다. 프레임 줄은 시뮬레이터
`observations/*.jsonl` 과 같은 모양이다(camera_id, stream_epoch, frame_sequence, estimated_capture_time_ms,
timestamp_source, timestamp_uncertainty_ms, detections[{bbox, score, keypoints_2d}]). 영상·이미지·crop 은 넣지 않는다
(11.5, AGENTS 8). 추출(`extract`, GPU 세션 또는 노트북)과 번들 조정(`cli adjust`)을 이 파일로 나눈다.

사람 대응은 한 명 걷기 규칙이다: 카메라 한 대의 한 프레임에 신뢰도 기준을 넘는 사람이 정확히 한 명일 때만 그 사람을 보정
걷는 사람으로 본다. 두 명 이상 보인 프레임은 대응을 모르므로 쓰지 않고 센다(지어내지 않는다). 카메라 간 시각 차이는
`geometry.interpolation.interpolate_views` 로 공통 시각에 맞춘다(외삽 없음, 시각 불확실성 기준을 넘는 프레임 제외).
shortcut: 여러 명 걷기의 카메라 간 대응(Cross-view 연결)은 하지 않는다. 여러 명 보정 걷기를 쓸 때 대응 단계를 붙인다.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_ai.calibration.bundle.inputs import PersonObservation
from simai_ai.calibration.bundle.skeleton import SUPPORTED_COUNTS
from simai_ai.geometry.interpolation import (
    InterpolationConfig,
    KeypointSample,
    interpolate_views,
)
from simai_ai.providers.contract import check_input_provenance
from simai_ai.tracking.time_model import require_ms

KEYPOINT_FILE_FORMAT = "simai.keypoint_sequences"
KEYPOINT_FILE_VERSION = "0.1.0"
WALKER_ID = "walker"


class CorrespondenceError(ValueError):
    """keypoint 파일로 번들 조정 관측을 만들 수 없다(공통 시각 없음, 한 명 프레임 없음)."""


# --------------------------------------------------------------------------- 파일


def write_keypoint_file(path: Path, header: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> int:
    """머리 + 프레임 줄을 쓴다. 쓴 프레임 수를 돌려준다. 같은 파일이 있으면 덮어쓰지 않는다."""
    check_header(header)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False, allow_nan=False) + "\n")
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            n += 1
    return n


def read_keypoint_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"{path}: 빈 파일이다")
    header = json.loads(lines[0])
    check_header(header)
    rows = [json.loads(ln) for ln in lines[1:]]
    known = {c["camera_id"] for c in header["cameras"]}
    for row in rows:
        if row.get("camera_id") not in known:
            raise ValueError(f"{path}: 머리 cameras 에 없는 카메라의 프레임이다: {row.get('camera_id')!r}")
        require_ms("estimated_capture_time_ms", row["estimated_capture_time_ms"])
    return header, rows


def check_header(header: Mapping[str, Any]) -> None:
    if header.get("format") != KEYPOINT_FILE_FORMAT:
        raise ValueError(f"keypoint 시퀀스 파일 형식이 아니다: {header.get('format')!r}")
    require_ms("created_at", header.get("created_at"))
    problems = check_input_provenance(header.get("input_provenance") or {})
    if problems:
        raise ValueError(f"keypoint 파일 input_provenance 가 계약에 맞지 않는다: {problems}")
    cams = header.get("cameras") or []
    ids = [c.get("camera_id") for c in cams]
    if not cams or len(set(ids)) != len(ids):
        raise ValueError(f"keypoint 파일 cameras 가 비었거나 camera_id 가 겹친다: {ids}")
    for c in cams:
        size = c.get("image_size")
        if not (isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and v > 0 for v in size)):
            raise ValueError(f"{c.get('camera_id')}: image_size 가 [width, height] 양의 정수가 아니다: {size!r}")


# --------------------------------------------------------------------------- 관측


@dataclass(frozen=True)
class CorrespondenceConfig:
    min_person_score: float = 0.5
    """이 값보다 검출 신뢰도가 낮은 사람은 세지 않는다(한 명 판단·관측 모두)"""
    sample_period_ms: int = 300
    """공통 시각 간격. 번들 조정 접지 판단의 이웃 표본 창(BundleConfig.stance_window_ms 300)과 맞춘다"""
    max_ambiguous_ratio: float = 0.2
    """사람이 보인 프레임 중 두 명 이상 보인 프레임 비율이 이보다 크면 경고한다(한 명 걷기 규칙이 맞지 않는 녹화)"""
    min_observations: int = 30
    """만든 관측(2대 이상에서 보인 공통 시각) 수가 이보다 적으면 경고한다"""
    interpolation: InterpolationConfig = field(default_factory=InterpolationConfig)

    def __post_init__(self) -> None:
        if self.sample_period_ms < 1:
            raise ValueError(f"sample_period_ms 는 1 이상이다: {self.sample_period_ms}")
        if not 0 <= self.min_person_score <= 1:
            raise ValueError(f"min_person_score 는 0~1 이다: {self.min_person_score}")


@dataclass(frozen=True)
class CorrespondenceReport:
    observations: tuple[PersonObservation, ...]
    warnings: tuple[str, ...]
    record: Mapping[str, Any]


def build_observations(
    header: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], config: CorrespondenceConfig | None = None
) -> CorrespondenceReport:
    """한 명 걷기 규칙으로 카메라별 keypoint 시퀀스 → 공통 시각 PersonObservation.

    한 명 프레임이 있는 카메라가 2대 미만이거나 관측이 하나도 없으면 CorrespondenceError. 두 명 이상 프레임 비율이 크거나
    관측이 적으면 경고(warnings)만 낸다. 판정은 번들 조정·criteria 가 한다.
    """
    config = config or CorrespondenceConfig()
    camera_ids = [c["camera_id"] for c in header["cameras"]]
    samples: dict[str, list[KeypointSample]] = defaultdict(list)
    counts = {cid: {"frames": 0, "empty": 0, "single": 0, "ambiguous": 0} for cid in camera_ids}
    for row in sorted(rows, key=lambda r: (r["camera_id"], r["estimated_capture_time_ms"])):
        cid = row["camera_id"]
        people = [d for d in row.get("detections") or [] if float(d.get("score", 0.0)) >= config.min_person_score]
        c = counts[cid]
        c["frames"] += 1
        if not people:
            c["empty"] += 1
            continue
        if len(people) > 1:
            c["ambiguous"] += 1  # 누가 보정 걷는 사람인지 모른다: 쓰지 않는다
            continue
        kps = tuple(_keypoint(k) for k in people[0]["keypoints_2d"])
        if len(kps) not in SUPPORTED_COUNTS:
            raise ValueError(f"{cid}: keypoint 수 {len(kps)} 는 번들 조정이 받지 않는다({SUPPORTED_COUNTS})")
        c["single"] += 1
        samples[cid].append(
            KeypointSample(
                camera_id=cid,
                estimated_capture_time=int(row["estimated_capture_time_ms"]),
                timestamp_source=row["timestamp_source"],
                timestamp_uncertainty_ms=row.get("timestamp_uncertainty_ms"),
                keypoints=kps,
            )
        )
    warnings: list[str] = []
    for cid, c in counts.items():
        seen = c["single"] + c["ambiguous"]
        if seen and c["ambiguous"] / seen > config.max_ambiguous_ratio:
            warnings.append(
                f"{cid}: 사람이 보인 프레임 {seen} 중 {c['ambiguous']} 에 두 명 이상이 보여 쓰지 않았다"
                f"(한 명 걷기 규칙. 여러 명 걷기는 대응 단계가 없다)"
            )
    used = [cid for cid in camera_ids if samples.get(cid)]
    record: dict[str, Any] = {
        "rule": "SINGLE_PERSON_PER_FRAME",
        "min_person_score": config.min_person_score,
        "sample_period_ms": config.sample_period_ms,
        "interpolation": {
            "window_ms": config.interpolation.window_ms,
            "max_timestamp_uncertainty_ms": config.interpolation.max_timestamp_uncertainty_ms,
        },
        "cameras": {cid: dict(c) for cid, c in counts.items()},
        "cameras_with_single_person": used,
    }
    if len(used) < 2:
        raise CorrespondenceError(f"한 명만 보인 프레임이 있는 카메라가 2대 미만이다: {used} (집계 {counts})")
    start = min(samples[cid][0].estimated_capture_time for cid in used)
    end = max(samples[cid][-1].estimated_capture_time for cid in used)
    observations: list[PersonObservation] = []
    view_counts: dict[int, int] = defaultdict(int)
    drops: dict[str, int] = defaultdict(int)
    by_camera = {cid: samples[cid] for cid in used}
    for t in range(start, end + 1, config.sample_period_ms):
        views, dropped = interpolate_views(by_camera, t, config.interpolation)
        for d in dropped:
            drops[str(d.reason)] += 1
        view_counts[len(views)] += 1
        if len(views) >= 2:
            observations.append(PersonObservation(WALKER_ID, t, {cid: v.keypoints for cid, v in views.items()}))
    record.update(
        {
            "time_range_ms": [start, end],
            "sample_times": sum(view_counts.values()),
            "observations": len(observations),
            "views_per_sample": {str(k): v for k, v in sorted(view_counts.items())},
            "interpolation_drops": dict(drops),
        }
    )
    if not observations:
        raise CorrespondenceError(f"2대 이상에서 같은 시각에 한 명이 보인 관측이 없다 (시각 {start}~{end})")
    if len(observations) < config.min_observations:
        warnings.append(f"관측이 {len(observations)} 개뿐이다(권장 {config.min_observations} 이상). 걷기를 길게 한다")
    record["warnings"] = list(warnings)
    return CorrespondenceReport(tuple(observations), tuple(warnings), record)


def _keypoint(k: Any) -> tuple[float, float, float] | None:
    if k is None or len(k) < 3 or not all(isinstance(v, int | float) and math.isfinite(v) for v in k[:3]):
        return None
    return (float(k[0]), float(k[1]), float(k[2]))
