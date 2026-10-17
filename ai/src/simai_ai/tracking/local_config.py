"""Local Tracker 설정: 추적기 종류와 명시 파라미터 (명세 5.1, 5.2 추적 3계층, 수정 7, 요구사항 FR-57).

- 기본 추적기는 TrackTrack(Ultralytics `tracktrack.yaml`), 설정 전환으로 ByteTrack(`bytetrack.yaml`)·
  BoT-SORT(`botsort.yaml`, ReID 선택)를 쓴다.
- 추적기 설정은 모든 키를 명시한다. Ultralytics 추적기는 빠진 키를 자기 기본값(`getattr(args, key, 기본값)`)으로
  채우므로, 여기서는 종류별 키 목록을 정확히 요구하고 빠지거나 모르는 키는 오류로 낸다.
- 고정 카메라라 카메라 움직임 보정(GMC)은 끈다(`gmc_method: none`). 다른 값은 받지 않는다.
- ReID 는 검출기 자체 특징(`model: auto`)만 받는다. 외부 ReID 모델은 영상 crop 이 필요해 이미지 의존이 생기므로
  여기서 막는다(필요해지면 A2, 명세 15.4).

기본값은 ultralytics 8.4.178 의 `cfg/trackers/*.yaml` 값을 옮겨 적고 GMC 만 껐다. 교실 값 조정은 G2·A0 에서 한다.
`track_buffer` 는 Ultralytics 와 같이 **프레임 수**다(이 추적기에 넣은 프레임 기준).

표준 라이브러리만 쓴다(ultralytics 없이 import 된다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class TrackerKind(StrEnum):
    """Local Tracker 종류. 값은 Ultralytics `tracker_type` 과 같다."""

    TRACKTRACK = "tracktrack"
    BYTETRACK = "bytetrack"
    BOTSORT = "botsort"


DEFAULT_TRACKER_KIND = TrackerKind.TRACKTRACK
"""명세 5.2 수정 7: 카메라별 독립 TrackTrack 이 기본"""

GMC_OFF = "none"
REID_NATIVE = "auto"

_BYTETRACK: dict[str, Any] = {
    "tracker_type": "bytetrack",
    "track_high_thresh": 0.25,
    "track_low_thresh": 0.1,
    "new_track_thresh": 0.25,
    "track_buffer": 30,
    "match_thresh": 0.8,
    "fuse_score": True,
}

_BOTSORT: dict[str, Any] = {
    "tracker_type": "botsort",
    "track_high_thresh": 0.25,
    "track_low_thresh": 0.1,
    "new_track_thresh": 0.25,
    "track_buffer": 30,
    "match_thresh": 0.8,
    "fuse_score": True,
    "gmc_method": GMC_OFF,
    "proximity_thresh": 0.5,
    "appearance_thresh": 0.8,
    "with_reid": False,
    "model": REID_NATIVE,
}

_TRACKTRACK: dict[str, Any] = {
    "tracker_type": "tracktrack",
    "track_high_thresh": 0.6,
    "track_low_thresh": 0.25,
    "new_track_thresh": 0.7,
    "track_buffer": 30,
    "match_thresh": 0.7,
    "lost_match_thr": 0.0,
    "iou_weight": 0.5,
    "reid_weight": 0.5,
    "conf_weight": 0.1,
    "angle_weight": 0.05,
    "penalty_p": 0.2,
    "penalty_q": 0.4,
    "reduce_step": 0.05,
    "tai_thr": 0.55,
    "min_track_len": 3,
    "gmc_method": GMC_OFF,
    "with_reid": False,
    "model": REID_NATIVE,
}

DEFAULT_PARAMS: Mapping[TrackerKind, Mapping[str, Any]] = MappingProxyType(
    {
        TrackerKind.TRACKTRACK: MappingProxyType(_TRACKTRACK),
        TrackerKind.BYTETRACK: MappingProxyType(_BYTETRACK),
        TrackerKind.BOTSORT: MappingProxyType(_BOTSORT),
    }
)
"""종류별 명시 파라미터. 키 목록이 곧 그 종류가 요구하는 키 전부다."""

_INT_KEYS = {"track_buffer", "min_track_len"}
_BOOL_KEYS = {"fuse_score", "with_reid"}
_STR_KEYS = {"tracker_type", "gmc_method", "model"}
_UNIT_KEYS = {"track_high_thresh", "track_low_thresh", "new_track_thresh", "match_thresh", "proximity_thresh"}
_UNIT_KEYS |= {"appearance_thresh", "tai_thr", "lost_match_thr"}


def _check_value(key: str, value: Any) -> None:
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise TypeError(f"{key}: bool 이어야 한다 (받은 값 {value!r})")
    elif key in _STR_KEYS:
        if not isinstance(value, str) or not value:
            raise TypeError(f"{key}: 빈 문자열이 아닌 str 이어야 한다 (받은 값 {value!r})")
    elif key in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key}: 1 이상 정수여야 한다 (받은 값 {value!r})")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
            raise ValueError(f"{key}: 0 이상의 유한한 수여야 한다 (받은 값 {value!r})")
        if key in _UNIT_KEYS and value > 1:
            raise ValueError(f"{key}: 0~1 이어야 한다 (받은 값 {value!r})")


@dataclass(frozen=True)
class TrackerConfig:
    """카메라 하나의 Local Tracker 설정. 모든 카메라가 같은 설정의 독립 인스턴스를 쓴다."""

    kind: TrackerKind
    params: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        kind = TrackerKind(self.kind)
        object.__setattr__(self, "kind", kind)
        params = dict(self.params)
        expected = set(DEFAULT_PARAMS[kind])
        missing, unknown = sorted(expected - set(params)), sorted(set(params) - expected)
        if missing or unknown:
            raise ValueError(f"{kind} 설정 키가 맞지 않다: 빠짐 {missing}, 모름 {unknown} (모든 키를 명시한다)")
        for key, value in params.items():
            _check_value(key, value)
        if params["tracker_type"] != kind.value:
            raise ValueError(f"tracker_type {params['tracker_type']!r} 가 종류 {kind.value!r} 와 다르다")
        if params["track_low_thresh"] > params["track_high_thresh"]:
            raise ValueError("track_low_thresh 는 track_high_thresh 이하여야 한다")
        if "gmc_method" in params and params["gmc_method"] != GMC_OFF:
            raise ValueError(f"고정 카메라라 카메라 움직임 보정을 끈다: gmc_method 는 {GMC_OFF!r} 만 받는다")
        if "model" in params and params["model"] != REID_NATIVE:
            raise ValueError(
                f"ReID 는 검출기 특징(model {REID_NATIVE!r})만 받는다. 외부 ReID 모델은 영상 crop 이 필요해 막는다"
            )
        object.__setattr__(self, "params", MappingProxyType(params))

    @property
    def with_reid(self) -> bool:
        return bool(self.params.get("with_reid", False))

    @property
    def track_buffer_frames(self) -> int:
        return int(self.params["track_buffer"])

    def as_dict(self) -> dict[str, Any]:
        """Ultralytics 추적기 생성자에 넘길 키·값 (사본)."""
        return dict(self.params)

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any]) -> TrackerConfig:
        """`tracker_type` 을 포함한 설정 묶음(예: YAML 을 읽은 dict)에서 만든다. 키는 모두 있어야 한다."""
        if "tracker_type" not in doc:
            raise ValueError("tracker_type 이 없다")
        return cls(TrackerKind(doc["tracker_type"]), doc)


def tracker_config(kind: TrackerKind | str = DEFAULT_TRACKER_KIND, **overrides: Any) -> TrackerConfig:
    """종류의 명시 기본값에 일부 값을 바꾼 설정. 예: `tracker_config("botsort", with_reid=True)`."""
    kind = TrackerKind(kind)
    unknown = sorted(set(overrides) - set(DEFAULT_PARAMS[kind]))
    if unknown:
        raise ValueError(f"{kind} 에 없는 설정 키: {unknown}")
    return TrackerConfig(kind, {**DEFAULT_PARAMS[kind], **overrides})
