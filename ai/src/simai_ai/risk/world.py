"""Risk Engine 입력: I1 world_state payload 의 최소 dataclass (명세 5.2, 12.3).

contracts/messages/world_state.schema.json 과 common/defs.schema.json 의 필드 이름·의미를 그대로 따른다.
contracts/ 생성 타입(S15P31A101-24)이 simai_ai.contracts 에 들어오면 이 모듈을 그 타입으로 교체한다.
그때까지 계약에 없는 필드를 더하거나 이름을 바꾸지 않는다.

- 이 타입은 Tier 0 가 만든 World 상태만 담는다(AGENTS.md 규칙 1). HMR·정적 3D·VLM 결과는 입력이 아니다.
- 시각은 UTC epoch ms 정수, 좌표는 World(m, 오른손, +Z 상방, 바닥 Z=0).
- 모르는 값은 None 이다. ground_anchor_xyz 를 Z=0 으로 채우지 않는다(규칙 2). 스키마의 조건부 규칙
  (지지 표면이 VALID 가 아니거나 z_validity 가 UNAVAILABLE 이면 ground_anchor_xyz 는 null, head_z_validity 가 VALID 면
  head_z 는 수, UNAVAILABLE 이면 head_z·head_z_sigma 는 null)을 여기서도 검사한다.
- **head_z 는 확정값만 담는다(S15P31A101-179).** world_state 2.0.0 은 head_z_validity 가 UNCERTAIN 이면 그 UNCERTAIN 값을
  head_z 에 싣는다(1.x 는 null). 이 모듈은 VALID 값(과 head_z_validity 가 없는 이전 버전 문서의 값)만 WorldTrack.head_z·
  head_z_sigma 로 넘기고, UNCERTAIN 값은 head_z_uncertain·head_z_uncertain_sigma 로 따로 넘긴다. 그래서 head_z 를 읽는 규칙
  (R1·R5 occupancy_height)은 계약이 바뀌어도 1.4.0 때와 같이 UNCERTAIN 을 'head_z 모름'(Z_UNKNOWN 경로)으로 본다(명세 5.2:
  UNCERTAIN head_z 는 확정 근거가 아니다). UNCERTAIN 값을 근거로 쓰는 규칙(R4)만 head_z_uncertain 을 읽고 확인 필요를 붙인다.
  payload 에는 schema_version 이 없어 1.x(UNCERTAIN + null)와 2.x(UNCERTAIN + 값)를 모두 받는다(버전별 짝은 스키마가 본다).
- head_z_validity(1.4.0 선택 필드)가 없는 문서(이전 버전 송신 측)는 None 이다. 이때 head_z 가 null 이면 이유를 모르는 것이고
  UNAVAILABLE 로 가정하지 않는다(계약 설명).
- 관절 배열 길이 일치처럼 스키마로 표현하지 못한 규칙도 검사한다(contracts/README.md, 명세 21.3).
- 수신 측은 모르는 필드를 무시한다(명세 12.1).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_bool,
    as_enum,
    as_int,
    as_list,
    as_mapping,
    as_number,
    as_opt_number,
    as_opt_str,
    as_opt_vec,
    as_str,
    as_timestamp_ms,
    as_vec,
    require,
)

# 길이는 파싱 때 검사한다(Vec2 = 2, Vec3 = 3).
Vec2 = tuple[float, ...]
Vec3 = tuple[float, ...]


class Validity(StrEnum):
    VALID = "VALID"
    UNCERTAIN = "UNCERTAIN"
    UNAVAILABLE = "UNAVAILABLE"


class ObservationState(StrEnum):
    """명세 6.1. OBSERVED 도 최신이라는 뜻이 아니다(staleness 로 따로 본다)."""

    OBSERVED = "OBSERVED"
    PREDICTED = "PREDICTED"
    UNKNOWN = "UNKNOWN"


class TrackLifecycle(StrEnum):
    """명세 5.2. 위험 판단용 관측 상태와 별도다."""

    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    OCCLUDED = "OCCLUDED"
    LOST = "LOST"


class IdentityConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IDENTITY_UNCERTAIN = "IDENTITY_UNCERTAIN"


class SupportSurfaceValidity(StrEnum):
    VALID = "VALID"
    UNCERTAIN = "UNCERTAIN"
    UNKNOWN = "UNKNOWN"


class JointSource(StrEnum):
    """observed = Tier 0 삼각측량, inferred = Tier 1 HMR (규칙 7)."""

    OBSERVED = "observed"
    INFERRED = "inferred"


class JointLayout(StrEnum):
    COCO17 = "COCO17"
    HALPE26 = "HALPE26"
    MHR70 = "MHR70"
    MHR_NATIVE = "MHR_NATIVE"


@dataclass(frozen=True)
class Skeleton:
    """관절 배열은 모두 같은 길이, joint_layout 순서. 2대 미만에서 보인 관절은 None."""

    joint_layout: JointLayout
    joints: tuple[Vec3 | None, ...]
    visible: tuple[bool, ...]
    validity: tuple[Validity, ...]
    joint_source: tuple[JointSource, ...]

    def __post_init__(self) -> None:
        lengths = {len(self.joints), len(self.visible), len(self.validity), len(self.joint_source)}
        if len(lengths) != 1:
            raise RiskInputError(
                "skeleton: joints·visible·validity·joint_source 길이가 같아야 한다 "
                f"({len(self.joints)}, {len(self.visible)}, {len(self.validity)}, {len(self.joint_source)})"
            )

    def observed_joint(self, index: int) -> Vec3 | None:
        """위험 판정에 쓸 수 있는 관절. Tier 0 관측(observed)만 돌려준다.

        inferred(HMR) 관절은 위험 판정에 넣지 않으므로 None 이다(규칙 1). 유효성(validity)은 규칙이 따로 본다.
        """
        if self.joint_source[index] is not JointSource.OBSERVED:
            return None
        return self.joints[index]

    @classmethod
    def from_json(cls, obj: Any, where: str = "skeleton") -> "Skeleton":
        obj = as_mapping(obj, where)
        joints = as_list(require(obj, "joints", where), f"{where}.joints")
        return cls(
            joint_layout=as_enum(JointLayout, require(obj, "joint_layout", where), f"{where}.joint_layout"),
            joints=tuple(as_opt_vec(j, 3, f"{where}.joints[{i}]") for i, j in enumerate(joints)),
            visible=tuple(
                as_bool(v, f"{where}.visible[{i}]")
                for i, v in enumerate(as_list(require(obj, "visible", where), f"{where}.visible"))
            ),
            validity=tuple(
                as_enum(Validity, v, f"{where}.validity[{i}]")
                for i, v in enumerate(as_list(require(obj, "validity", where), f"{where}.validity"))
            ),
            joint_source=tuple(
                as_enum(JointSource, v, f"{where}.joint_source[{i}]")
                for i, v in enumerate(as_list(require(obj, "joint_source", where), f"{where}.joint_source"))
            ),
        )


@dataclass(frozen=True)
class WorldTrack:
    """사람 한 명의 World 상태 (world_state.schema.json $defs.world_track)."""

    world_id: str
    t_last_valid: int
    observation_state: ObservationState
    track_lifecycle: TrackLifecycle
    identity_confidence: IdentityConfidence
    identity_revision: int
    position_xy: Vec2
    position_xy_sigma: Vec2
    pelvis_xyz: Vec3 | None
    ground_anchor_xyz: Vec3 | None
    support_surface_id: str | None
    support_surface_validity: SupportSurfaceValidity
    contact_validity: Validity
    z_validity: Validity
    head_z: float | None
    cameras: tuple[str, ...]
    frame_age_ms: float | None = None
    ground_anchor_sigma: Vec3 | None = None
    head_z_sigma: float | None = None
    velocity: Vec3 | None = None
    skeleton: Skeleton | None = None
    head_z_validity: Validity | None = None
    """관절 게이트가 낸 head_z 유효성(1.4.0). None 이면 송신 측이 싣지 않은 이전 버전 문서다"""
    head_z_uncertain: float | None = None
    """head_z_validity 가 UNCERTAIN 일 때 world_state 2.0.0 이 실은 UNCERTAIN 머리 높이. 확정 근거가 아니다(모듈 설명).
    1.x 문서이거나 UNCERTAIN 이 아니면 None 이다. 확정값 head_z 는 이때 늘 None 이다"""
    head_z_uncertain_sigma: float | None = None
    """head_z_uncertain 의 σ(m). 값이 없거나 σ 를 모르면 None"""
    aliases: tuple[str, ...] = field(default=(), compare=False)
    """병합으로 이 ID 에 합쳐진 이전 ID(world_state aliases, 선택). R4 가 합쳐져 사라진 ID 의 이력을 버리는 데만 쓴다(-657).
    판정 입력의 같음 비교에는 넣지 않는다(별칭 유무로 같은 관측이 달라지지 않는다)"""

    def __post_init__(self) -> None:
        # 스키마 allOf 의 조건부 규칙: 지지 표면을 모르면 접지점을 비운다(Z=0 대체 금지).
        if self.ground_anchor_xyz is not None:
            if self.support_surface_validity is not SupportSurfaceValidity.VALID:
                raise RiskInputError(
                    f"track {self.world_id}: support_surface_validity 가 VALID 가 아니면 "
                    "ground_anchor_xyz 는 null 이어야 한다"
                )
            if self.z_validity is Validity.UNAVAILABLE:
                raise RiskInputError(
                    f"track {self.world_id}: z_validity 가 UNAVAILABLE 이면 ground_anchor_xyz 는 null 이어야 한다"
                )
        # 조건부 규칙: VALID 면 확정값이 있고, UNCERTAIN·UNAVAILABLE 이면 확정값 칸(head_z·head_z_sigma)은 비어 있다
        # (UNCERTAIN 값은 head_z_uncertain 칸, 모듈 설명). 필드가 없는 이전 버전 문서에는 적용하지 않는다(하위 호환).
        if self.head_z_validity is Validity.VALID and self.head_z is None:
            raise RiskInputError(f"track {self.world_id}: head_z_validity 가 VALID 면 head_z 는 수여야 한다")
        if self.head_z_validity in (Validity.UNCERTAIN, Validity.UNAVAILABLE) and (
            self.head_z is not None or self.head_z_sigma is not None
        ):
            raise RiskInputError(
                f"track {self.world_id}: head_z_validity 가 {self.head_z_validity} 면 확정값 head_z·head_z_sigma 는 비어 있어야 "
                "한다(UNCERTAIN 값은 head_z_uncertain)"
            )
        if self.head_z_validity is not Validity.UNCERTAIN and (
            self.head_z_uncertain is not None or self.head_z_uncertain_sigma is not None
        ):
            raise RiskInputError(f"track {self.world_id}: head_z_uncertain 은 head_z_validity 가 UNCERTAIN 일 때만 있다")
        if self.head_z_uncertain is None and self.head_z_uncertain_sigma is not None:
            raise RiskInputError(f"track {self.world_id}: head_z_uncertain 없이 σ 만 있다")

    @classmethod
    def from_json(cls, obj: Any, where: str = "track") -> "WorldTrack":
        obj = as_mapping(obj, where)

        def req(key: str) -> Any:
            return require(obj, key, where)

        def at(key: str) -> str:
            return f"{where}.{key}"

        skeleton = obj.get("skeleton")
        # 키가 없으면 이전 버전 문서(None). 키가 있는데 null·모르는 값이면 계약 위반이다(as_enum 이 거부한다).
        head_validity = (
            as_enum(Validity, obj["head_z_validity"], at("head_z_validity")) if "head_z_validity" in obj else None
        )
        head_z = as_opt_number(req("head_z"), at("head_z"))
        head_sigma = as_opt_number(obj.get("head_z_sigma"), at("head_z_sigma"))
        uncertain_z: float | None = None
        uncertain_sigma: float | None = None
        if head_validity is Validity.UNCERTAIN:
            # 2.0.0 의 UNCERTAIN 값은 확정값 칸에 두지 않는다(R1·R5 가 확정 근거로 쓰지 않게, 모듈 설명). 1.x 는 null 이다
            uncertain_z, uncertain_sigma, head_z, head_sigma = head_z, head_sigma, None, None
        return cls(
            world_id=as_str(req("world_id"), at("world_id")),
            t_last_valid=as_timestamp_ms(req("t_last_valid"), at("t_last_valid")),
            observation_state=as_enum(ObservationState, req("observation_state"), at("observation_state")),
            track_lifecycle=as_enum(TrackLifecycle, req("track_lifecycle"), at("track_lifecycle")),
            identity_confidence=as_enum(IdentityConfidence, req("identity_confidence"), at("identity_confidence")),
            identity_revision=as_int(req("identity_revision"), at("identity_revision"), minimum=0),
            position_xy=as_vec(req("position_xy"), 2, at("position_xy")),
            position_xy_sigma=as_vec(req("position_xy_sigma"), 2, at("position_xy_sigma")),
            pelvis_xyz=as_opt_vec(req("pelvis_xyz"), 3, at("pelvis_xyz")),
            ground_anchor_xyz=as_opt_vec(req("ground_anchor_xyz"), 3, at("ground_anchor_xyz")),
            support_surface_id=as_opt_str(req("support_surface_id"), at("support_surface_id")),
            support_surface_validity=as_enum(
                SupportSurfaceValidity, req("support_surface_validity"), at("support_surface_validity")
            ),
            contact_validity=as_enum(Validity, req("contact_validity"), at("contact_validity")),
            z_validity=as_enum(Validity, req("z_validity"), at("z_validity")),
            head_z=head_z,
            cameras=tuple(
                as_str(c, f"{at('cameras')}[{i}]") for i, c in enumerate(as_list(req("cameras"), at("cameras")))
            ),
            frame_age_ms=_opt_duration(obj.get("frame_age_ms"), at("frame_age_ms")),
            ground_anchor_sigma=as_opt_vec(obj.get("ground_anchor_sigma"), 3, at("ground_anchor_sigma")),
            head_z_sigma=head_sigma,
            velocity=as_opt_vec(obj.get("velocity"), 3, at("velocity")),
            skeleton=None if skeleton is None else Skeleton.from_json(skeleton, at("skeleton")),
            head_z_validity=head_validity,
            head_z_uncertain=uncertain_z,
            head_z_uncertain_sigma=uncertain_sigma,
            aliases=tuple(
                as_str(w, f"{at('aliases')}[{i}]") for i, w in enumerate(as_list(obj.get("aliases") or [], at("aliases")))
            ),
        )


@dataclass(frozen=True)
class WorldState:
    """I1 world_state 의 payload. 봉투(msg_id 등)는 담지 않는다."""

    t_latest_global: int
    calibration_version: str
    scene_version: str
    tracks: tuple[WorldTrack, ...]

    def __post_init__(self) -> None:
        ids = [t.world_id for t in self.tracks]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise RiskInputError(f"world_state: world_id 가 중복된다 {duplicated}")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str = "world_state") -> "WorldState":
        payload = as_mapping(payload, where)
        tracks = as_list(require(payload, "tracks", where), f"{where}.tracks")
        return cls(
            t_latest_global=as_timestamp_ms(require(payload, "t_latest_global", where), f"{where}.t_latest_global"),
            calibration_version=as_str(require(payload, "calibration_version", where), f"{where}.calibration_version"),
            scene_version=as_str(require(payload, "scene_version", where), f"{where}.scene_version"),
            tracks=tuple(WorldTrack.from_json(t, f"{where}.tracks[{i}]") for i, t in enumerate(tracks)),
        )


def _opt_duration(value: Any, where: str) -> float | None:
    if value is None:
        return None
    number = as_number(value, where)
    if number < 0:
        raise RiskInputError(f"{where}: 0 이상이어야 한다")
    return number
