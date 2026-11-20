import {
  CARD_MERGE_GAP_MS,
  GHOST_MAX_LIFE_MS,
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
import { identityOf, knownLifeMs, rootOf, type TrackLifeState } from '@/lib/trackLife';
import type { WorldTrack } from '@simai/contracts';

// 알림 단위 카드와 트랙 번호 변경에 따른 화면 연결을 계산한다(명세 7.4).

/** 카드에 표시할 알림 하나. */
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
  const incidents = [...groupBy(entries, (event) => `event:${event.event_id}`)].map(
    ([key, group]) => buildIncident(key, group),
  );
  return incidents.sort(
    (a, b) =>
      Number(b.active) - Number(a.active) ||
      SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity] ||
      b.latestAt - a.latestAt,
  );
}

/** 이 이벤트가 발생한 횟수: 처음 한 번에, 해소 뒤 곧바로 다시 열린(resume) 횟수를 더한다. */
export const occurrencesOf = (event: Pick<EventItem, 'resume_count'>): number =>
  1 + (event.resume_count ?? 0);

/**
 * 지금 살아 있는(추적 중인) 사람의 World ID 집합(병합으로 흡수된 이전 ID 포함). 화면 묶음(S15P31A101-731)이 「실제 두 번째 사람」을
 * 가리지 않게 쓴다. null 은 모른다(World 를 아직 못 받음)이고, 그때는 모두 살아 있는 것으로 보아 서로 다른 사람을 묶지 않는다.
 */
export type AliveIds = ReadonlySet<string> | null;

const isAlive = (alive: AliveIds, person: string) => alive === null || alive.has(person);

/**
 * 지금 관제 중인데 대상 사람이 카메라에서 사라진(트랙이 끝난) 묶음(S15P31A101-757). 위험이 끝났다는 뜻이 아니라 해소를 기다리는 중이다.
 * 사람 수를 모르거나(null) 구역 대상·지난 관제·이미 해소된 묶음은 아니다.
 */
export const isGone = (incident: Incident, alive: AliveIds): boolean =>
  incident.active &&
  !incident.stale &&
  incident.target?.type === 'person' &&
  !isAlive(alive, incident.target.id);

/**
 * 진행 중 카드를 「진행 중」에서 빼 「최근 해소」 칸에 보일지(S15P31A101-798). 대상 사람이 사라졌으면(isGone) 등급(위험 포함)·확인 여부와
 * 상관없이 옮긴다. 화면에서만 옮기며 서버 기록·확인(ACK)·해소는 건드리지 않는다.
 * 예외(예: 확인 전 낙상 의심 R4 는 확인할 때까지 진행 중에 남김)를 둘 때는 이 함수 하나만 고친다.
 */
export const movesToResolved = (card: CardGroup, alive: AliveIds): boolean =>
  isGone(card.incident, alive);

/** 이 묶음에 든 사람(진행 중 규칙의 대상) ID. */
const activePeople = (incident: Incident): string[] => [
  ...new Set(
    incident.entries
      .filter((e) => isActive(e.event))
      .map((e) => incidentTarget(e.event))
      .filter((t): t is { type: 'person'; id: string } => t?.type === 'person')
      .map((t) => t.id),
  ),
];

/** 유령 트랙 판정(S15P31A101-785)에 쓰는 World ID 기록(lib/trackLife). */
export type GhostLife = Pick<TrackLifeState, 'lives' | 'survivorOf' | 'watched'>;

/**
 * 「지금 안 보임」 카드를 접을 후보: 사라진 사람의 카드이고(isGone), 확인 대기 경보가 없고, 든 사람(병합으로 이어진 ID 포함) 누구에게도
 * 워치가 배정된 적이 없다. 확인 대기 경보가 있는 카드는 운영자가 확인해야 하므로 접지 않는다.
 */
function foldCandidate(card: CardGroup, alive: AliveIds, life: GhostLife): boolean {
  if (!isGone(card.incident, alive) || card.incident.alarming) return false;
  return activePeople(card.incident).every((id) =>
    identityOf(life, id).every((member) => !life.watched[member]),
  );
}

/** 잠깐 보였다 사라졌다: 같은 사람(병합 포함)이 보인 길이를 알고 GHOST_MAX_LIFE_MS 보다 짧다. 모르면 아니다. */
function shortLived(id: string, life: GhostLife): boolean {
  const ms = knownLifeMs(life, identityOf(life, id));
  return ms !== null && ms < GHOST_MAX_LIFE_MS;
}

/**
 * 진행 중 카드를 그대로 둘 것과 접을 것(유령 트랙, S15P31A101-785)으로 나눈다. 접는 카드는 사라진 사람의 카드 중 후보(foldCandidate)이고,
 * 든 사람마다 다음 중 하나다.
 * - 잠깐 보였다 사라졌다(shortLived).
 * - 병합으로 다른 ID 에 흡수됐고, 흡수한 사람이 지금 보이거나 그 사람의 다른 카드가 그대로 남는다(같은 사람의 카드가 이미 있다).
 * 오래 보였거나 보인 길이를 모르는 사람(접속 전부터 있었음·프레임 공백), 워치가 붙었던 사람, 확인 대기 경보는 그대로 둔다.
 */
export function splitGhostCards(
  cards: readonly CardGroup[],
  alive: AliveIds,
  life: GhostLife,
): { kept: CardGroup[]; ghost: CardGroup[] } {
  if (alive === null) return { kept: [...cards], ghost: [] };
  const candidate = new Set(cards.filter((c) => foldCandidate(c, alive, life)));
  const absorbed = (id: string) => rootOf(life, id) !== id;
  const short = new Set(
    [...candidate].filter((c) => activePeople(c.incident).every((id) => shortLived(id, life))),
  );
  // 흡수된 ID 의 카드가 기댈 「같은 사람의 남는 카드」: 후보가 아니거나, 잠깐도 아니고 흡수된 사람도 없는 카드.
  const keptPeople = new Set(
    cards
      .filter(
        (c) =>
          !candidate.has(c) ||
          (!short.has(c) && !activePeople(c.incident).some((id) => absorbed(id))),
      )
      .flatMap((c) => activePeople(c.incident)),
  );
  const duplicate = (id: string) =>
    absorbed(id) &&
    identityOf(life, id).some(
      (member) => member !== id && (keptPeople.has(member) || alive.has(member)),
    );
  const ghost = new Set(
    [...candidate].filter((c) =>
      activePeople(c.incident).every((id) => shortLived(id, life) || duplicate(id)),
    ),
  );
  return {
    kept: cards.filter((c) => !ghost.has(c)),
    ghost: cards.filter((c) => ghost.has(c)),
  };
}

/** 알림 하나를 표시하는 카드. 조건을 만족하는 트랙 변경은 횟수로만 잇는다. */
export interface CardGroup {
  /** 목록 key. 대표 묶음 키(incident.key)와 다를 수 있다(같은 사람의 다른 규칙·구역 해소 카드). */
  id: string;
  /** 카드가 보이는 묶음. 선택·상세·3D 초점은 이 묶음의 대상(살아 있는 사람이 있으면 그 사람)이다. */
  incident: Incident;
  /** 카드 대표 위험(규칙·구역)의 발생 횟수(새 event_id 로 다시 난 것과 해소 뒤 재개를 모두 센다). 2 이상이면 「n회」를 보인다. */
  occurrences: number;
  /** 카드에 묶인 대상(사람 트랙 번호) 수. 1 이면 다른 트랙 번호를 묶지 않은 것이다. */
  people: number;
}

/** 최근 해소 칸의 카드 하나(이전 이름). */
export type ResolvedGroup = CardGroup;

/** 시각 순으로 이을 것 하나: 시작(발생)·끝(마지막 갱신)과 사람. 사람이 없으면(구역 대상) person 이 null 이다. */
export interface ChainItem<T> {
  value: T;
  start: number;
  end: number;
  person: string | null;
}

interface Chain<T> {
  values: T[];
  end: number;
  people: Set<string>;
  /** 이 묶음에 든 사람 중 지금 살아 있는 사람. 살아 있는 서로 다른 두 사람은 한 묶음에 들지 않는다. */
  living: Set<string>;
}

/**
 * 같은 위험·같은 구역 항목을 시각(시작) 순으로 잇는다(S15P31A101-731). 다음일 때 앞 묶음에 넣는다:
 * - 생존 여부를 알고 앞 사람은 사라졌으며 새 알림의 사람 번호가 다르다.
 * - 앞 알림의 마지막 갱신에서 CARD_MERGE_GAP_MS 안에 시작했다.
 */
export function chainItems<T>(items: readonly ChainItem<T>[], alive: AliveIds): T[][] {
  const chains: Chain<T>[] = [];
  for (const item of [...items].sort((a, b) => a.start - b.start)) {
    const person = item.person;
    const living = person !== null && isAlive(alive, person);
    const chain = [...chains]
      .reverse()
      .find(
        (c) =>
          alive !== null &&
          person !== null &&
          !c.people.has(person) &&
          c.people.size > 0 &&
          c.living.size === 0 &&
          item.start >= c.end &&
          item.start - c.end <= CARD_MERGE_GAP_MS,
      );
    if (chain) {
      chain.values.push(item.value);
      chain.end = Math.max(chain.end, item.end);
      if (person !== null) chain.people.add(person);
      if (living) chain.living.add(person);
    } else {
      chains.push({
        values: [item.value],
        end: item.end,
        people: new Set(person === null ? [] : [person]),
        living: new Set(living ? [person] : []),
      });
    }
  }
  return chains.map((c) => c.values);
}

/** 같은 위험·같은 구역 키. */
export const riskKeyOf = (ruleId: string, zoneId: string | null): string =>
  `${ruleId}|${zoneId ?? ''}`;
const riskKey = (event: EventItem): string => riskKeyOf(event.rule_id, event.zone_id);

const personOf = (incident: Incident): string | null =>
  incident.target?.type === 'person' ? incident.target.id : null;

/** 살아 있는 사람의 알림, 없으면 가장 최근 알림을 대표로 삼는다. */
function combineActive(members: readonly Incident[], alive: AliveIds): CardGroup {
  const startOf = (i: Incident) => firstActiveRaisedAt(i) ?? i.latestAt;
  const living = members.find((i) => {
    const person = personOf(i);
    return person !== null && isAlive(alive, person);
  });
  const lead = living ?? members.reduce((a, b) => (startOf(b) > startOf(a) ? b : a));
  const leadRisk = riskKey(topEntry(lead).event);
  const occurrences = members.reduce(
    (sum, i) =>
      sum +
      i.entries
        .filter((e) => isActive(e.event) && riskKey(e.event) === leadRisk)
        .reduce((n, e) => n + occurrencesOf(e.event), 0),
    0,
  );
  const others = members.filter((i) => i !== lead);
  if (others.length === 0) return { id: lead.key, incident: lead, occurrences, people: 1 };
  return {
    id: lead.key,
    incident: {
      ...lead,
      entries: lead.entries,
      severity: lead.severity,
      alarming: lead.alarming,
      latestAt: lead.latestAt,
    },
    occurrences,
    people: members.length,
  };
}

/** 알림별 카드에 같은 위험·구역의 트랙 변경만 연결한다. */
export function activeCards(incidents: readonly Incident[], alive: AliveIds): CardGroup[] {
  const byRisk = new Map<string, ChainItem<Incident>[]>();
  const cards: CardGroup[] = [];
  for (const incident of incidents) {
    const person = personOf(incident);
    if (person === null) {
      cards.push(combineActive([incident], alive));
      continue;
    }
    const key = riskKey(topEntry(incident).event);
    const item = {
      value: incident,
      start: firstActiveRaisedAt(incident) ?? incident.latestAt,
      end: incident.latestAt,
      person,
    };
    const group = byRisk.get(key);
    if (group) group.push(item);
    else byRisk.set(key, [item]);
  }
  for (const group of byRisk.values()) {
    for (const members of chainItems(group, alive)) cards.push(combineActive(members, alive));
  }
  return cards.sort(
    (a, b) =>
      SEVERITY_RANK[b.incident.severity] - SEVERITY_RANK[a.incident.severity] ||
      b.incident.latestAt - a.incident.latestAt,
  );
}

/**
 * 해소된 알림도 같은 위험·구역에서 생존 여부와 시간 조건을 만족할 때만 잇는다.
 * 최근에 해소된 묶음이 앞선다. EC2 마감(S15P31A101-722: Tier 0 가 사라져 닫은 것)은 관측 해소가 아니라 넣지 않는다
 * (지난 작업·이벤트 목록에서는 그대로 보인다).
 */
export function resolvedGroups(
  entries: readonly EventEntry[],
  alive: AliveIds = null,
): CardGroup[] {
  const byRisk = groupBy(
    entries.filter((e) => !isActive(e.event) && serverCloseKind(e.event) === null),
    riskKey,
  );
  const cards: CardGroup[] = [];
  for (const [key, group] of byRisk) {
    const items = group.map((entry) => {
      const end = eventTime(entry.event) ?? entry.event.raised_at ?? 0;
      const target = incidentTarget(entry.event);
      return {
        value: entry,
        start: entry.event.raised_at ?? end,
        end,
        person: target?.type === 'person' ? target.id : null,
      };
    });
    for (const members of chainItems(items, alive)) {
      const first = members[0]!;
      cards.push({
        id: `${key}|${first.event.event_id}`,
        incident: buildIncident(`event:${members[members.length - 1]!.event.event_id}`, [
          members[members.length - 1]!,
        ]),
        occurrences: members.reduce((sum, e) => sum + occurrencesOf(e.event), 0),
        people: new Set(members.map((e) => targetKey(e.event))).size,
      });
    }
  }
  return cards.sort((a, b) => b.incident.latestAt - a.incident.latestAt);
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
 * 이벤트 패널의 세 칸: 진행 중 카드 전부, 이번 실행에서 해소된 카드 전부, 「지난 관제 · 확인 필요」 사건 전부(S15P31A101-595).
 * staleEvents 는 지난 관제 칸의 이벤트 수(사건이 아니라 규칙 하나하나)다.
 */
export interface EventSections {
  active: CardGroup[];
  /**
   * 잠깐 생겼다 사라졌거나 병합으로 흡수된 사람(유령 트랙)의 사라짐 카드(S15P31A101-785). 진행 중에서 빼 목록 끝의 접힌 칸에 둔다.
   * 기록(life)을 주지 않으면 비어 있다.
   */
  ghost: CardGroup[];
  /**
   * 대상 사람이 사라진 진행 중 카드(movesToResolved, S15P31A101-798). 「진행 중」에서 빼 「최근 해소」 칸 맨 앞에 둔다. 아직 해소되지
   * 않았으며 유령 트랙 카드는 ghost 에 있다.
   */
  gone: CardGroup[];
  resolved: CardGroup[];
  stale: Incident[];
  staleEvents: number;
}

export interface SectionOptions {
  /** 지금 살아 있는 사람(화면 묶음이 서로 다른 두 사람을 섞지 않게). 주지 않으면 모른다(서로 다른 사람은 묶지 않는다). */
  alive?: AliveIds;
  /**
   * 지금 실행이 시작된 시각(EC2 ms, S15P31A101-731). 주면 「최근 해소」에 그 뒤에 난 이벤트만 남긴다. null 은 지금 실행이 없다는 뜻이라
   * 「최근 해소」를 비운다(작업 종료 때 Tier 0 가 한꺼번에 닫은 이벤트가 대기 화면에 남지 않게). 주지 않으면(undefined) 거르지 않는다.
   */
  runStartedAt?: number | null;
  /** World ID 가 보인 기록(lib/trackLife). 주면 유령 트랙의 사라짐 카드를 진행 중에서 뺀다(splitGhostCards). 주지 않으면 빼지 않는다. */
  life?: GhostLife;
}

/** 지금 실행의 이벤트만(발생 시각 기준, 모르면 마지막 갱신 시각). */
function inRun(
  entries: readonly EventEntry[],
  runStartedAt: number | null | undefined,
): readonly EventEntry[] {
  if (runStartedAt === undefined) return entries;
  if (runStartedAt === null) return [];
  return entries.filter((e) => (e.event.raised_at ?? eventTime(e.event) ?? 0) >= runStartedAt);
}

export function eventSections(
  entries: readonly EventEntry[],
  { alive = null, runStartedAt, life }: SectionOptions = {},
): EventSections {
  const { now, stale } = splitStale(entries);
  const cards = activeCards(
    groupIncidents(now).filter((i) => i.active),
    alive,
  );
  const { kept, ghost } = life ? splitGhostCards(cards, alive, life) : { kept: cards, ghost: [] };
  return {
    active: kept.filter((card) => !movesToResolved(card, alive)),
    ghost,
    gone: kept.filter((card) => movesToResolved(card, alive)),
    resolved: resolvedGroups(inRun(entries, runStartedAt), alive),
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
 * 이 이벤트에 해당하는 알림을 찾는다.
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
