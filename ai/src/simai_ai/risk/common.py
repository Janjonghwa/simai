"""판정 출처와 심각도 (명세 7.1, 7.3, 7.4, 11.4)."""

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
