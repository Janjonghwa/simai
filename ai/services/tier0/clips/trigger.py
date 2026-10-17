"""어떤 이벤트 전이가 클립을 만드는지 (명세 5.1, 7.3, AGENTS.md 규칙 5).

판정 로직은 없다. Risk Engine 생명주기(simai_ai.risk.lifecycle)가 확정한 전이에서 이미 정해진 값만 읽는다.

- 출처가 LIVE 인 전이만(Origin.alertable). PLANNED·WHAT_IF·REPLAY 는 영상과 상관없는 판정이고 실제 알림 대상도 아니다.
- 등급이 현장 경보 등급(alerting.LOCAL_ALERT_SEVERITIES = CRITICAL·HIGH)인 전이만. 7.3 표가 정본이고 따로 고르지 않는다.
- 위험이 시작된 순간만: 발생(RAISED), 해소 후 재개(RESUMED), 등급 변경(SEVERITY, 예: CAUTION → HIGH).
  확인(ACK)·근거 갱신·체크포인트 복원(RESTORED) 같은 전이로는 만들지 않는다. 복원 뒤에는 발생 순간의 영상이 버퍼에
  없으므로 엉뚱한 구간을 '이벤트 클립'으로 올리지 않는다.
- 같은 위험 구간에는 클립 하나: 열쇠는 (event_id, resume_count) 다. HIGH → CRITICAL 상향은 같은 열쇠라 새로 자르지 않고,
  재개는 새 위험 발생처럼 다루므로(6.4) 새 클립이다. 중복 제거는 EventClipService 가 한다.
- 기준 시각(anchor)은 전이 상태의 observation_time(판정에 쓴 World 상태의 촬영 시각 기준)이다. 링 버퍼도 촬영 시각으로
  자르므로 같은 시간축이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from simai_ai.risk.alerting import LOCAL_ALERT_SEVERITIES
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.lifecycle import Change, EventTransition, Lifecycle

# 클립 대상 등급 = 현장 경보 등급(7.3 표). 5.1 'CRITICAL·HIGH 이벤트가 나면'과 같다.
CLIP_SEVERITIES = LOCAL_ALERT_SEVERITIES
_ONSET_CHANGES = frozenset({Change.RAISED, Change.RESUMED, Change.SEVERITY})


@dataclass(frozen=True)
class ClipTrigger:
    """클립 하나를 만들 이유. 판정 값은 전이에서 그대로 옮긴다."""

    event_id: str
    gateway_id: str
    event_sequence: int
    resume_count: int
    rule_id: str
    severity: Severity
    anchor_capture_ms: int
    occurred_at: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.event_id, self.resume_count)


def clip_trigger_for(transition: EventTransition) -> ClipTrigger | None:
    """전이가 클립 대상이면 ClipTrigger, 아니면 None."""
    s = transition.state
    if not Origin(s.origin).alertable:
        return None
    if transition.transition is Lifecycle.RESOLVED:
        return None
    if Severity(s.severity) not in CLIP_SEVERITIES:
        return None
    if Change.RESTORED in transition.changes or not _ONSET_CHANGES.intersection(transition.changes):
        return None
    return ClipTrigger(
        event_id=s.event_id,
        gateway_id=s.gateway_id,
        event_sequence=s.event_sequence,
        resume_count=s.resume_count,
        rule_id=s.rule_id,
        severity=Severity(s.severity),
        anchor_capture_ms=s.observation_time,
        occurred_at=transition.occurred_at,
    )
