"""I1 `world_state` 문서 조립 (contracts/messages/world_state.schema.json, 명세 5.2 World 위치의 정의, 6.1, 12.3).

World Track Manager 결과(`world_track.WorldTrackFrame`)를 계약 형식으로 옮긴다. 필드를 더하거나 이름을 바꾸지 않는다.

- 관측 상태(observation_state)는 생명주기와 따로 시간 모델로 정한다: gap = t_latest_global − t_last_valid 를
  `time_model.classify_observation` 에 넣는다(0~200 ms OBSERVED, ~500 ms PREDICTED, 넘으면 UNKNOWN, 명세 6.1).
- 모르는 값은 비운다. 접지(`ground_anchor_xyz`, 지지 표면)는 ContactEstimator 결과(`SupportContact`)가 있을 때만
  싣고, 없으면 null·UNKNOWN·UNAVAILABLE 이다. Z=0 으로 채우지 않는다(AGENTS.md 규칙 2).
- `head_z` 는 관절 유효성 게이트가 VALID 로 낸 값만 싣는다. UNCERTAIN·UNAVAILABLE 이면 null 이다(계약에
  head_z 유효성 필드가 없어 UNCERTAIN 값이 확정값처럼 실리지 않게, 사용자 결정·docs/design/tier0-joint-gate.md).
- `skeleton`·`pelvis_xyz` 는 이번 시각에 클러스터가 붙은 Track 만 있고, 관절은 모두 Tier 0 관측(joint_source observed)이다.
- `position_xy` 는 관측이 없을 때 t_last_valid 위치를 유지하고 σ 만 키운 값이다(계약 1.1.0 설명, world_track 이 만든다).
- `input_provenance`(계약 1.1.0 선택 필드)는 `WorldTrackFrame.input_provenance` 가 있으면 payload 에 그대로 싣는다
  (관측과 추정 구분, SIM·DATASET_GT 입력이면 is_upper_bound true).
현재 시각을 읽지 않는다. 메시지 봉투의 msg_id·sent_at 은 호출자가 넣는다.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simai_ai.tracking.time_model import TimeModelConfig, classify_observation, require_ms
from simai_ai.tracking.world_track import WorldTrackFrame, WorldTrackSnapshot

WORLD_STATE_SCHEMA_VERSION = "1.1.0"
"""world_state 계약이 마지막으로 바뀐 버전 (1.1.0: input_provenance 선택 필드, position_xy 설명 보강)"""
DEFAULT_SOURCE_ID = "tier0-01"
_VALIDITY = frozenset({"VALID", "UNCERTAIN", "UNAVAILABLE"})
_SURFACE_VALIDITY = frozenset({"VALID", "UNCERTAIN", "UNKNOWN"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class SupportContact:
    """ContactEstimator 결과 한 사람분 (명세 5.2 접지 추정). 기본값은 "모름"이다.

    지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 이면 ground_anchor_xyz 는 None 이어야 한다(계약 불변 규칙).
    ContactEstimator 는 아직 없으므로 지금은 기본값(`UNKNOWN_CONTACT`)만 쓴다.
    """

    ground_anchor_xyz: tuple[float, float, float] | None = None
    ground_anchor_sigma: tuple[float, float, float] | None = None
    support_surface_id: str | None = None
    support_surface_validity: str = "UNKNOWN"
    contact_validity: str = "UNAVAILABLE"
    z_validity: str = "UNAVAILABLE"

    def __post_init__(self) -> None:
        if self.support_surface_validity not in _SURFACE_VALIDITY:
            raise ValueError(f"support_surface_validity 값이 아니다: {self.support_surface_validity}")
        for name in ("contact_validity", "z_validity"):
            if getattr(self, name) not in _VALIDITY:
                raise ValueError(f"{name} 값이 아니다: {getattr(self, name)}")
        if self.ground_anchor_xyz is not None:
            if self.support_surface_validity != "VALID" or self.z_validity == "UNAVAILABLE":
                raise ValueError("지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 이면 ground_anchor_xyz 는 비운다")
            _require_finite("ground_anchor_xyz", self.ground_anchor_xyz, 3)
        elif self.ground_anchor_sigma is not None:
            raise ValueError("ground_anchor_xyz 가 없는데 ground_anchor_sigma 가 있다")
        if self.ground_anchor_sigma is not None:
            _require_finite("ground_anchor_sigma", self.ground_anchor_sigma, 3)


UNKNOWN_CONTACT = SupportContact()


def world_track_document(
    track: WorldTrackSnapshot,
    t_latest_global: int,
    contact: SupportContact = UNKNOWN_CONTACT,
    time_config: TimeModelConfig = TimeModelConfig(),
) -> dict[str, Any]:
    """`world_state.schema.json#/$defs/world_track` 문서 하나.

    t_latest_global 이 t_last_valid 보다 앞이면 입력 불일치라 ValueError 다(0 으로 덮지 않는다, time_model 과 같음).
    """
    require_ms("t_latest_global", t_latest_global)
    gap = t_latest_global - track.t_last_valid
    if gap < 0:
        raise ValueError(f"{track.world_id}: t_last_valid {track.t_last_valid} 가 t_latest_global {t_latest_global} 보다 뒤다")
    _require_finite("position_xy", track.position_xy, 2)
    _require_finite("position_xy_sigma", track.position_xy_sigma, 2)
    head = track.head_z
    head_valid = head is not None and head.value is not None and str(head.validity) == "VALID"
    doc: dict[str, Any] = {
        "world_id": track.world_id,
        "t_last_valid": track.t_last_valid,
        "observation_state": str(classify_observation(gap, time_config)),
        "track_lifecycle": str(track.lifecycle),
        "identity_confidence": str(track.identity_confidence),
        "identity_revision": track.identity_revision,
        "position_xy": [track.position_xy[0], track.position_xy[1]],
        "position_xy_sigma": [track.position_xy_sigma[0], track.position_xy_sigma[1]],
        "pelvis_xyz": list(track.pelvis_xyz) if track.pelvis_xyz is not None else None,
        "ground_anchor_xyz": list(contact.ground_anchor_xyz) if contact.ground_anchor_xyz is not None else None,
        "support_surface_id": contact.support_surface_id,
        "support_surface_validity": contact.support_surface_validity,
        "contact_validity": contact.contact_validity,
        "z_validity": contact.z_validity,
        "head_z": head.value if head_valid else None,  # type: ignore[union-attr]
        "head_z_sigma": head.sigma_m if head_valid else None,  # type: ignore[union-attr]
        "velocity": list(track.velocity),
        "cameras": list(track.cameras),
    }
    if contact.ground_anchor_sigma is not None:
        doc["ground_anchor_sigma"] = list(contact.ground_anchor_sigma)
    if track.skeleton is not None:
        doc["skeleton"] = track.skeleton.to_skeleton()
    return doc


def world_state_payload(
    frame: WorldTrackFrame,
    t_latest_global: int,
    scene_version: str,
    contacts: Mapping[str, SupportContact] | None = None,
    time_config: TimeModelConfig = TimeModelConfig(),
) -> dict[str, Any]:
    """I1 world_state 의 payload. contacts 는 World ID → ContactEstimator 결과(없는 ID 는 모름).

    frame.input_provenance 가 있으면 payload.input_provenance 로 싣는다(복사본). 없으면 필드를 넣지 않는다.
    """
    if not scene_version:
        raise ValueError("scene_version 이 비었다")
    contacts = contacts or {}
    unknown = sorted(set(contacts) - {tr.world_id for tr in frame.tracks})
    if unknown:
        raise ValueError(f"contacts 에 이 시각 Track 이 아닌 World ID 가 있다: {unknown}")
    payload: dict[str, Any] = {
        "t_latest_global": require_ms("t_latest_global", t_latest_global),
        "calibration_version": frame.calibration_version,
        "scene_version": scene_version,
    }
    if frame.input_provenance is not None:
        payload["input_provenance"] = copy.deepcopy(dict(frame.input_provenance))
    payload["tracks"] = [
        world_track_document(tr, t_latest_global, contacts.get(tr.world_id, UNKNOWN_CONTACT), time_config)
        for tr in frame.tracks
    ]
    return payload


def world_state_message(
    payload: Mapping[str, Any], *, msg_id: str, sent_at: int, source_id: str = DEFAULT_SOURCE_ID
) -> dict[str, Any]:
    """공통 봉투에 넣은 world_state 메시지. msg_id(UUID)와 sent_at(UTC ms)은 호출자가 넣는다."""
    if not _UUID_RE.match(msg_id):
        raise ValueError(f"msg_id 는 소문자 UUID 여야 한다: {msg_id!r}")
    if not source_id:
        raise ValueError("source_id 가 비었다")
    return {
        "schema_version": WORLD_STATE_SCHEMA_VERSION,
        "msg_type": "world_state",
        "msg_id": msg_id,
        "sent_at": require_ms("sent_at", sent_at),
        "source_id": source_id,
        "payload": dict(payload),
    }


def _require_finite(name: str, values: Any, n: int) -> None:
    if len(values) != n or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError(f"{name} 는 유한한 수 {n}개여야 한다: {values!r}")
