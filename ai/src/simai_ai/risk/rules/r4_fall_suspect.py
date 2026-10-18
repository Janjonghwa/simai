"""R4 낙상 의심 (명세 7.2 R4와 R4 설명, 5.2 head_z·3D 관절 유효성 게이트, 7.1 확인 필요, 6.4, 7.3, 부록 D-G6-02).

명세: 머리 높이 급하강 후 낮은 자세 유지, 또는 급하강 직후 관측 상실이면 낙상 의심(CRITICAL)이다. 관절이 UNCERTAIN 이면
'낙상 의심 + 확인 필요'다. 앉기·물건 줍기·책상 뒤 이동과 영상상 비슷해서 관절 유효성 게이트를 통과한 관절만 확정 근거로 쓴다.
심각도는 늘 CRITICAL 이고 신뢰도가 낮아도 내리지 않는다(7.3). 채널은 alerting.channels_for 가 정한다.

**머리 높이와 그 유효성(무엇을 읽는가).** world_state 의 head_z 는 관절 게이트가 VALID 로 낸 값만 실리고, 게이트가 낸
유효성은 1.4.0 선택 필드 head_z_validity 로 실린다(VALID 일 때만 값, UNCERTAIN 값은 싣지 않음, 사용자 결정). R4 는 머리 높이와
그 근거가 VALID 인지 UNCERTAIN 인지를 head_z_validity 로 정한다.
- VALID: head_z 를 그대로 쓴다(확정 근거). skeleton 을 다시 보지 않는다.
- UNCERTAIN: 머리가 보이기는 했지만 확정할 근거가 없다. 계약에 값이 없으므로 같은 트랙 skeleton 의 관측(observed) UNCERTAIN
  귀·눈(COCO17 1~4)에서 Tier 0 와 같은 식(Z 평균 + head_top_offset_m, geometry.joint_gate.GatedSkeleton.head_z 의 UNCERTAIN
  값)으로 다시 만들어 판정하고 확인 필요(`JOINT_UNCERTAIN`)를 붙인다(명세 7.2 R4 '관절이 UNCERTAIN 이면 낙상 의심 + 확인
  필요', 5.2 'UNCERTAIN 이면 등급은 유지하고 확인 필요'). 이 경로만 남긴 이유: 2대 관측 경쟁 후보 영역처럼 머리 근거가 계속
  UNCERTAIN 인 사람은 값 없이는 급하강을 볼 수 없어 낙상을 놓친다(단상 낙상 벡터). 귀·눈 좌표가 없으면 머리 높이를 모르는
  것으로 본다(아래 '머리 높이를 모름', head_uncertain). 오프셋 기본값(0.12 m)은 Tier 0 JointGateConfig.head_top_offset_m 과 같아야 한다
  (같은지 시험한다. G2 에서 바뀌면 함께 바꾼다).
- UNAVAILABLE: 머리 높이를 모른다(좌표 있는 귀·눈 없음, 이번 시각 관측 없음). skeleton 으로 다시 만들지 않는다.
- head_z_validity 가 없는 이전 버전(1.3.0 이하) 문서: 지금까지처럼 읽는다(하위 호환). head_z 가 있으면 그 값(VALID, 다만
  skeleton 이 있는데 관측 VALID 귀·눈이 하나도 없으면 UNCERTAIN 귀·눈에서 왔다고 보고 UNCERTAIN), head_z 가 null 이면 이유를
  모르므로 skeleton 의 관측 VALID 귀·눈을 먼저, 없으면 UNCERTAIN 귀·눈을 같은 식으로 쓴다. 둘 다 없으면 모름이다.
추정(inferred, HMR) 관절은 어느 경우에도 쓰지 않는다(규칙 1). head_z_validity 와 head_z 가 어긋난 문서(VALID 인데 null 등)는
risk.world 가 계약 위반으로 거부한다.

**지면 기준 높이.** 낮은 자세는 머리 높이 − 서 있는 면 Z(ground_anchor_xyz 의 Z, 단상 위면 상판 높이)로 본다. 계단·단상에서
내려오면 머리와 함께 접지도 내려가므로 낮은 자세가 아니다. 접지를 모르면 바닥 Z=0 으로 대체하지 않는다(규칙 2).
- 지금 접지점이 없으면 이 사람의 마지막 접지 Z(support_memory_ms 안)를 쓰고 확인 필요(`Z_UNKNOWN`)를 붙인다. 단상에서 떨어진
  사람은 마지막 접지(상판)보다 머리가 더 낮아지므로 놓치지 않는다.
- 마지막 접지도 없으면 지면 기준 높이를 모른다. 급하강 후보가 있으면 명세 5.2 '낙상(R4)은 head_z 변화를 쓴다'대로 급하강 전
  머리 높이(창 안 최고값)에서 내려간 양으로 낮은 자세를 본다(fallback_low_drop_m, `Z_UNKNOWN`). 후보가 없으면 판정하지 않고
  유지만 한다(아래).

**판정 단계(사람마다).**
- 급하강: 새 관측 표본마다 drop_window_ms 안 머리 높이 최고값 − 지금 값이 drop_min_m 이상이면 낙상 후보가 된다(t_drop 갱신).
  머리 높이 변화라 접지와 무관하다. 의도적으로 천천히 눕기는 이 창 안 하강량이 작아 후보가 되지 않는다.
- 낮은 자세 유지: 후보가 된 뒤 followup_ms 안에 지면 기준 머리 높이가 low_raise_m 이하가 되고, low_clear_m 을 넘지 않은 채
  low_hold_ms 이어지며 지금도 low_raise_m 이하면 발생(holding=False). 앉기(머리 약 1.25 m)·줍기(약 1.0 m)·웅크림(약 1.1 m)은
  낮은 자세에 들지 않는다. 낮아지지 않은 채 followup_ms 가 지나면 후보를 버린다.
- 급하강 직후 관측 상실: 후보인 사람이 관측을 잃으면(트랙이 OBSERVED 가 아님·world_state 에서 사라짐·머리 높이를 모름) 마지막
  머리 관측이 급하강에서 lost_window_ms 안이었거나 이미 낮은 자세였으면 바로 발생한다. 발생 뒤(FALLEN)에 잃어도 같다. 확인
  필요 이유는 트랙을 잃었으면 `PREDICTED`(위치가 마지막 추정값), 트랙은 보이는데 머리 근거 관절만 잃었으면 `JOINT_UNCERTAIN`
  이다(머리 근거가 UNAVAILABLE 이거나 UNCERTAIN 인데 귀·눈 값이 없을 때 포함). 관측이 돌아오지 않는 동안 계속 발생 판정을
  내고(사라진 트랙은 lost_memory_ms 까지), 돌아오면 그때 높이로 다시 본다(낮은 자세·띠면 FALLEN 으로 잇고, 일어나 있으면
  후보를 버린다).
- 발생 뒤(FALLEN): 지면 기준 머리 높이가 low_raise_m 이하면 발생 판정, low_clear_m 이하(히스테리시스 띠)면 유지(holding),
  넘으면(일어남) 판정을 내지 않는다. 판정 없이 유효 관측이 3초 이어지면 lifecycle 이 해소한다.
발생 지속 조건(3프레임 중 2)·해소 유지 시간·재개는 lifecycle 이 한다.

**모르는 상태로 해소하지 않음(6.4).** lifecycle 은 판정이 없고 트랙이 OBSERVED 면 해소 시간을 센다. 그래서 트랙은 관측되지만
R4 에 필요한 값을 모르면 유지 판정(holding)을 낸다. holding 은 새 이벤트를 만들지 않고 활성 R4 이벤트의 해소 확인만 다시 세게
한다(R5 의 속도 모름 holding 과 같은 방식).
- 머리 높이를 모름(위 관측 상실 발생에 해당하지 않을 때): input_unknown + head_unavailable(UNCERTAIN 인데 귀·눈 값이 없으면
  head_uncertain), `JOINT_UNCERTAIN`. 급하강 후보인 사람이 이렇게 되면 위 '급하강 직후 관측 상실'과 같다(근거 관절 상실).
- 지면 기준 높이를 모름(접지·마지막 접지 없음, 후보 아님): input_unknown + support_unknown, `Z_UNKNOWN`.
- 낮은 자세인데 급하강 이력이 없음(프로세스 재시작·규칙 버전 변경·보정 변경 뒤 이력 없음, 의도적으로 누움): low_posture. 활성 이벤트만 유지한다.
트랙이 PREDICTED·UNKNOWN 이면 lifecycle 이 이미 UNVERIFIABLE 로 두고 해소하지 않으므로 후보가 아닌 사람에게는 내지 않는다.

**판정 사이 기억(규칙 객체 밖).** R4 는 시간 조건('급하강 후 유지')이라 사람별 머리 높이 이력이 필요한데 world_state 에는
이력이 없다. 이력은 규칙 객체가 아니라 RuleContext.state(엔진이 소유하는 RuleState 를 출처별로 나눈 칸, 키 = 규칙 ID·버전)에
둔다. 그래서 규칙 객체는 다른 규칙처럼 설정만 갖고, 구성 묶음끼리 공유해도 된다.
- 출처 분리: 엔진이 출처(LIVE·PLANNED·REPLAY·WHAT_IF)마다 다른 칸을 넘긴다. REPLAY 관측이 LIVE 이력에 섞이지 않는다.
- 구성 전환: ConfigSync 가 묶음마다 만드는 엔진에 같은 RuleState 를 넘겨, 같은 규칙 버전이면 진행 중인 후보·유지 시간을
  이어받는다. 규칙 버전이 바뀌면 새 칸이다(이전 판정 의미의 이력을 쓰지 않는다).
- 같은 입력 순서면 같은 출력이다. 같은 관측을 다시 판정해도(t_last_valid 가 그대로) 표본을 다시 넣지 않아 출력이 같다.
- 한 칸 안에서 t_latest_global 이 뒤로 가거나 calibration_version 이 바뀌면 모든 이력을, 트랙의 t_last_valid 가 뒤로 가거나
  identity_revision 이 오르면 그 사람 이력을 버린다(다른 재생·다른 좌표계·다른 사람의 이력과 섞지 않는다). 버린 뒤에도 낮은
  자세 holding 이 활성 이벤트를 지킨다.
- 같은 출처의 독립 재생·What-if 세션은 엔진(또는 RuleState)을 따로 만든다. 같은 엔진에 섞어 넣으면 시간 역행 초기화만 막아 준다.

**확인 필요 이유(7.1).** 트랙 이유(PREDICTED·SINGLE_VIEW·IDENTITY_UNCERTAIN, r1_zone_entry.track_confidence_reasons),
관측 상실 발생의 PREDICTED(트랙 상실) 또는 JOINT_UNCERTAIN(머리 근거 상실), 머리 높이 근거가 UNCERTAIN 이었던 표본이 후보의
급하강 창·그 뒤에 있었으면 JOINT_UNCERTAIN, 지금 접지점이 아닌 기준(마지막 접지·머리 변화)을 썼으면 Z_UNKNOWN.

판정 근거 코드(reasons): head_drop(후보 근거) 뒤에 low_posture_sustained(발생) / low_posture_within_clear_margin(띠 유지) /
observation_lost_after_drop(상실 발생, 뒤에 track_not_observed·track_missing·head_unavailable·head_uncertain 중 하나) /
low_posture(후보이지만 아직 유지 시간 전, holding). 기준을 대체했으면 support_last_known 또는 support_unknown_head_change 가 뒤따른다.
수치(metrics): head_drop_m(후보의 최대 하강량), head_height_m(지면 기준 머리 높이, 알 때만), head_drop_from_peak_m(머리 변화
기준을 쓸 때만). 표시용으로 mm 반올림한다.

초기값은 모두 명세에 없는 값이다(명세화 대상). SIM 측정은 tests/risk/test_r4_sim_g6.py (상한, 판정 아님).
"""

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.common import ConfidenceReason, Severity
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.rules.r1_zone_entry import track_confidence_reasons
from simai_ai.risk.world import JointLayout, ObservationState, Validity, WorldTrack

R4_RULE_ID = "R4"
R4_RULE_VERSION = 1
R4_SEVERITY = Severity.CRITICAL
"""명세 7.2 R4 심각도. 확인 필요여도 내리지 않는다(7.3)"""

R4_DROP_WINDOW_MS = 1000
"""급하강을 보는 창. 사람 낙상은 대략 0.5~1초에 끝난다"""
R4_DROP_MIN_M = 0.8
"""창 안 머리 높이 하강량 하한. 빠른 앉기(약 0.45 m)·쪼그려 줍기(약 0.6 m)보다 크고, 서 있다 넘어짐(1.3 m 이상)보다 작다"""
R4_LOW_RAISE_M = 0.6
"""지면 기준 머리 높이가 이 값 이하면 낮은 자세(발생 임계). 바닥에 앉기(약 0.9 m)·웅크림(약 1.1 m)보다 낮고 누움(0.1~0.3 m)보다 높다"""
R4_LOW_CLEAR_M = 0.8
"""이 값을 넘으면 낮은 자세가 아니다(해소 임계, 히스테리시스 띠 0.6~0.8 m)"""
R4_FOLLOWUP_MS = 2000
"""급하강 뒤 이 시간 안에 낮은 자세가 되어야 한다"""
R4_LOW_HOLD_MS = 2000
"""낮은 자세가 이만큼 이어지면 발생('낮은 자세 유지')"""
R4_LOST_WINDOW_MS = 1000
"""마지막 머리 관측이 급하강에서 이 시간 안이면 관측 상실을 '급하강 직후 관측 상실'로 본다"""
R4_LOST_MEMORY_MS = 10_000
"""world_state 에서 사라진 사람에게 상실 발생 판정을 내는 최대 시간. 지나면 이력을 버린다(활성 이벤트는 lifecycle 이 유지)"""
R4_SUPPORT_MEMORY_MS = 10_000
"""지금 접지점이 없을 때 마지막 접지 Z 를 기준으로 쓰는 최대 경과 시간"""
R4_FALLBACK_LOW_DROP_M = 1.0
"""접지를 전혀 모를 때: 급하강 전 머리 높이에서 이만큼 이상 내려가 있으면 낮은 자세(명세 5.2 head_z 변화)"""
R4_FALLBACK_CLEAR_DROP_M = 0.8
"""접지를 전혀 모를 때: 내려간 양이 이보다 작으면 낮은 자세가 아니다(띠 0.8~1.0 m)"""
R4_HEAD_TOP_OFFSET_M = 0.12
"""귀·눈 Z 평균에서 머리 꼭대기까지. Tier 0 geometry.joint_gate.JointGateConfig.head_top_offset_m 기본값과 같아야 한다"""

# 판정 근거 코드
REASON_HEAD_DROP = "head_drop"
REASON_LOW_SUSTAINED = "low_posture_sustained"
REASON_LOW_CLEAR_MARGIN = "low_posture_within_clear_margin"
REASON_LOW_POSTURE = "low_posture"
REASON_LOST_AFTER_DROP = "observation_lost_after_drop"
REASON_INPUT_UNKNOWN = "input_unknown"
REASON_HEAD_UNAVAILABLE = "head_unavailable"
REASON_HEAD_UNCERTAIN = "head_uncertain"
REASON_SUPPORT_UNKNOWN = "support_unknown"
REASON_TRACK_NOT_OBSERVED = "track_not_observed"
REASON_TRACK_MISSING = "track_missing"
REASON_SUPPORT_LAST_KNOWN = "support_last_known"
REASON_SUPPORT_HEAD_CHANGE = "support_unknown_head_change"

# COCO17·HALPE26 앞 17개 순서의 눈·귀(명세 5.2 head_z 근거). risk 는 geometry 를 import 하지 않으므로 여기 둔다.
_HEAD_JOINTS = (1, 2, 3, 4)
_HEAD_LAYOUTS = frozenset({JointLayout.COCO17, JointLayout.HALPE26})

_R4_READS = (
    "tracks.observation_state",
    "tracks.t_last_valid",
    "tracks.identity_revision",
    "tracks.identity_confidence",
    "tracks.head_z",
    "tracks.head_z_validity",
    "tracks.skeleton",
    "tracks.ground_anchor_xyz",
    "tracks.cameras",
)


class _Phase(StrEnum):
    NONE = "NONE"
    CANDIDATE = "CANDIDATE"  # 급하강을 봤다. 낮은 자세 유지 또는 관측 상실을 기다린다
    FALLEN = "FALLEN"  # 낮은 자세 유지로 발생했다
    LOST = "LOST"  # 급하강 직후(또는 발생 뒤) 관측을 잃었다


class _Posture(StrEnum):
    LOW = "LOW"  # 발생 임계 이하
    BAND = "BAND"  # 히스테리시스 띠
    HIGH = "HIGH"  # 해소 임계 초과
    UNKNOWN = "UNKNOWN"  # 지면 기준 높이를 모르고 후보도 아니다


@dataclass(frozen=True)
class _Head:
    value: float
    uncertain: bool


@dataclass(frozen=True)
class _Sample:
    t: int
    head: float
    uncertain: bool


@dataclass(frozen=True)
class _Reading:
    """지금 표본의 자세 판단."""

    posture: _Posture
    height_m: float | None = None  # 지면 기준 머리 높이
    drop_from_peak_m: float | None = None  # 머리 변화 기준일 때 급하강 전 최고값에서 내려간 양
    support_code: str | None = None  # 기준을 대체했으면 그 근거 코드


@dataclass
class _Memory:
    """사람 한 명의 R4 이력."""

    revision: int
    last_seen_global: int
    samples: deque[_Sample] = field(default_factory=deque)
    last_sample_t: int | None = None
    last_support: tuple[int, float] | None = None  # (관측 시각, 접지 Z)
    phase: _Phase = _Phase.NONE
    t_drop: int = 0
    drop_m: float = 0.0
    peak_head: float = 0.0
    t_low: int | None = None
    uncertain: bool = False  # 후보 근거에 UNCERTAIN 머리 표본이 있었다
    z_unknown: bool = False  # 후보 근거에 대체 기준(마지막 접지·머리 변화)을 썼다
    lost_code: str | None = None

    def clear_candidate(self) -> None:
        self.phase = _Phase.NONE
        self.t_low = None
        self.uncertain = False
        self.z_unknown = False
        self.lost_code = None


@dataclass
class _Session:
    """한 판정 세션(엔진의 RuleState 칸 하나, 출처별)의 R4 이력."""

    memory: dict[str, _Memory] = field(default_factory=dict)
    last_global: int | None = None
    calibration_version: str | None = None


class FallSuspectRule:
    """R4 낙상 의심. 규칙 객체는 설정만 갖고, 사람별 머리 높이 이력은 RuleContext.state 의 자기 칸에 둔다
    (모듈 설명 '판정 사이 기억').

    - drop_window_ms·drop_min_m: 급하강 창과 하강량 하한.
    - low_raise_m·low_clear_m: 지면 기준 머리 높이의 낮은 자세 발생·해소 임계(히스테리시스).
    - followup_ms: 급하강 뒤 낮은 자세가 되어야 하는 시간. low_hold_ms: 낮은 자세 유지 시간.
    - lost_window_ms: 급하강 직후 관측 상실로 볼 시간. lost_memory_ms: 사라진 트랙에 상실 판정을 내는 최대 시간.
    - support_memory_ms: 마지막 접지 Z 를 기준으로 쓰는 최대 경과 시간.
    - fallback_low_drop_m·fallback_clear_drop_m: 접지를 전혀 모를 때 머리 변화 기준의 발생·해소 임계.
    - head_top_offset_m: UNCERTAIN(또는 이전 버전 문서의 head_z null) 때 skeleton 귀·눈에서 머리 높이를 만들며 더하는
      오프셋(Tier 0 과 같은 값).
    """

    def __init__(
        self,
        *,
        drop_window_ms: int = R4_DROP_WINDOW_MS,
        drop_min_m: float = R4_DROP_MIN_M,
        low_raise_m: float = R4_LOW_RAISE_M,
        low_clear_m: float = R4_LOW_CLEAR_M,
        followup_ms: int = R4_FOLLOWUP_MS,
        low_hold_ms: int = R4_LOW_HOLD_MS,
        lost_window_ms: int = R4_LOST_WINDOW_MS,
        lost_memory_ms: int = R4_LOST_MEMORY_MS,
        support_memory_ms: int = R4_SUPPORT_MEMORY_MS,
        fallback_low_drop_m: float = R4_FALLBACK_LOW_DROP_M,
        fallback_clear_drop_m: float = R4_FALLBACK_CLEAR_DROP_M,
        head_top_offset_m: float = R4_HEAD_TOP_OFFSET_M,
    ) -> None:
        for name, value, minimum in (
            ("drop_window_ms", drop_window_ms, 1),
            ("followup_ms", followup_ms, 0),
            ("low_hold_ms", low_hold_ms, 0),
            ("lost_window_ms", lost_window_ms, 0),
            ("lost_memory_ms", lost_memory_ms, 0),
            ("support_memory_ms", support_memory_ms, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise RiskInputError(f"R4.{name} 는 {minimum} 이상 정수여야 한다")
        for name, value in (
            ("drop_min_m", drop_min_m),
            ("low_raise_m", low_raise_m),
            ("low_clear_m", low_clear_m),
            ("fallback_low_drop_m", fallback_low_drop_m),
            ("fallback_clear_drop_m", fallback_clear_drop_m),
            ("head_top_offset_m", head_top_offset_m),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise RiskInputError(f"R4.{name} 는 유한한 수여야 한다")
        if drop_min_m <= 0:
            raise RiskInputError("R4.drop_min_m 은 0 보다 커야 한다")
        if low_raise_m > low_clear_m:
            raise RiskInputError("R4: low_raise_m ≤ low_clear_m 이어야 한다(히스테리시스)")
        if not 0 < fallback_clear_drop_m <= fallback_low_drop_m:
            raise RiskInputError("R4: 0 < fallback_clear_drop_m ≤ fallback_low_drop_m 이어야 한다")
        self._window = drop_window_ms
        self._drop_min = float(drop_min_m)
        self._low_raise = float(low_raise_m)
        self._low_clear = float(low_clear_m)
        self._followup = followup_ms
        self._hold = low_hold_ms
        self._lost_window = lost_window_ms
        self._lost_memory = lost_memory_ms
        self._support_memory = support_memory_ms
        self._fb_low = float(fallback_low_drop_m)
        self._fb_clear = float(fallback_clear_drop_m)
        self._offset = float(head_top_offset_m)
        self._spec = RuleSpec(rule_id=R4_RULE_ID, rule_version=R4_RULE_VERSION, reads=_R4_READS)

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        s = ctx.state.slot(self._spec.key, _Session)
        now = ctx.world.t_latest_global
        if s.last_global is not None and now < s.last_global:
            s.memory.clear()  # 관측 시각이 뒤로 갔다(같은 출처의 다른 재생). 이력을 섞지 않는다
        if s.calibration_version is not None and ctx.world.calibration_version != s.calibration_version:
            s.memory.clear()  # 보정이 바뀌면 World 좌표가 달라져 이전 머리 높이와 비교할 수 없다(가짜 급하강 방지)
        s.last_global = now
        s.calibration_version = ctx.world.calibration_version
        present = {t.world_id: t for t in ctx.world.tracks}
        findings: list[Finding] = []
        for world_id in sorted(set(present) | set(s.memory)):
            track = present.get(world_id)
            if track is None:
                finding = self._missing(s, world_id, now)
            else:
                finding = self._present(s, track, now)
            if finding is not None:
                findings.append(finding)
        return iter(findings)

    # --- 사람 하나 -------------------------------------------------------------------

    def _present(self, s: _Session, track: WorldTrack, now: int) -> Finding | None:
        memory = s.memory.get(track.world_id)
        if memory is not None and (
            track.identity_revision > memory.revision
            or (memory.last_sample_t is not None and track.t_last_valid < memory.last_sample_t)
        ):
            memory = None  # ID 교정·시간 역행: 다른 사람(또는 다른 재생)의 이력일 수 있다
        if memory is None:
            memory = s.memory[track.world_id] = _Memory(revision=track.identity_revision, last_seen_global=now)
        memory.revision = track.identity_revision
        memory.last_seen_global = now

        observed = track.observation_state is ObservationState.OBSERVED
        if observed and track.ground_anchor_xyz is not None:
            if memory.last_support is None or track.t_last_valid >= memory.last_support[0]:
                memory.last_support = (track.t_last_valid, track.ground_anchor_xyz[2])
        if not observed:
            return self._lost(track.world_id, memory, REASON_TRACK_NOT_OBSERVED, track, observed, now)
        head = self._head(track)
        if isinstance(head, str):
            return self._lost(track.world_id, memory, head, track, observed, now)

        reading = self._update(memory, track, head)
        return self._judge(track.world_id, memory, track, head, reading)

    def _missing(self, s: _Session, world_id: str, now: int) -> Finding | None:
        memory = s.memory[world_id]
        if now - memory.last_seen_global > self._lost_memory:
            del s.memory[world_id]
            return None
        return self._lost(world_id, memory, REASON_TRACK_MISSING, None, False, now)

    # --- 머리 높이 -------------------------------------------------------------------

    def _head(self, track: WorldTrack) -> _Head | str:
        """머리 높이와 근거 유효성(모듈 설명 '머리 높이와 그 유효성'). 모르면 그 판정 근거 코드(head_unavailable·head_uncertain)."""
        validity = track.head_z_validity
        if validity is None:
            return self._legacy_head(track)
        if validity is Validity.VALID:
            # risk.world 가 VALID 인데 값 없는 문서를 거부한다. 우회해 들어와도 값을 지어내지 않고 모름으로 둔다.
            return REASON_HEAD_UNAVAILABLE if track.head_z is None else _Head(track.head_z, uncertain=False)
        if validity is Validity.UNCERTAIN:
            uncertain_z = self._head_joint_z(track)[Validity.UNCERTAIN]
            if uncertain_z:
                return _Head(sum(uncertain_z) / len(uncertain_z) + self._offset, uncertain=True)
            return REASON_HEAD_UNCERTAIN
        return REASON_HEAD_UNAVAILABLE

    def _legacy_head(self, track: WorldTrack) -> _Head | str:
        """head_z_validity 가 없는 이전 버전(1.3.0 이하) 문서. head_z null 의 이유를 몰라 skeleton 귀·눈으로 다시 만든다."""
        joint_z = self._head_joint_z(track)
        valid_z, uncertain_z = joint_z[Validity.VALID], joint_z[Validity.UNCERTAIN]
        if track.head_z is not None:
            # skeleton 이 있는데 VALID 귀·눈이 없으면 head_z 는 UNCERTAIN 귀·눈에서 왔다(R1 occupancy_height 와 같은 방어).
            return _Head(track.head_z, uncertain=track.skeleton is not None and not valid_z)
        if valid_z:
            return _Head(sum(valid_z) / len(valid_z) + self._offset, uncertain=False)
        if uncertain_z:
            return _Head(sum(uncertain_z) / len(uncertain_z) + self._offset, uncertain=True)
        return REASON_HEAD_UNAVAILABLE

    @staticmethod
    def _head_joint_z(track: WorldTrack) -> dict[Validity, list[float]]:
        """skeleton 의 관측(observed) 귀·눈 Z 를 유효성별로. 추정(inferred) 관절·좌표 없는 관절은 뺀다."""
        out: dict[Validity, list[float]] = {Validity.VALID: [], Validity.UNCERTAIN: []}
        skeleton = track.skeleton
        if skeleton is None or skeleton.joint_layout not in _HEAD_LAYOUTS or len(skeleton.joints) <= max(_HEAD_JOINTS):
            return out
        for j in _HEAD_JOINTS:
            joint = skeleton.observed_joint(j)
            if joint is not None and skeleton.validity[j] in out:
                out[skeleton.validity[j]].append(joint[2])
        return out

    # --- 상태 갱신 -------------------------------------------------------------------

    def _update(self, memory: _Memory, track: WorldTrack, head: _Head) -> _Reading:
        """새 관측 표본이면 이력과 단계를 갱신한다. 같은 관측을 다시 판정하면 갱신하지 않는다(멱등)."""
        t = track.t_last_valid
        fresh = memory.last_sample_t is None or t > memory.last_sample_t
        relocated = False
        if fresh:
            memory.last_sample_t = t
            memory.samples.append(_Sample(t, head.value, head.uncertain))
            while memory.samples and memory.samples[0].t < t - self._window:
                memory.samples.popleft()
            relocated = memory.phase is _Phase.LOST
            self._detect_drop(memory, t)
        reading = self._reading(memory, track, head)
        if not fresh:
            return reading
        if memory.phase is not _Phase.NONE:
            memory.uncertain |= head.uncertain
            memory.z_unknown |= reading.support_code is not None
        if relocated:
            # 상실 뒤 다시 보였다: 그때 높이로 본다. 낮은 자세(띠 포함)면 발생 상태를 잇고, 일어나 있으면 후보를 버린다
            # (판정 없이 유효 관측이 이어지면 lifecycle 이 해소한다).
            memory.lost_code = None
            if reading.posture is _Posture.HIGH:
                memory.clear_candidate()
            else:
                memory.phase = _Phase.FALLEN
                memory.t_low = t if reading.posture is _Posture.LOW else memory.t_low
            return reading
        if memory.phase is _Phase.CANDIDATE:
            if memory.t_low is None and t - memory.t_drop > self._followup:
                # 후속 창(마지막 급하강 표본에서 followup_ms)이 지났는데 아직 낮은 자세가 시작되지 않았다. 이 표본의 낮은
                # 자세는 창 밖이라 받지 않는다(창 안에서 시작한 낮은 자세의 유지 시간과 구분, 경계 = followup_ms 포함).
                memory.clear_candidate()
            elif reading.posture is _Posture.LOW:
                if memory.t_low is None:
                    memory.t_low = t
                if t - memory.t_low >= self._hold:
                    memory.phase = _Phase.FALLEN
            elif reading.posture is _Posture.HIGH:
                memory.t_low = None
        elif memory.phase is _Phase.FALLEN:
            if reading.posture is _Posture.HIGH:
                memory.clear_candidate()  # 일어났다. 판정 없이 유효 관측이 이어지면 lifecycle 이 해소한다
            elif reading.posture is _Posture.LOW:
                memory.t_low = memory.t_low if memory.t_low is not None else t
        return reading

    def _detect_drop(self, memory: _Memory, t: int) -> None:
        current = memory.samples[-1]
        peak = max(s.head for s in memory.samples)
        drop = peak - current.head
        if drop < self._drop_min:
            return
        if memory.phase is _Phase.NONE:
            memory.phase = _Phase.CANDIDATE
            memory.t_low = None
            memory.drop_m = drop
            memory.peak_head = peak
            memory.uncertain = any(s.uncertain for s in memory.samples)
            memory.z_unknown = False
        else:
            memory.drop_m = max(memory.drop_m, drop)
            memory.peak_head = max(memory.peak_head, peak)
            memory.uncertain |= any(s.uncertain for s in memory.samples)
        memory.t_drop = t

    def _reading(self, memory: _Memory, track: WorldTrack, head: _Head) -> _Reading:
        """지면 기준 머리 높이로 자세를 정한다. 접지를 모르면 Z=0 으로 채우지 않는다."""
        support: float | None = None
        code: str | None = None
        if track.ground_anchor_xyz is not None:
            support = track.ground_anchor_xyz[2]
        elif memory.last_support is not None and track.t_last_valid - memory.last_support[0] <= self._support_memory:
            support, code = memory.last_support[1], REASON_SUPPORT_LAST_KNOWN
        if support is not None:
            height = head.value - support
            if height <= self._low_raise:
                posture = _Posture.LOW
            elif height <= self._low_clear:
                posture = _Posture.BAND
            else:
                posture = _Posture.HIGH
            return _Reading(posture, height_m=height, support_code=code)
        if memory.phase is _Phase.NONE:
            return _Reading(_Posture.UNKNOWN)
        # 접지를 전혀 모른다: 급하강 전 머리 높이에서 내려간 양으로 본다(명세 5.2 head_z 변화).
        drop = memory.peak_head - head.value
        if drop >= self._fb_low:
            posture = _Posture.LOW
        elif drop >= self._fb_clear:
            posture = _Posture.BAND
        else:
            posture = _Posture.HIGH
        return _Reading(posture, drop_from_peak_m=drop, support_code=REASON_SUPPORT_HEAD_CHANGE)

    # --- 판정 -----------------------------------------------------------------------

    def _lost(
        self, world_id: str, memory: _Memory, code: str, track: WorldTrack | None, observed: bool, now: int
    ) -> Finding | None:
        """관측(트랙·머리 높이)을 잃었다. 급하강 직후이거나 이미 낮은 자세였으면 발생, 아니면 필요할 때 유지만 한다."""
        if memory.phase is _Phase.CANDIDATE:
            recent = memory.last_sample_t is not None and memory.last_sample_t - memory.t_drop <= self._lost_window
            if recent or memory.t_low is not None:
                memory.phase = _Phase.LOST
            elif now - memory.t_drop > self._followup:
                memory.clear_candidate()
        elif memory.phase is _Phase.FALLEN:
            memory.phase = _Phase.LOST
        if memory.phase is _Phase.LOST:
            memory.lost_code = code
            # 트랙(위치)을 잃었으면 위치가 마지막 추정값이다(PREDICTED). 트랙은 보이는데 머리 근거 관절만 잃었으면
            # 근거 관절을 확인할 수 없다(JOINT_UNCERTAIN).
            head_lost = code in (REASON_HEAD_UNAVAILABLE, REASON_HEAD_UNCERTAIN)
            confidence = {ConfidenceReason.JOINT_UNCERTAIN if head_lost else ConfidenceReason.PREDICTED}
            if track is not None:
                confidence |= track_confidence_reasons(track)
            return self._finding(
                world_id,
                memory,
                confidence,
                (REASON_HEAD_DROP, REASON_LOST_AFTER_DROP, memory.lost_code),
                {"head_drop_m": memory.drop_m},
                holding=False,
            )
        if track is None or not observed:
            return None  # PREDICTED·UNKNOWN·사라짐: lifecycle 이 UNVERIFIABLE 로 두고 해소하지 않는다
        # 트랙은 관측되는데 머리 높이를 모른다: 판정하지 않되 해소 근거로도 쓰지 않는다.
        return self._finding(
            world_id,
            memory,
            track_confidence_reasons(track) | {ConfidenceReason.JOINT_UNCERTAIN},
            (REASON_INPUT_UNKNOWN, code),
            {},
            holding=True,
            candidate_flags=False,
        )

    def _judge(
        self, world_id: str, memory: _Memory, track: WorldTrack, head: _Head, reading: _Reading
    ) -> Finding | None:
        confidence = track_confidence_reasons(track)
        if head.uncertain:
            confidence.add(ConfidenceReason.JOINT_UNCERTAIN)
        if reading.support_code is not None:
            confidence.add(ConfidenceReason.Z_UNKNOWN)
        metrics: dict[str, float] = {}
        if reading.height_m is not None:
            metrics["head_height_m"] = reading.height_m
        if reading.drop_from_peak_m is not None:
            metrics["head_drop_from_peak_m"] = reading.drop_from_peak_m
        support = () if reading.support_code is None else (reading.support_code,)

        if reading.posture is _Posture.UNKNOWN:
            # 지면 기준 높이를 모르고 후보도 아니다: 유지만 한다.
            return self._finding(
                world_id,
                memory,
                confidence | {ConfidenceReason.Z_UNKNOWN},
                (REASON_INPUT_UNKNOWN, REASON_SUPPORT_UNKNOWN),
                {},
                holding=True,
                candidate_flags=False,
            )
        if memory.phase is _Phase.FALLEN:
            metrics["head_drop_m"] = memory.drop_m
            if reading.posture is _Posture.LOW:
                return self._finding(
                    world_id, memory, confidence, (REASON_HEAD_DROP, REASON_LOW_SUSTAINED, *support), metrics,
                    holding=False,
                )  # fmt: skip
            return self._finding(
                world_id, memory, confidence, (REASON_HEAD_DROP, REASON_LOW_CLEAR_MARGIN, *support), metrics,
                holding=True,
            )  # fmt: skip
        if reading.posture is _Posture.HIGH:
            return None
        # 낮은 자세(또는 띠)인데 아직 발생 조건이 아니다(후보가 유지 시간 전이거나 급하강 이력이 없음): 유지만 한다.
        if memory.phase is _Phase.CANDIDATE:
            metrics["head_drop_m"] = memory.drop_m
            return self._finding(
                world_id, memory, confidence, (REASON_HEAD_DROP, REASON_LOW_POSTURE, *support), metrics, holding=True
            )
        return self._finding(
            world_id, memory, confidence, (REASON_LOW_POSTURE, *support), metrics, holding=True, candidate_flags=False
        )

    def _finding(
        self,
        world_id: str,
        memory: _Memory,
        confidence: set[ConfidenceReason],
        reasons: tuple[str | None, ...],
        metrics: dict[str, float],
        *,
        holding: bool,
        candidate_flags: bool = True,
    ) -> Finding:
        if candidate_flags:
            if memory.uncertain:
                confidence.add(ConfidenceReason.JOINT_UNCERTAIN)
            if memory.z_unknown:
                confidence.add(ConfidenceReason.Z_UNKNOWN)
        return Finding(
            rule_id=self._spec.rule_id,
            rule_version=self._spec.rule_version,
            severity=R4_SEVERITY,
            world_ids=(world_id,),
            confidence_reasons=tuple(confidence),  # Finding 이 선언 순서로 정렬하고 needs_check 를 맞춘다
            reasons=tuple(r for r in reasons if r is not None),
            metrics={k: round(v, 3) for k, v in metrics.items()},  # 표시용 mm 반올림. 비교는 반올림 전 값으로 했다
            holding=holding,
        )
