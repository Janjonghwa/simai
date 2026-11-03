import { ACK_CHANNEL_LABEL } from '@/features/events/constants';
import {
  acknowledgedAt,
  entryStatus,
  eventTime,
  isActive,
  isAlarmSeverity,
  isAlarming,
} from '@/features/events/entries';
import { formatTime, ruleName } from '@/features/events/format';
import type { Incident } from '@/features/events/incidents';
import type { EventItem } from '@/features/events/types';

// 묶음(Incident)을 카드·상세에 보이는 값으로 바꾸는 계산. 화면 없이 시험한다.

/** 대상 문구: 사람 ID, 구역이면 "구역 Z-05". */
export const targetLabel = (target: Incident['target']) =>
  !target ? '대상 없음' : target.type === 'person' ? target.id : `구역 ${target.id}`;

/** 구역 표시: "Z-05 새 구역". 이름을 모르면 번호만. */
export const zoneLabel = (zoneId: string, zoneName?: (zoneId: string) => string | undefined) => {
  const name = zoneName?.(zoneId);
  return name ? `${zoneId} ${name}` : zoneId;
};

/**
 * 카드 둘째 줄의 장소: 대상 · 첫 구역(번호와 이름) · 나머지 구역(번호만).
 * 대상이 구역이면(사람 없는 이벤트) 그 구역을 먼저 적고 같은 구역을 되풀이하지 않는다.
 */
export function placeLine(
  incident: Incident,
  zoneName?: (zoneId: string) => string | undefined,
): string {
  const own = incident.target?.type === 'zone' ? incident.target.id : null;
  const zones = [
    ...new Set(
      incident.entries.map((e) => e.event.zone_id).filter((z): z is string => !!z && z !== own),
    ),
  ];
  const head = own ? [zoneLabel(own, zoneName)] : incident.target ? [incident.target.id] : [];
  const rest = zones.map((z, n) => (n === 0 && !own ? zoneLabel(z, zoneName) : z));
  return [...head, ...rest].join(' · ');
}

/** 모두 해소된 묶음의 마지막 해소 시각 문구: "해소 12:03:41". */
export function resolvedLabel(incident: Incident): string {
  const times = incident.entries.map((e) => {
    const status = entryStatus(e);
    return status.kind === 'resolved' ? (status.at ?? 0) : 0;
  });
  return `해소 ${formatTime(Math.max(...times))}`;
}

/** 카드 머리줄 오른쪽의 처리 상태 칩 하나. */
export interface CardChip {
  kind: 'ticket' | 'ack' | 'unacked' | 'resolved';
  label: string;
}

/**
 * 처리 상태는 가장 최근 단계 하나만 보인다: 조치 단계 > 확인 시각 > 미확인. 모두 해소되면 해소 시각.
 * 조치 단계(요청됨·반장 확인·조치 중·완료 보고)는 조치 티켓 API(M2)가 생기면 ticketStage 로 넘긴다.
 * 안내·주의만 있는 묶음은 확인할 경보가 없어 칩이 없다.
 */
export function cardChip(
  incident: Incident,
  options: { ticketStage?: string } = {},
): CardChip | null {
  if (!incident.active) return { kind: 'resolved', label: resolvedLabel(incident) };
  const live = incident.entries.filter((e) => isActive(e.event));
  if (options.ticketStage) return { kind: 'ticket', label: options.ticketStage };
  const alarmLevel = live.filter((e) => isAlarmSeverity(e.event.severity));
  if (alarmLevel.length === 0) return null;
  if (alarmLevel.some(isAlarming)) return { kind: 'unacked', label: '미확인' };
  const at = Math.max(...alarmLevel.map((e) => acknowledgedAt(e) ?? 0));
  return { kind: 'ack', label: at > 0 ? `확인 ${formatTime(at)}` : '확인' };
}

export interface TimelineItem {
  at: number;
  who: string;
  text: string;
}

/** 규칙 하나의 타임라인 항목: 발생, 확인들, 해소(운영자 종료). */
function ruleTimeline(event: EventItem): TimelineItem[] {
  const rule = ruleName(event.rule_id);
  const items: TimelineItem[] = [];
  if (event.raised_at !== undefined) {
    items.push({ at: event.raised_at, who: '시스템', text: `${rule} 발생` });
  }
  for (const ack of event.acks ?? []) {
    items.push({
      at: ack.acked_at,
      who: ACK_CHANNEL_LABEL[ack.channel] ?? ack.channel,
      text: `${rule} 확인 · ${ack.actor_id}`,
    });
  }
  const resolvedAt = isActive(event) ? null : (event.resolved_at ?? eventTime(event));
  if (resolvedAt !== null) {
    const closed = event.resolution?.kind === 'OPERATOR_CLOSED';
    items.push({
      at: resolvedAt,
      who: closed ? '운영자' : '시스템',
      text: `${rule} ${closed ? '종료' : '해소'}`,
    });
  }
  return items;
}

/** 사건 상세 타임라인: 규칙마다 발생·확인·해소(운영자 종료)를 시각 순으로. 받은 이벤트 값만으로 만든다. */
export const incidentTimeline = (incident: Incident): TimelineItem[] =>
  incident.entries.flatMap((e) => ruleTimeline(e.event)).sort((a, b) => a.at - b.at);
