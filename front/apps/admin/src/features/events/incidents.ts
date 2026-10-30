import { RESOLVED_PER_INCIDENT, SEVERITY_RANK } from '@/features/events/constants';
import {
  byRecent,
  eventTime,
  isActive,
  isAlarming,
  type EventEntry,
} from '@/features/events/entries';
import type { EventItem, Severity } from '@/features/events/types';
import type { WorldTrack } from '@simai/contracts';

// 한 사람(또는 사람이 없는 구역)에 걸린 이벤트를 묶는 계산(명세 7.4). 화면 없이 시험한다.

/** 한 사람(또는 사람이 없는 구역)에 걸린 이벤트 묶음(명세 7.4 "한 사람에게 걸린 여러 규칙은 화면에서 한 묶음"). */
export interface Incident {
  key: string;
  target: { type: 'person' | 'zone'; id: string } | null;
  /** 진행 중 규칙(등급 높은 순) 전부와 최근 해소된 규칙 몇 건(최근 순). */
  entries: EventEntry[];
  /** 진행 중 규칙 중 가장 높은 등급(모두 해소면 해소된 것 중 가장 높은 등급). */
  severity: Severity;
  active: boolean;
  alarming: boolean;
  latestAt: number;
}

/** 이벤트의 대상: 사람이 있으면 사람, 없으면 구역. 둘 다 없으면 null. */
export function incidentTarget(event: EventItem): Incident['target'] {
  const person = event.world_ids[0];
  if (person) return { type: 'person', id: person };
  if (event.zone_id) return { type: 'zone', id: event.zone_id };
  return null;
}

const bySeverity = (a: EventEntry, b: EventEntry) =>
  SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
  a.event.rule_id.localeCompare(b.event.rule_id);

/** 같은 대상의 이벤트끼리 모은다. 대상이 없는 이벤트는 각자 한 묶음이다. */
function groupByTarget(entries: readonly EventEntry[]): Map<string, EventEntry[]> {
  const groups = new Map<string, EventEntry[]>();
  for (const entry of entries) {
    const target = incidentTarget(entry.event);
    const key = target ? `${target.type}:${target.id}` : `event:${entry.event.event_id}`;
    const group = groups.get(key);
    if (group) group.push(entry);
    else groups.set(key, [entry]);
  }
  return groups;
}

function buildIncident(key: string, group: EventEntry[]): Incident {
  const live = group.filter((e) => isActive(e.event)).sort(bySeverity);
  const resolved = group
    .filter((e) => !isActive(e.event))
    .sort(byRecent)
    .slice(0, RESOLVED_PER_INCIDENT);
  const top = (live[0] ?? [...resolved].sort(bySeverity)[0])!;
  const shown = [...live, ...resolved];
  return {
    key,
    target: incidentTarget(top.event),
    entries: shown,
    severity: top.event.severity,
    active: live.length > 0,
    alarming: live.some(isAlarming),
    latestAt: Math.max(...shown.map((e) => eventTime(e.event) ?? 0)),
  };
}

export function groupIncidents(entries: readonly EventEntry[]): Incident[] {
  const incidents = [...groupByTarget(entries)].map(([key, group]) => buildIncident(key, group));
  return incidents.sort(
    (a, b) =>
      Number(b.active) - Number(a.active) ||
      SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity] ||
      b.latestAt - a.latestAt,
  );
}

/** 묶음에서 가장 먼저 발생한 시각(카드 머리줄). 발생 시각을 모르면 null. */
export function firstRaisedAt(incident: Incident): number | null {
  const times = incident.entries
    .map((e) => e.event.raised_at)
    .filter((t): t is number => t !== undefined);
  return times.length > 0 ? Math.min(...times) : null;
}

/** 진행 중 규칙 중 가장 먼저 발생한 시각(경과 표시의 기준). 진행 중인 규칙이 없으면 null. */
export function firstActiveRaisedAt(incident: Incident): number | null {
  const times = incident.entries
    .filter((e) => isActive(e.event))
    .map((e) => e.event.raised_at)
    .filter((t): t is number => t !== undefined);
  return times.length > 0 ? Math.min(...times) : null;
}

/** 이 이벤트가 든 묶음. 목록에서 빠졌으면 없다. */
export function incidentOf(entries: readonly EventEntry[], eventId: string): Incident | undefined {
  return groupIncidents(entries).find((i) => i.entries.some((e) => e.event.event_id === eventId));
}

/** 카드 머리줄이 보이는 대표 규칙: 묶음에서 가장 높은 등급(진행 중 먼저). */
export const topEntry = (incident: Incident): EventEntry => incident.entries[0]!;

/** 이 사람을 지금 화면에 담고 있는 카메라 중 첫 번째. 사람이 아니거나 어느 카메라에도 없으면 없다. */
export function bestCamera(
  tracks: readonly Pick<WorldTrack, 'world_id' | 'aliases' | 'cameras'>[] | undefined,
  target: Incident['target'],
): string | undefined {
  if (!tracks || target?.type !== 'person') return undefined;
  const track = tracks.find((t) => t.world_id === target.id || t.aliases?.includes(target.id));
  return track?.cameras[0];
}
