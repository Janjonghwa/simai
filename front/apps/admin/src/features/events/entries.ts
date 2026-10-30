import { MAX_RESOLVED, SEVERITY_RANK } from '@/features/events/constants';
import type { EventEscalation, EventItem, Severity } from '@/features/events/types';

// 이벤트 하나하나의 상태 계산(명세 3.10, 12.4, FR-8). 화면 없이 시험한다.

/** 화면이 들고 있는 이벤트 하나. */
export interface EventEntry {
  event: EventItem;
  /**
   * 지금 경보 단계가 열린 시각(EC2 ms). 경보 단계는 발생·등급 상향·재개가 연다(명세 3.10).
   * 이 시각 뒤의 확인(ACK)만 소리를 멈춘다. acks 에 확인 대상 event_sequence 가 없어 시각으로 판단한다.
   */
  stageStartedAt: number | null;
}

/** 규칙 하나의 표시 상태. */
export type EntryStatus =
  | { kind: 'alarm' }
  | { kind: 'acked'; at: number }
  | { kind: 'notice' }
  | { kind: 'resolved'; at: number | null; closed: boolean };

export const eventTime = (event: EventItem) => event.updated_at ?? event.raised_at ?? null;

export const isActive = (event: EventItem) => event.lifecycle !== 'RESOLVED';

/** 소리·점멸·확인 대상 등급(HIGH 이상). */
export const isAlarmSeverity = (severity: Severity) =>
  SEVERITY_RANK[severity] >= SEVERITY_RANK.HIGH;

/** 최근 갱신 순. */
export const byRecent = (a: EventEntry, b: EventEntry) =>
  (eventTime(b.event) ?? 0) - (eventTime(a.event) ?? 0);

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

function escalationKey(item: EventEscalation): string {
  return (
    item.escalation_id ??
    item.assessment_job_id ??
    `${item.requested_at}:${item.target_severity}:${item.actor_id ?? ''}`
  );
}

/** 재개 전까지는 운영자 상향 기록도 유지한다(명세 5.4). */
function unionEscalations(
  base: EventItem['escalations'],
  other: EventItem['escalations'],
): EventItem['escalations'] {
  if (!other?.length) return base;
  if (!base?.length) return other;
  const known = new Set(base.map(escalationKey));
  const added = other.filter((e) => !known.has(escalationKey(e)));
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

/** 등급이 오르거나 재개되면 새 경보 단계가 열린다. */
const opensNewStage = (prev: EventItem, next: EventItem) =>
  SEVERITY_RANK[next.severity] > SEVERITY_RANK[prev.severity] ||
  (next.resume_count ?? 0) > (prev.resume_count ?? 0);

/**
 * 받은 이벤트를 반영한다. 같거나 오래된 상태면 prev 를 그대로 돌려준다.
 * 다만 확인·상향 기록은 합친다. 재개(resume_count 증가)된 전이는 이전 상향 기록을 비운다(명세 5.4).
 */
export function mergeEvent(prev: EventEntry | undefined, next: EventItem): EventEntry {
  if (!prev) return { event: next, stageStartedAt: firstStageStart(next) };
  const resumed = (next.resume_count ?? 0) > (prev.event.resume_count ?? 0);
  if (!isNewer(prev.event, next)) {
    const acks = unionAcks(prev.event.acks, next.acks);
    const escalations = resumed
      ? prev.event.escalations
      : unionEscalations(prev.event.escalations, next.escalations);
    return acks === prev.event.acks && escalations === prev.event.escalations
      ? prev
      : { ...prev, event: { ...prev.event, acks, escalations } };
  }
  const acks = unionAcks(next.acks, prev.event.acks);
  const escalations = resumed
    ? next.escalations
    : unionEscalations(next.escalations, prev.event.escalations);
  return {
    event:
      acks === next.acks && escalations === next.escalations
        ? next
        : { ...next, acks, escalations },
    stageStartedAt: opensNewStage(prev.event, next)
      ? (eventTime(next) ?? prev.stageStartedAt)
      : prev.stageStartedAt,
  };
}

/** 해소된 이벤트는 최근 것(MAX_RESOLVED 건)만 남긴다. */
function dropOldResolved(entries: EventEntry[]): EventEntry[] {
  const resolved = entries.filter((e) => !isActive(e.event));
  if (resolved.length <= MAX_RESOLVED) return entries;
  const dropped = new Set(
    resolved
      .sort(byRecent)
      .slice(MAX_RESOLVED)
      .map((e) => e.event.event_id),
  );
  return entries.filter((e) => !dropped.has(e.event.event_id));
}

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
  return changed ? dropOldResolved([...byId.values()]) : (entries as EventEntry[]);
}

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
    isAlarmSeverity(event.severity) &&
    isActive(event) &&
    acknowledgedAt(entry) === null
  );
}

export function entryStatus(entry: EventEntry): EntryStatus {
  const { event } = entry;
  if (!isActive(event)) {
    return {
      kind: 'resolved',
      at: event.resolved_at ?? eventTime(event),
      closed: event.resolution?.kind === 'OPERATOR_CLOSED',
    };
  }
  if (!isAlarmSeverity(event.severity)) return { kind: 'notice' };
  const at = acknowledgedAt(entry);
  return at === null ? { kind: 'alarm' } : { kind: 'acked', at };
}

/** 확인 필요(명세 7.1): 이유가 있거나 needs_check. */
export const needsCheck = (event: EventItem) =>
  event.needs_check || (event.confidence_reasons?.length ?? 0) > 0;

/**
 * 운영자 확인 요청(FR-4, FR-115: Tier 0 재시작 뒤 복원한 활성 이벤트는 UNVERIFIABLE 로 유지된다).
 * verification=UNVERIFIABLE 이거나 needs_check·confidence_reasons 가 있으면 확인을 요청한다.
 * 복원 전달 방식(delivery)은 지연 알림 꼬리표가 따로 보인다.
 */
export const needsConfirmation = (event: EventItem) =>
  needsCheck(event) || event.verification === 'UNVERIFIABLE';
