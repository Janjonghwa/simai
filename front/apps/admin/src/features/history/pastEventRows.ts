import { SEVERITY_RANK } from '@/features/events/constants';
import { chainItems, occurrencesOf, riskKeyOf } from '@/features/events/incidents';
import type { Severity } from '@/features/events/types';
import { isActiveAt, type EventRow } from '@/features/history/replayIndex';

// 지난 기록은 트랙 생존 여부를 알 수 없어 알림별로 표시한다.

/** 로그 줄 하나. */
export interface PastEventRow {
  key: string;
  /** 줄에서 가장 이른 이벤트(누르면 그 시각으로 옮긴다, 대표 규칙·구역). */
  first: EventRow;
  /** 줄에 든 이벤트(발생순). */
  members: EventRow[];
  /** 줄에서 가장 높은 등급. */
  severity: Severity;
  /** 발생 횟수(해소 뒤 재개 포함). 2 이상이면 「×n」을 보인다. */
  occurrences: number;
  /** 줄에 든 사람(World ID, 처음 나온 순). 사람이 없으면(구역 대상) 빈 배열. */
  people: string[];
}

export function pastEventRows(events: readonly EventRow[]): PastEventRow[] {
  const byRisk = new Map<string, EventRow[]>();
  for (const event of events) {
    const key = riskKeyOf(event.ruleId, event.zoneId);
    const group = byRisk.get(key);
    if (group) group.push(event);
    else byRisk.set(key, [event]);
  }
  const rows: PastEventRow[] = [];
  for (const [risk, group] of byRisk) {
    const items = group.map((event) => ({
      value: event,
      start: event.raisedAt,
      end: event.resolvedAt ?? event.raisedAt,
      person: event.worldIds[0] ?? null,
    }));
    for (const members of chainItems(items, null)) {
      const sorted = [...members].sort((a, b) => a.raisedAt - b.raisedAt);
      const first = sorted[0]!;
      rows.push({
        key: `${risk}|${first.eventId}`,
        first,
        members: sorted,
        severity: sorted.reduce(
          (top, e) => (SEVERITY_RANK[e.severity] > SEVERITY_RANK[top] ? e.severity : top),
          first.severity,
        ),
        occurrences: sorted.reduce(
          (sum, e) => sum + occurrencesOf({ resume_count: e.resumeCount }),
          0,
        ),
        people: [...new Set(sorted.flatMap((e) => e.worldIds))],
      });
    }
  }
  return rows.sort((a, b) => a.first.raisedAt - b.first.raisedAt);
}

/** 재생 시각에 줄의 이벤트 중 하나라도 활성이면 활성 줄이다. */
export const isRowActiveAt = (row: PastEventRow, t: number) =>
  row.members.some((event) => isActiveAt(event, t));
