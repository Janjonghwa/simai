"""판정 출처, 심각도, “확인 필요” 이유 (명세 7.1, 7.3, 7.4, 11.4)."""

import logging
from collections.abc import Iterable
from enum import StrEnum


class Origin(StrEnum):
    """판정 출처. 같은 규칙을 네 가지 입력에 적용한다(명세 7장 머리말).

    실제 알림 채널로 보내는 것은 LIVE 뿐이다(AGENTS.md 규칙 5, 명세 7.4 출처 분리·11.4-3).
    PLANNED·WHAT_IF·REPLAY 결과는 보고서·시뮬레이션 화면에만 쓴다.
    """

    LIVE = "LIVE"
    PLANNED = "PLANNED"
    REPLAY = "REPLAY"
    WHAT_IF = "WHAT_IF"

    @property
    def alertable(self) -> bool:
        """실제 알림 대상인지. LIVE 만 True 다."""
        return self is Origin.LIVE


class Severity(StrEnum):
    """심각도 (명세 7.1). 신뢰도가 낮아도 등급은 내려가지 않는다(7.3)."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    CAUTION = "CAUTION"
    INFO = "INFO"


class ConfidenceReason(StrEnum):
    """“확인 필요”의 이유 (명세 7.1, contracts defs confidence_reason). 순서는 선언 순서로 정렬한다.

    “확인 필요”는 이유 목록이 비어 있지 않은 상태다. 등급과 독립이다(7.3: 신뢰도가 낮아도 등급 유지).
    """

    Z_UNKNOWN = "Z_UNKNOWN"  # 지지 표면·높이를 모름(z_validity UNAVAILABLE)
    PREDICTED = "PREDICTED"  # 위치가 관측이 아니라 예측·마지막 추정값(관측 상실 포함)
    PROVISIONAL_CALIB = "PROVISIONAL_CALIB"  # 보정 상태 PROVISIONAL
    SINGLE_VIEW = "SINGLE_VIEW"  # 2대 이상 유효 관측이 없음
    JOINT_UNCERTAIN = "JOINT_UNCERTAIN"  # 근거 관절 UNCERTAIN
    IDENTITY_UNCERTAIN = "IDENTITY_UNCERTAIN"  # 카메라 간 연결이 애매함
    UNSPECIFIED = "UNSPECIFIED"  # 확인 필요인데 생산자가 이유를 적지 않음(하위 호환, 원인 미상)


# 이유를 적지 않은 needs_check=True(이유 목록이 생기기 전 형식)에 붙이는 이유. 원인을 모르므로 특정 원인(PREDICTED 등)으로
# 단정하지 않고 '원인 미상'으로 남긴다. 원인을 아는 규칙(R6 holding 의 PREDICTED 등)은 이유를 직접 적는다.
LEGACY_NEEDS_CHECK_REASON = ConfidenceReason.UNSPECIFIED

_log = logging.getLogger(__name__)
_warned_sources: set[str] = set()

_REASON_ORDER = {r: i for i, r in enumerate(ConfidenceReason)}


def normalize_confidence(
    needs_check: bool, reasons: Iterable[ConfidenceReason | str], source: str = "unknown"
) -> tuple[bool, tuple[ConfidenceReason, ...]]:
    """(needs_check, 이유 목록)을 “확인 필요 = 이유 목록이 비어 있지 않음”(7.1)으로 맞춘다.

    - 이유가 있으면 needs_check 는 True 다(이유가 확인 필요를 정한다).
    - needs_check=True 인데 이유가 없으면(하위 호환) UNSPECIFIED 하나를 붙이고 경고 로그를 남긴다(source 별 한 번).
      확인 필요를 버리지 않고, 원인을 지어내지 않는다.
    - 이유는 중복을 없애고 선언 순서로 정렬한다(같은 입력이면 같은 출력). 모르는 값은 ValueError.
    """
    normalized = tuple(sorted({ConfidenceReason(r) for r in reasons}, key=_REASON_ORDER.__getitem__))
    if needs_check and not normalized:
        normalized = (LEGACY_NEEDS_CHECK_REASON,)
        if source not in _warned_sources:
            _warned_sources.add(source)
            _log.warning("%s: 이유 없는 needs_check=True 에 %s 를 붙였다(규칙은 confidence_reasons 를 적는다)", source,
                         LEGACY_NEEDS_CHECK_REASON)
    return bool(normalized), normalized
