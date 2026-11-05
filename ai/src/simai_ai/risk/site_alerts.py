"""활성 현장 경보 목록 (명세 3.10 현장 경보 재동기화, 12.2 I0, FR-137, 계약 health.active_site_alerts).

RunPod Tier 0 는 노트북 제어 채널이 (재)접속한 직후와 1Hz health 마다 이 목록을 보낸다. 노트북은 local_alert·alarm_stop
을 놓치거나 다시 접속해도 이 목록에 맞춰 소리·표시를 켜고 끈다. 여기서는 생명주기 추적기의 활성 이벤트 상태
(lifecycle.EventState)로 목록을 만드는 순수 함수만 둔다. 송신(주기·접속 직후·대기열)은 services/tier0 이 한다.

목록에 넣는 이벤트 (계약 health.active_site_alerts):
- 현장 경보 대상: 7.3 표(alerting.channels_for)가 LOCAL_ALERT 를 주는 이벤트. 곧 LIVE 이고 지금 등급이 CRITICAL·HIGH 다.
  PLANNED·WHAT_IF·REPLAY 는 절대 넣지 않는다(AGENTS.md 규칙 5). 채널을 여기서 따로 고르지 않는다.
- 아직 RESOLVED 가 아닌 이벤트 전부. 노트북이 끊겨 local_alert 를 받지 못한 이벤트도 넣는다(일부만 싣지 않는다).
- 같은 event_id 는 한 번만 싣는다.

acknowledged 는 EventState.site_acknowledged 다: 지금 경보 단계(마지막 발생·등급 상향·재개) 이후 상태를 대상으로 한
확인만 센다. 상향 전 상태에 대한 늦은 확인으로 상향된 경보 소리를 멈추지 않는다(alarm_stop.event_sequence 와 같은 규칙).

summary 는 규칙 근거 한 줄(규칙 이름과 측정값)이다. VLM 근거는 넣지 않는다(10.1). local_alert.summary 와 같은 값이어야
하므로 local_alert 를 만드는 쪽도 같은 summarize 함수를 쓴다.
"""

from collections.abc import Callable, Iterable
from typing import Any

from simai_ai.risk.alerting import Channel, channels_for
from simai_ai.risk.lifecycle import EventState, Lifecycle

Summarize = Callable[[EventState], str]

_MAX_METRICS_IN_SUMMARY = 3


def default_summary(state: EventState) -> str:
    """규칙 근거 한 줄: 규칙 ID, 구역, 측정값 몇 개. 규칙별 문구가 생기면 summarize 를 바꿔 끼운다."""
    parts = [state.rule_id]
    if state.zone_id:
        parts.append(state.zone_id)
    metrics = ", ".join(f"{k} {v:.2f}" for k, v in sorted(state.metrics.items())[:_MAX_METRICS_IN_SUMMARY])
    line = " ".join(parts)
    return f"{line}: {metrics}" if metrics else line


def is_site_alert(state: EventState) -> bool:
    """현장 경보 목록에 들어가는 이벤트인지: 해소 전이고 7.3 표가 LOCAL_ALERT 를 주는(LIVE·CRITICAL·HIGH) 이벤트."""
    if state.lifecycle is Lifecycle.RESOLVED:
        return False
    return Channel.LOCAL_ALERT in channels_for(state.severity, state.confidence, state.origin).channels


def site_alert_item(state: EventState, summarize: Summarize = default_summary) -> dict[str, Any]:
    """활성 현장 경보 하나 (계약 health.$defs.active_site_alert)."""
    summary = summarize(state) or state.rule_id
    return {
        "event_id": state.event_id,
        "event_sequence": state.event_sequence,
        "source": str(state.origin),
        "acknowledged": state.site_acknowledged,
        "severity": str(state.severity),
        "sound_mode": state.sound_mode,
        "summary": summary,
        "rule_id": state.rule_id,
        "zone_id": state.zone_id,
    }


def local_alert_payload(state: EventState, summarize: Summarize = default_summary) -> dict[str, Any]:
    """현장 경보 신호(LocalAlert·AlertClear)를 낳은 전이 뒤 상태 → I0 local_alert payload (계약 local_alert.schema.json).

    lifecycle 은 그 전이 뒤 상태 그대로다(발생·상향·재개는 RAISED·UPDATED, 해소 신호는 RESOLVED). acknowledged 는
    site_acknowledged(지금 경보 단계 이후 상태를 대상으로 한 확인만)이고, summary 는 active_site_alerts 와 같은 함수다.
    LIVE 가 아니거나 등급이 CRITICAL·HIGH 가 아니면 계약이 받지 않으므로 ValueError(지어내지 않는다. 호출자는 보내지
    않고 다음 active_site_alerts 목록으로 노트북을 맞춘다. 예: 현장 경보 뒤 CAUTION 으로 내려간 채 해소된 이벤트).
    """
    if not state.origin.alertable:
        raise ValueError(f"{state.event_id}: {state.origin} 이벤트는 현장 경보로 보내지 않는다(LIVE 만)")
    if str(state.severity) not in ("CRITICAL", "HIGH"):
        raise ValueError(f"{state.event_id}: 등급 {state.severity} 는 현장 경보(local_alert) 등급이 아니다")
    return {
        "event_id": state.event_id,
        "event_sequence": state.event_sequence,
        "lifecycle": str(state.lifecycle),
        "source": str(state.origin),
        "severity": str(state.severity),
        "sound_mode": state.sound_mode,
        "confidence": str(state.confidence),
        "needs_check": state.needs_check,
        "rule_id": state.rule_id,
        "zone_id": state.zone_id,
        "world_ids": list(state.world_ids),
        "summary": summarize(state) or state.rule_id,
        "observation_time": state.observation_time,
        "evaluated_at": state.evaluated_at,
        "acknowledged": state.site_acknowledged,
    }


def build_active_site_alerts(
    states: Iterable[EventState], summarize: Summarize = default_summary
) -> list[dict[str, Any]]:
    """활성 이벤트 상태들 → health.active_site_alerts. 빈 목록은 '활성 현장 경보 없음'이다.

    같은 event_id 가 여러 번 들어오면(추적기 여러 개를 합칠 때 등) event_sequence 가 가장 큰 상태 하나로 판단한다
    (그 상태가 현장 경보 대상이 아니면 싣지 않는다).
    순서는 event_id 순으로 고정한다(같은 상태면 같은 목록).
    """
    latest: dict[str, EventState] = {}
    for state in states:
        prev = latest.get(state.event_id)
        if prev is None or state.event_sequence > prev.event_sequence:
            latest[state.event_id] = state
    return [site_alert_item(latest[eid], summarize) for eid in sorted(latest) if is_site_alert(latest[eid])]
