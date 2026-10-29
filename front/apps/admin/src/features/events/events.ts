import type { EventItem, Severity } from '@/features/events/types';

// 관제 화면 이벤트 목록의 계산(명세 3.10, 7.4, 12.4, FR-4·8·9). 화면 없이 시험한다.

export const SEVERITY_RANK: Record<Severity, number> = {
  CRITICAL: 3,
  HIGH: 2,
  CAUTION: 1,
  INFO: 0,
};

/** 화면이 들고 있는 이벤트 하나. */
export interface EventEntry {
  event: EventItem;
  /**
   * 지금 경보 단계가 열린 시각(EC2 ms). 경보 단계는 발생·등급 상향·재개가 연다(명세 3.10).
   * 이 시각 뒤의 확인(ACK)만 소리를 멈춘다. acks 에 확인 대상 event_sequence 가 없어 시각으로 판단한다.
   */
  stageStartedAt: number | null;
}

const eventTime = (event: EventItem) => event.updated_at ?? event.raised_at ?? null;

/**
 * 처음 본 이벤트(로그인 직후 목록 등)의 경보 단계 시작. 발생·운영자 상향 시각 중 늦은 쪽이다.
 * 재개됐거나 앞선 확인(acks)이 있으면 마지막 갱신 시각까지 본다. 재개 시각과 규칙이 등급을 올린 시각은 기록에 없고,
 * acks 에 확인 대상 event_sequence 가 없어(계약 공백) 앞선 확인이 지금 단계 것인지 알 수 없다.
 * 모르면 다시 울리는 쪽을 택한다(명세 3.10: 확인 대상 뒤에 오른 경보는 그 확인으로 멈추지 않는다).
 */
function firstStageStart(event: EventItem): number | null {
  const unknownStage = (event.resume_count ?? 0) > 0 || (event.acks?.length ?? 0) > 0;
  const candidates = [
    event.raised_at,
    unknownStage ? event.updated_at : undefined,
    ...(event.escalations ?? []).map((e) => e.requested_at),
  ].filter((t): t is number => t !== undefined);
  return candidates.length ? Math.max(...candidates) : null;
}

/** 확인 기록은 지워지지 않는다. 엇갈려 온 메시지에만 있는 확인도 잃지 않게 ack_id 로 합친다. */
function unionAcks(base: EventItem['acks'], other: EventItem['acks']): EventItem['acks'] {
  if (!other?.length) return base;
  if (!base?.length) return other;
  const known = new Set(base.map((a) => a.ack_id));
  const added = other.filter((a) => !known.has(a.ack_id));
  return added.length ? [...base, ...added] : base;
}

/** next 가 prev 보다 새 상태인지. event_sequence 가 있으면 그것으로, 없으면 시각으로 본다. */
function isNewer(prev: EventItem, next: EventItem): boolean {
  if (prev.event_sequence !== undefined && next.event_sequence !== undefined) {
    return next.event_sequence > prev.event_sequence;
  }
  const a = eventTime(prev);
  const b = eventTime(next);
  return a === null || (b !== null && b > a);
}

/**
 * 받은 이벤트를 반영한다. 같거나 오래된 상태면 prev 를 그대로 돌려준다.
 * 다만 확인 기록은 합친다. EC2 가 순번을 올리지 않고 acks 만 늘려 보내도 소리를 멈춘다.
 */
export function mergeEvent(prev: EventEntry | undefined, next: EventItem): EventEntry {
  if (!prev) return { event: next, stageStartedAt: firstStageStart(next) };
  if (!isNewer(prev.event, next)) {
    const acks = unionAcks(prev.event.acks, next.acks);
    return acks === prev.event.acks ? prev : { ...prev, event: { ...prev.event, acks } };
  }
  const raised = SEVERITY_RANK[next.severity] > SEVERITY_RANK[prev.event.severity];
  const resumed = (next.resume_count ?? 0) > (prev.event.resume_count ?? 0);
  const reopened = raised || resumed;
  const acks = unionAcks(next.acks, prev.event.acks);
  return {
    event: acks === next.acks ? next : { ...next, acks },
    stageStartedAt: reopened ? (eventTime(next) ?? prev.stageStartedAt) : prev.stageStartedAt,
  };
}

const MAX_RESOLVED = 50;

/**
 * 목록에 이벤트들을 반영한다. 관제 화면은 LIVE 만 다룬다. 바뀐 것이 없으면 같은 배열을 돌려준다.
 * 해소된 이벤트는 최근 것만 남긴다.
 */
export function upsertEvents(
  entries: readonly EventEntry[],
  items: readonly EventItem[],
): EventEntry[] {
  const byId = new Map(entries.map((entry) => [entry.event.event_id, entry]));
  let changed = false;
  for (const item of items) {
    if (item.source !== 'LIVE') continue;
    const prev = byId.get(item.event_id);
    const next = mergeEvent(prev, item);
    if (next !== prev) {
      byId.set(item.event_id, next);
      changed = true;
    }
  }
  if (!changed) return entries as EventEntry[];
  const all = [...byId.values()];
  const resolved = all
    .filter((e) => e.event.lifecycle === 'RESOLVED')
    .sort((a, b) => (eventTime(b.event) ?? 0) - (eventTime(a.event) ?? 0));
  const dropped = new Set(resolved.slice(MAX_RESOLVED).map((e) => e.event.event_id));
  return all.filter((e) => !dropped.has(e.event.event_id));
}

export const isActive = (event: EventItem) => event.lifecycle !== 'RESOLVED';

/** 이벤트 대상 문구: 사람 ID, 사람이 없으면 구역. */
export const eventSubject = (event: EventItem) =>
  event.world_ids[0] ?? (event.zone_id ? `구역 ${event.zone_id}` : '');

/** 지금 경보 단계를 연 뒤에 누가 확인했는지(관리자·반장·현장 누구든). */
export function acknowledgedAt(entry: EventEntry): number | null {
  const since = entry.stageStartedAt ?? 0;
  const acks = (entry.event.acks ?? []).map((a) => a.acked_at).filter((t) => t >= since);
  return acks.length ? Math.max(...acks) : null;
}

/** 관제 화면 소리·점멸 대상(명세 12.4): LIVE·CRITICAL/HIGH·RAISED/UPDATED 이고 지금 단계에서 아직 확인 전. */
export function isAlarming(entry: EventEntry): boolean {
  const { event } = entry;
  return (
    event.source === 'LIVE' &&
    SEVERITY_RANK[event.severity] >= SEVERITY_RANK.HIGH &&
    isActive(event) &&
    acknowledgedAt(entry) === null
  );
}

/** 확인 필요(명세 7.1): 이유가 있거나 needs_check. */
export const needsCheck = (event: EventItem) =>
  event.needs_check || (event.confidence_reasons?.length ?? 0) > 0;

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

function incidentTarget(event: EventItem): Incident['target'] {
  const person = event.world_ids[0];
  if (person) return { type: 'person', id: person };
  if (event.zone_id) return { type: 'zone', id: event.zone_id };
  return null;
}

/** 묶음 안의 해소된 규칙은 최근 것만 보인다. 같은 사람이 같은 규칙에 되풀이해 걸려도 카드가 길어지지 않게. */
const RESOLVED_PER_INCIDENT = 2;

const bySeverity = (a: EventEntry, b: EventEntry) =>
  SEVERITY_RANK[b.event.severity] - SEVERITY_RANK[a.event.severity] ||
  a.event.rule_id.localeCompare(b.event.rule_id);

const byRecent = (a: EventEntry, b: EventEntry) =>
  (eventTime(b.event) ?? 0) - (eventTime(a.event) ?? 0);

export function groupIncidents(entries: readonly EventEntry[]): Incident[] {
  const groups = new Map<string, EventEntry[]>();
  for (const entry of entries) {
    const target = incidentTarget(entry.event);
    const key = target ? `${target.type}:${target.id}` : `event:${entry.event.event_id}`;
    groups.set(key, [...(groups.get(key) ?? []), entry]);
  }
  const incidents = [...groups.entries()].map(([key, group]): Incident => {
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
  });
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

/** 이 이벤트가 든 묶음. 목록에서 빠졌으면 없다. */
export function incidentOf(entries: readonly EventEntry[], eventId: string): Incident | undefined {
  return groupIncidents(entries).find((i) => i.entries.some((e) => e.event.event_id === eventId));
}

/** 확인(ACK)이 들어온 곳. */
export const ACK_CHANNEL: Record<string, string> = {
  CONTROL_ROOM: '관제',
  FOREMAN_PWA: '반장 휴대폰',
  SITE_BUTTON: '현장 버튼',
};

export interface TimelineItem {
  at: number;
  who: string;
  text: string;
}

/** 사건 상세 타임라인: 규칙마다 발생·확인·해소(운영자 종료)를 시각 순으로. 받은 이벤트 값만으로 만든다. */
export function incidentTimeline(incident: Incident): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const { event } of incident.entries) {
    const rule = ruleName(event.rule_id);
    if (event.raised_at !== undefined) {
      items.push({ at: event.raised_at, who: '시스템', text: `${rule} 발생` });
    }
    for (const ack of event.acks ?? []) {
      items.push({
        at: ack.acked_at,
        who: ACK_CHANNEL[ack.channel] ?? ack.channel,
        text: `${rule} 확인 · ${ack.actor_id}`,
      });
    }
    if (!isActive(event)) {
      const at = event.resolved_at ?? eventTime(event);
      const closed = event.resolution?.kind === 'OPERATOR_CLOSED';
      if (at !== null) {
        items.push({
          at,
          who: closed ? '운영자' : '시스템',
          text: `${rule} ${closed ? '종료' : '해소'}`,
        });
      }
    }
  }
  return items.sort((a, b) => a.at - b.at);
}

/** 규칙 하나의 표시 상태. */
export type EntryStatus =
  | { kind: 'alarm' }
  | { kind: 'acked'; at: number }
  | { kind: 'notice' }
  | { kind: 'resolved'; at: number | null; closed: boolean };

export function entryStatus(entry: EventEntry): EntryStatus {
  const { event } = entry;
  if (!isActive(event)) {
    return {
      kind: 'resolved',
      at: event.resolved_at ?? eventTime(event),
      closed: event.resolution?.kind === 'OPERATOR_CLOSED',
    };
  }
  if (SEVERITY_RANK[event.severity] < SEVERITY_RANK.HIGH) return { kind: 'notice' };
  const at = acknowledgedAt(entry);
  return at === null ? { kind: 'alarm' } : { kind: 'acked', at };
}

const TIME = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

/** 이벤트 시각(EC2 ms)을 시:분:초로. */
export const formatTime = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? '—' : TIME.format(ms);

export const RULE_NAME: Record<string, string> = {
  R1: '위험 구역 진입',
  R2: '개구부·가장자리 근접',
  R3: '고소 위치',
  R4: '낙상 의심',
  R5: '진입 예측',
  R6: '위험 구역 내 관측 상실',
  R7: '구역 감시 능력 저하',
};

export const ruleName = (ruleId: string) => RULE_NAME[ruleId] ?? ruleId;

const METRIC_LABEL: Record<string, string> = {
  edge_distance_m: '가장자리',
  height_diff_m: '높이 차',
};

/** 지표 문구. _m 은 미터, _s 는 초로 보인다. */
export function metricLabels(metrics: EventItem['metrics']): string[] {
  return Object.entries(metrics).map(([key, value]) => {
    const unit = key.endsWith('_m') ? 'm' : key.endsWith('_s') ? '초' : '';
    const label = METRIC_LABEL[key] ?? key.replace(/_(m|s)$/, '');
    return `${label} ${value.toFixed(unit === 'm' ? 2 : 1)}${unit}`;
  });
}

export const CONFIDENCE_REASON: Record<string, string> = {
  Z_UNKNOWN: '높이 모름',
  PREDICTED: '예측 위치',
  PROVISIONAL_CALIB: '보정 미확정',
  SINGLE_VIEW: '한 대만 관측',
  JOINT_UNCERTAIN: '관절 불확실',
  IDENTITY_UNCERTAIN: '신원 불확실',
  UNSPECIFIED: '이유 미상',
};
