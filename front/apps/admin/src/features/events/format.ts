import {
  CONFIDENCE_REASON,
  METRIC_LABEL,
  RULE_NAME,
  RULE_NAME_BY_SEVERITY,
} from '@/features/events/constants';
import { isActive, type EntryStatus } from '@/features/events/entries';
import { R9_RELEASE_LABEL, type R9Release } from '@/features/events/ppePolicy';
import type { EventItem } from '@/features/events/types';
import { zoneNameOrDefault, type ZoneNameOf } from '@/features/zones/zoneName';

// 이벤트 값을 화면 문구로 바꾸는 계산. 화면 없이 시험한다.

const TIME = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

/** 이벤트 시각(EC2 ms)을 시:분:초로. */
export const formatTime = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? '—' : TIME.format(ms);

export const ruleName = (ruleId: string) => RULE_NAME[ruleId] ?? ruleId;

/** 이벤트의 규칙 이름: 등급에 따라 이름이 달라지는 규칙(R1 주의)이면 그 이름, 아니면 `ruleName`. */
export const eventRuleName = (event: Pick<EventItem, 'rule_id' | 'severity'>) =>
  RULE_NAME_BY_SEVERITY[event.rule_id]?.[event.severity] ?? ruleName(event.rule_id);

/** 이벤트 대상 문구: 사람 ID, 사람이 없으면 구역 이름(모르면 `구역`). */
export const eventSubject = (event: EventItem, zoneName: ZoneNameOf) =>
  event.world_ids[0] ?? (event.zone_id ? zoneNameOrDefault(event.zone_id, zoneName) : '');

/** 규칙 하나의 처리 상태 문구. release 가 있으면(해소된 R9) "해소" 대신 해소 원인을 보인다. */
export function statusText(status: EntryStatus, release: R9Release | null = null) {
  switch (status.kind) {
    case 'alarm':
      return '확인 대기';
    case 'acked':
      return `확인 ${formatTime(status.at)}`;
    case 'notice':
      return '안내';
    case 'resolved': {
      const word = status.closed ? '운영자 종료' : release ? R9_RELEASE_LABEL[release] : '해소';
      return `${word} ${formatTime(status.at)}`;
    }
  }
}

/** 지표 문구. _m 은 미터, _s 는 초로 보인다. */
export function metricLabels(metrics: EventItem['metrics']): string[] {
  return Object.entries(metrics).map(([key, value]) => {
    const unit = key.endsWith('_m') ? 'm' : key.endsWith('_s') ? '초' : '';
    const label = METRIC_LABEL[key] ?? key.replace(/_(m|s)$/, '');
    return `${label} ${value.toFixed(unit === 'm' ? 2 : 1)}${unit}`;
  });
}

/** 확인 요청에 붙일 첫 이유 문구. 이유가 없으면 null. */
function firstReasonText(event: EventItem): string | null {
  const first = event.confidence_reasons?.[0];
  return first ? (CONFIDENCE_REASON[first] ?? first) : null;
}

/** 확인 요청 문구: "확인 필요" 에 첫 이유를 붙인다. */
export const confirmationText = (event: EventItem) => {
  const reason = firstReasonText(event);
  return reason ? `확인 필요 · ${reason}` : '확인 필요';
};

/** 발생 뒤 경과(ms)를 "12초" · "3분 20초" 로. 시계가 어긋나 음수면 0초. */
export function elapsedLabel(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}초`;
  const rest = s % 60;
  return `${Math.floor(s / 60)}분${rest ? ` ${rest}초` : ''}`;
}

/** EC2 시계 기준 지금(ms). 이 브라우저 시계에 EC2 와의 차이(health 의 clockOffsetMs)를 더한다. */
export const ec2Now = (now: number, clockOffsetMs: number | undefined) =>
  now + (clockOffsetMs ?? 0);

/** 진입 예측(R5)의 근거 한 줄. 개구부 수치·구역 진입 거리는 사건 상세에만 있다. */
export function entryEtaLine(event: EventItem): string | null {
  if (event.rule_id !== 'R5' || !isActive(event)) return null;
  const eta = event.metrics['time_to_entry_s'];
  return eta !== undefined && Number.isFinite(eta) ? `${eta.toFixed(1)}초 뒤 진입 예상` : null;
}
