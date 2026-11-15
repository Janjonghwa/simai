import {
  RECENT_RESOLVED_INCIDENTS,
  RESOLVED_PER_INCIDENT,
  SEVERITY_RANK,
} from '@/features/events/constants';
import {
  byRecent,
  eventTime,
  isActive,
  isAlarming,
  isStale,
  serverCloseKind,
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
  /**
   * 진행 중 규칙이 모두 「지난 관제」(해소 전이지만 지금 Tier 0 가 들고 있지 않음, S15P31A101-595)다.
   * 패널은 이런 묶음을 「지난 관제 · 확인 필요」 칸에 따로 모으고 경보처럼 보이지 않게 한다.
   */
  stale: boolean;
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

/** 묶음 키: 같은 대상의 이벤트가 한 묶음이고, 대상이 없는 이벤트는 각자 한 묶음이다. */
const targetKey = (event: EventItem): string => {
  const target = incidentTarget(event);
  return target ? `${target.type}:${target.id}` : `event:${event.event_id}`;
};

/** 같은 키의 이벤트끼리 모은다. */
function groupBy(
  entries: readonly EventEntry[],
  keyOf: (event: EventItem) => string,
): Map<string, EventEntry[]> {
  const groups = new Map<string, EventEntry[]>();
  for (const entry of entries) {
    const key = keyOf(entry.event);
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
    stale: live.length > 0 && live.every((e) => isStale(e.event)),
    alarming: live.some(isAlarming),
    latestAt: Math.max(...shown.map((e) => eventTime(e.event) ?? 0)),
  };
}

export function groupIncidents(entries: readonly EventEntry[]): Incident[] {
  const incidents = [...groupBy(entries, targetKey)].map(([key, group]) =>
    buildIncident(key, group),
  );
  return incidents.sort(
    (a, b) =>
      Number(b.active) - Number(a.active) ||
      SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity] ||
      b.latestAt - a.latestAt,
  );
}

/** 해소된 이벤트를 묶는 키: 같은 대상·규칙·구역에서 되풀이된 발생이 한 카드가 된다. */
const resolvedKey = (event: EventItem): string =>
  `${targetKey(event)}|${event.rule_id}|${event.zone_id ?? ''}`;

/** 이 이벤트가 발생한 횟수: 처음 한 번에, 해소 뒤 곧바로 다시 열린(resume) 횟수를 더한다. */
const occurrencesOf = (event: EventItem): number => 1 + (event.resume_count ?? 0);

/** 최근 해소 칸의 카드 하나(명세 7.4 의 사람별 묶음과 달리, 해소된 발생만 규칙·구역별로 묶는다). */
export interface ResolvedGroup {
  /** 목록 key. 같은 사람의 다른 규칙·구역과 갈린다(incident.key 는 대상만 가리킨다). */
  id: string;
  /** 해소된 이벤트만 담은 묶음. 선택·상세는 진행 중 묶음과 같이 대상으로 다룬다. */
  incident: Incident;
  /** 같은 대상·규칙·구역에서 발생한 횟수. 새 event_id 로 다시 생긴 것도 센다. */
  occurrences: number;
}

/**
 * 해소된 이벤트를 (대상, 규칙, 구역)별로 묶는다. 최근에 해소된 묶음이 앞선다. EC2 마감(S15P31A101-722: Tier 0 가 사라져 닫은 것)은
 * 관측 해소가 아니라 넣지 않는다(지난 작업·이벤트 목록에서는 그대로 보인다).
 */
export function resolvedGroups(entries: readonly EventEntry[]): ResolvedGroup[] {
  const groups = groupBy(
    entries.filter((e) => !isActive(e.event) && serverCloseKind(e.event) === null),
    resolvedKey,
  );
  return [...groups]
    .map(([id, group]) => ({
      id,
      incident: buildIncident(targetKey(group[0]!.event), group),
      occurrences: group.reduce((sum, e) => sum + occurrencesOf(e.event), 0),
    }))
    .sort((a, b) => b.incident.latestAt - a.incident.latestAt);
}

/**
 * 이벤트 목록을 「지금」 쪽(진행 중·해소)과 「지난 관제」 쪽으로 나눈다. 두 쪽을 따로 묶어야 지난 관제의 World ID 가
 * 지금 관제의 같은 ID 와 한 카드로 섞이지 않는다(Tier 0 가 바뀌면 ID 가 다시 시작한다).
 */
function splitStale(entries: readonly EventEntry[]) {
  const now: EventEntry[] = [];
  const stale: EventEntry[] = [];
  for (const entry of entries) (isStale(entry.event) ? stale : now).push(entry);
  return { now, stale };
}

/**
 * 이벤트 패널의 세 칸: 진행 중인 사건 전부, 최근에 해소된 묶음 몇 건, 「지난 관제 · 확인 필요」 사건 전부(S15P31A101-595).
 * staleEvents 는 지난 관제 칸의 이벤트 수(사건이 아니라 규칙 하나하나)다.
 */
export interface EventSections {
  active: Incident[];
  resolved: ResolvedGroup[];
  stale: Incident[];
  staleEvents: number;
}

export function eventSections(entries: readonly EventEntry[]): EventSections {
  const { now, stale } = splitStale(entries);
  return {
    active: groupIncidents(now).filter((i) => i.active),
    resolved: resolvedGroups(entries).slice(0, RECENT_RESOLVED_INCIDENTS),
    stale: groupIncidents(stale),
    staleEvents: stale.length,
  };
}

/** 지금 Tier 0 가 들고 있지 않은 이벤트를 뺀다(진행 중·해소만 남김). 같은 사람 묶음에 지난 관제가 섞이지 않게 쓴다. */
export const withoutStale = (entries: readonly EventEntry[]): EventEntry[] =>
  splitStale(entries).now;

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

/**
 * 이 이벤트가 든 묶음. 패널과 같이 지금 쪽과 지난 관제 쪽을 따로 묶는다. 사람별 묶음에 없는 해소 이벤트는 해소 묶음이다.
 * 목록에서 빠졌으면 없다.
 */
export function incidentOf(entries: readonly EventEntry[], eventId: string): Incident | undefined {
  const holds = (i: Incident) => i.entries.some((e) => e.event.event_id === eventId);
  const { now, stale } = splitStale(entries);
  return (
    groupIncidents(now).find(holds) ??
    groupIncidents(stale).find(holds) ??
    resolvedGroups(entries)
      .map((g) => g.incident)
      .find(holds)
  );
}

/** 카드 머리줄이 보이는 대표 규칙: 묶음에서 가장 높은 등급(진행 중 먼저). */
export const topEntry = (incident: Incident): EventEntry => incident.entries[0]!;

/** 이 ID 의 사람 트랙: 지금 world_id 이거나 병합(ID 교정)으로 aliases 에 들어간 이전 ID. 없으면 없다. */
function personTrack<T extends Pick<WorldTrack, 'world_id' | 'aliases'>>(
  tracks: readonly T[] | undefined,
  id: string,
): T | undefined {
  return tracks?.find((t) => t.world_id === id || t.aliases?.includes(id));
}

/**
 * 이 ID 의 사람이 지금 쓰는 World ID(-661). 병합으로 흡수된 ID 면 생존 트랙의 world_id 다. 없으면 null.
 * 문자열을 돌려주므로 셀렉터로 써도 그 사람의 ID 가 바뀔 때만 다시 그린다.
 */
export function currentPersonId(
  tracks: readonly Pick<WorldTrack, 'world_id' | 'aliases'>[] | undefined,
  id: string | null | undefined,
): string | null {
  return id ? (personTrack(tracks, id)?.world_id ?? null) : null;
}

/** 이 사람을 지금 화면에 담고 있는 카메라 중 첫 번째. 사람이 아니거나 어느 카메라에도 없으면 없다. */
export function bestCamera(
  tracks: readonly Pick<WorldTrack, 'world_id' | 'aliases' | 'cameras'>[] | undefined,
  target: Incident['target'],
): string | undefined {
  if (target?.type !== 'person') return undefined;
  return personTrack(tracks, target.id)?.cameras[0];
}
