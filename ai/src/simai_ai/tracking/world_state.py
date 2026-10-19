"""I1 `world_state` 문서 조립 (contracts/messages/world_state.schema.json, 명세 5.2 World 위치의 정의, 6.1, 12.3).

World Track Manager 결과(`world_track.WorldTrackFrame`)를 계약 형식으로 옮긴다. 필드를 더하거나 이름을 바꾸지 않는다.

- 관측 상태(observation_state)는 생명주기와 따로 시간 모델로 정한다: gap = t_latest_global − t_last_valid 를
  `time_model.classify_observation` 에 넣는다(0~200 ms OBSERVED, ~500 ms PREDICTED, 넘으면 UNKNOWN, 명세 6.1).
- 모르는 값은 비운다. 접지(`ground_anchor_xyz`, 지지 표면)는 ContactEstimator(`simai_ai.contact`) 결과
  (`SupportContact`)가 있을 때만 싣고, 없으면 null·UNKNOWN·UNAVAILABLE 이다. Z=0 으로 채우지 않는다(AGENTS.md 규칙 2).
- 지지 표면 출처(`SupportContact.surface_source`: observed = 이번 시각 발목 근거, inferred = 가려진 동안 마지막 유효
  지지 표면 유지)는 계약 1.3.0 선택 필드 `surface_source` 로 싣는다(AGENTS 규칙 7, 2026-10-18 승인, S15P31A101-170).
  접지점(ground_anchor_xyz)이 있을 때만, 그때는 늘 싣고, 접지점이 없으면 두지 않는다(SupportContact 불변식이 둘을 묶는다).
  유지 중이면 z_validity·contact_validity 가 UNCERTAIN 이고 ground_anchor_sigma 가 커진다.
- 별칭(`aliases`, 계약 1.3.0 선택 필드)은 World Track Manager 가 병합 때 남는 Track 에 붙인 이전 ID(`WorldTrackSnapshot.aliases`)를
  순서대로 싣는다. 별칭이 없으면 두지 않는다. 별칭 목록은 Track 이 살아 있는 동안 뒤에만 붙고(world_track `_merge`), 여기서는
  고르거나 빼지 않는다(덮어쓰지 않음). 중복은 한 번만 싣고, 자기 world_id 가 들어 있으면 입력 불일치라 ValueError 다.
- `head_z` 는 관절 유효성 게이트가 VALID 나 UNCERTAIN 으로 낸 값을 싣고, 그 유효성을 `head_z_validity` 로 늘 싣는다
  (계약 2.0.0, 사용자 결정 2026-10-19, S15P31A101-179. 2026-10-18 의 'VALID 일 때만' 을 대체). 접지점과 z_validity 의
  관계와 같다: VALID·UNCERTAIN 이면 값·σ, UNAVAILABLE 이면 null. 소비자는 값이 있어도 head_z_validity 를 확인한다.
  값을 싣는 조건(관측 관절만, AGENTS 규칙 1·7): 같은 Track 의 이번 시각 skeleton 이 있고, 게이트가 쓴 귀·눈
  (HeadZ.joints)이 모두 그 skeleton 에서 좌표가 있고 joint_source 가 observed 이며 유효성이 HeadZ 와 같아야 하고, UNCERTAIN
  이면 skeleton 에 VALID 귀·눈이 없어야 한다. 하나라도 어긋나면(skeleton 없음, 근거 관절 목록 없음, inferred 관절, 유효성
  불일치, VALID 귀·눈이 있는데 UNCERTAIN) 근거를 확인할 수 없으므로 UNAVAILABLE·null
  이다(모르면 비움). 게이트가 값을 못 냈거나(VALID·UNCERTAIN 인데 값이 없거나 유한하지 않음) 이번 시각 클러스터가 붙지
  않아 HeadZ 가 없으면 UNAVAILABLE 이다. σ 가 유한하지 않으면 σ 만 비운다(값과 유효성은 그대로).
- `skeleton`·`pelvis_xyz` 는 이번 시각에 클러스터가 붙은 Track 만 있고, 관절은 모두 Tier 0 관측(joint_source observed)이다.
- `position_xy` 는 명세 5.2 순서(골반 XY → 검증된 접지점 XY → 예측값)와 계약 1.1.0 설명(관측이 없으면 t_last_valid
  시각의 위치를 유지하고 σ 를 속도 상한 × 공백만큼 키운다, 속도로 옮기지 않음)을 함께 따른다. 위치 관측은 두 가지다:
  골반(world_track, 시각 = track.t_last_valid)과 ContactEstimator 가 발목으로 확인한 접지점(`SupportContact.observed_*`,
  시각 = observed_at). 더 최근 관측 하나를 고르고, 문서의 위치·σ·t_last_valid·observation_state 를 모두 그 관측 시각
  기준으로 맞춘다. 같은 시각이면 골반이 우선이다. 접지점을 고르면 σ = 접지점 σ + max_speed_mps × (t − observed_at)
  이고, 속도(velocity, 선택 필드)는 그 시각의 값을 모르므로 싣지 않는다(골반 필터 속도는 더 이른 시각의 값이다).
  유지 중(inferred)인 접지점은 위치 관측이 아니다: observed_at 은 마지막으로 발목으로 확인한 시각이다.
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

from simai_ai.geometry.joint_gate import COCO17_HEAD_JOINTS
from simai_ai.tracking.time_model import TimeModelConfig, classify_observation, require_ms
from simai_ai.tracking.world_track import WorldTrackFrame, WorldTrackSnapshot

WORLD_STATE_SCHEMA_VERSION = "2.0.0"
"""world_state 계약이 마지막으로 바뀐 버전 (1.1.0: input_provenance, 1.2.0: config_version, 1.3.0: 트랙의
surface_source·aliases 선택 필드, 1.4.0: 트랙의 head_z_validity 선택 필드, 2.0.0: UNCERTAIN head_z 값을 싣는 의미 변경)"""
DEFAULT_SOURCE_ID = "tier0-01"
_VALIDITY = frozenset({"VALID", "UNCERTAIN", "UNAVAILABLE"})
_SURFACE_VALIDITY = frozenset({"VALID", "UNCERTAIN", "UNKNOWN"})
_SURFACE_SOURCE = frozenset({"observed", "inferred"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class SupportContact:
    """ContactEstimator(`simai_ai.contact.ContactEstimator`) 결과 한 사람분 (명세 5.2 접지 추정). 기본값은 "모름"이다.

    지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 이면 ground_anchor_xyz 는 None 이어야 한다(계약 불변 규칙).
    surface_source 는 접지점이 있으면 반드시, 없으면 두지 않는다: observed(이번 시각 발목 근거) 또는 inferred(가려진
    동안 마지막 유효 지지 표면 유지). world_state 1.3.0 트랙의 surface_source 로 싣는다.
    observed_xy·observed_xy_sigma·observed_at 은 마지막으로 발목으로 확인한 접지점의 수평 위치·σ·시각이다(지금 접지가
    UNKNOWN 이어도 그 시각의 관측으로 남는다). position_xy 의 접지점 대체에 쓴다. 셋은 함께 있거나 함께 없다.
    """

    ground_anchor_xyz: tuple[float, float, float] | None = None
    ground_anchor_sigma: tuple[float, float, float] | None = None
    support_surface_id: str | None = None
    support_surface_validity: str = "UNKNOWN"
    contact_validity: str = "UNAVAILABLE"
    z_validity: str = "UNAVAILABLE"
    surface_source: str | None = None
    observed_xy: tuple[float, float] | None = None
    observed_xy_sigma: tuple[float, float] | None = None
    observed_at: int | None = None
    """마지막으로 발목으로 접지를 확인한 공통 시각(UTC ms). surface_source 가 observed 이면 이번 시각이다"""

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
        if (self.surface_source is None) != (self.ground_anchor_xyz is None):
            raise ValueError("접지점(ground_anchor_xyz)이 있으면 surface_source 가 반드시 있고, 없으면 두지 않는다")
        if self.surface_source is not None and self.surface_source not in _SURFACE_SOURCE:
            raise ValueError(f"surface_source 값이 아니다: {self.surface_source}")
        observed = (self.observed_xy, self.observed_xy_sigma, self.observed_at)
        if any(v is None for v in observed) and not all(v is None for v in observed):
            raise ValueError("observed_xy·observed_xy_sigma·observed_at 은 함께 있거나 함께 없다")
        if self.observed_at is not None:
            require_ms("observed_at", self.observed_at)
            _require_finite("observed_xy", self.observed_xy, 2)
            _require_finite("observed_xy_sigma", self.observed_xy_sigma, 2)
        if self.surface_source == "observed" and self.observed_at is None:
            raise ValueError("surface_source 가 observed 이면 observed_at 이 있어야 한다")


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
    t_last_valid = track.t_last_valid
    position_xy, position_sigma = track.position_xy, track.position_xy_sigma
    velocity: tuple[float, float, float] | None = track.velocity
    if contact.observed_at is not None and contact.observed_at > track.t_last_valid:
        # 명세 5.2: 골반이 없으면 검증된 접지점 XY. 위치·σ·t_last_valid·관측 상태를 접지점 관측 시각 기준으로 맞춘다
        if contact.observed_at > track.t:
            raise ValueError(f"{track.world_id}: 접지점 관측 시각 {contact.observed_at} 가 Track 시각 {track.t} 보다 뒤다")
        assert contact.observed_xy is not None and contact.observed_xy_sigma is not None
        t_last_valid = contact.observed_at
        grow = time_config.max_speed_mps * (track.t - contact.observed_at) / 1000.0
        position_xy = contact.observed_xy
        position_sigma = (contact.observed_xy_sigma[0] + grow, contact.observed_xy_sigma[1] + grow)
        velocity = None  # 그 시각의 속도를 모른다 (모르면 비운다)
    gap = t_latest_global - t_last_valid
    if gap < 0:
        raise ValueError(f"{track.world_id}: t_last_valid {t_last_valid} 가 t_latest_global {t_latest_global} 보다 뒤다")
    _require_finite("position_xy", position_xy, 2)
    _require_finite("position_xy_sigma", position_sigma, 2)
    head_z, head_sigma, head_validity = _head_z(track)
    doc: dict[str, Any] = {
        "world_id": track.world_id,
        "t_last_valid": t_last_valid,
        "observation_state": str(classify_observation(gap, time_config)),
        "track_lifecycle": str(track.lifecycle),
        "identity_confidence": str(track.identity_confidence),
        "identity_revision": track.identity_revision,
        "position_xy": [position_xy[0], position_xy[1]],
        "position_xy_sigma": [position_sigma[0], position_sigma[1]],
        "pelvis_xyz": list(track.pelvis_xyz) if track.pelvis_xyz is not None else None,
        "ground_anchor_xyz": list(contact.ground_anchor_xyz) if contact.ground_anchor_xyz is not None else None,
        "support_surface_id": contact.support_surface_id,
        "support_surface_validity": contact.support_surface_validity,
        "contact_validity": contact.contact_validity,
        "z_validity": contact.z_validity,
        "head_z": head_z,
        "head_z_sigma": head_sigma,
        "head_z_validity": head_validity,
        "cameras": list(track.cameras),
    }
    aliases = _aliases(track)
    if aliases:
        doc["aliases"] = aliases
    if contact.ground_anchor_xyz is not None:
        # 접지점이 있으면 출처를 늘 싣고, 접지점이 없으면 싣지 않는다(계약 1.3.0). SupportContact 불변식이 둘을 묶지만
        # 불변식을 우회한 값이 출처 없이 나가지 않게 여기서도 막는다(-O 로 사라지는 assert 를 쓰지 않는다)
        if contact.surface_source not in _SURFACE_SOURCE:
            raise ValueError(f"{track.world_id}: 접지점이 있는데 surface_source 가 없거나 값이 아니다: {contact.surface_source!r}")
        doc["surface_source"] = contact.surface_source
    if velocity is not None:
        doc["velocity"] = list(velocity)
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


def _head_z(track: WorldTrackSnapshot) -> tuple[float | None, float | None, str]:
    """(head_z, head_z_sigma, head_z_validity). 게이트가 VALID·UNCERTAIN 으로 낸 유한한 값을 관측 관절 근거와 함께만 싣는다.

    계약 2.0.0 불변 규칙: VALID·UNCERTAIN ↔ 수, UNAVAILABLE ↔ null. 근거를 확인할 수 없으면 UNAVAILABLE 이다.
    """
    head = track.head_z
    if head is None:
        return None, None, "UNAVAILABLE"  # 이번 시각 클러스터가 붙지 않았다(관측 없음)
    validity = str(head.validity)
    if validity not in _VALIDITY:
        raise ValueError(f"{track.world_id}: head_z 유효성 값이 아니다: {validity!r}")
    if validity == "UNAVAILABLE":
        return None, None, "UNAVAILABLE"
    value = head.value
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, None, "UNAVAILABLE"  # VALID·UNCERTAIN 이라면서 값이 없거나 유한하지 않다: 값을 만들지 못한 것이다
    if not _head_joints_observed(track, validity):
        return None, None, "UNAVAILABLE"  # 관측(observed) 귀·눈 근거를 확인할 수 없다: 지어내지 않는다
    sigma = head.sigma_m
    if sigma is not None and (isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not math.isfinite(sigma)):
        sigma = None  # σ 를 모르면 비운다(값과 유효성은 그대로)
    return float(value), None if sigma is None else float(sigma), validity


def _head_joints_observed(track: WorldTrackSnapshot, validity: str) -> bool:
    """게이트가 head_z 에 쓴 귀·눈(HeadZ.joints)이 같은 Track 의 이번 시각 skeleton 에서 모두 좌표가 있는 관측(observed)
    관절이고 유효성이 head_z 유효성과 같은지. UNCERTAIN 이면 skeleton 에 VALID 귀·눈이 없어야 한다(게이트는 VALID 귀·눈이
    있으면 VALID 로 낸다. R1·R4 는 'skeleton 에 VALID 근거가 없으면 head_z 는 UNCERTAIN' 으로 읽는다). skeleton·근거 목록이
    없으면 확인할 수 없어 False 다."""
    head, skeleton = track.head_z, track.skeleton
    if head is None or skeleton is None or not head.joints:
        return False
    joints = skeleton.joints
    for j in head.joints:
        if isinstance(j, bool) or not isinstance(j, int) or not 0 <= j < len(joints):
            return False
        joint = joints[j]
        if joint.xyz is None or joint.joint_source != "observed" or str(joint.validity) != validity:
            return False
    if validity == "UNCERTAIN" and any(
        j < len(joints) and joints[j].xyz is not None and str(joints[j].validity) == "VALID" for j in COCO17_HEAD_JOINTS
    ):
        return False  # VALID 귀·눈이 있는데 UNCERTAIN 이라는 것은 게이트와 맞지 않는다(소비자가 VALID 로 오인할 수 있음)
    return True


def _aliases(track: WorldTrackSnapshot) -> list[str]:
    """병합 별칭을 합쳐진 순서대로 중복 없이. 자기 ID 나 빈 ID 는 입력 불일치다."""
    out: list[str] = []
    for alias in track.aliases:
        if not isinstance(alias, str) or not alias:
            raise ValueError(f"{track.world_id}: 별칭이 빈 World ID 다: {alias!r}")
        if alias == track.world_id:
            raise ValueError(f"{track.world_id}: 별칭에 자기 World ID 가 있다")
        if alias not in out:
            out.append(alias)
    return out


def _require_finite(name: str, values: Any, n: int) -> None:
    if len(values) != n or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError(f"{name} 는 유한한 수 {n}개여야 한다: {values!r}")
