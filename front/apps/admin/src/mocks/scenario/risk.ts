import { EVENT_CYCLE_MS, nextMsgId, SOURCE_ID, uuidTail } from '@/mocks/scenario/shared';
import type { ActionMismatchMessage, RiskEventMessage } from '@simai/contracts';

// /topic/events·/topic/admin/events 시나리오: 한 바퀴(EVENT_CYCLE_MS)마다 같은 위험 이벤트 전이와 조치 불일치가 일어난다.
// 계약 예제(contracts/examples)를 바탕으로 시각과 위치만 바꾼다.

type Payload = RiskEventMessage['payload'];
type Base = Pick<Payload, 'rule_id' | 'severity' | 'world_ids' | 'zone_id'>;

/**
 * - edge: w-0015 개구부 근접 CRITICAL(발생 → 가까워짐 → 해소)
 * - zone: 같은 사람 w-0015 의 위험 구역 진입 HIGH(한 묶음 표시)
 * - predict: w-0012 진입 예측 CAUTION(안내만, 소리 없음)
 * - unsure: w-0021 높이를 몰라 확인 필요인 HIGH(해소 → 재개 → 해소)
 */
const EVENTS: Record<'edge' | 'zone' | 'predict' | 'unsure', Base> = {
  edge: { rule_id: 'R2', severity: 'CRITICAL', world_ids: ['w-0015'], zone_id: 'z-04' },
  zone: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0015'], zone_id: 'z-02' },
  predict: { rule_id: 'R5', severity: 'CAUTION', world_ids: ['w-0012'], zone_id: 'z-02' },
  unsure: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0021'], zone_id: 'z-04' },
};
type EventKey = keyof typeof EVENTS;
const KEYS = Object.keys(EVENTS) as EventKey[];

const UNSURE: Partial<Payload> = {
  verification: 'UNVERIFIABLE',
  confidence: 'LOW',
  needs_check: true,
  confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
};

/** 바퀴 안에서 at(ms)에 일어나는 이벤트 전이 하나. */
interface Step {
  at: number;
  key: EventKey;
  lifecycle: Payload['lifecycle'];
  metrics: Payload['metrics'];
  change?: Partial<Payload>;
}

const STEPS: Step[] = [
  { at: 4_000, key: 'predict', lifecycle: 'RAISED', metrics: { time_to_entry_s: 1.6 } },
  { at: 8_000, key: 'predict', lifecycle: 'RESOLVED', metrics: { time_to_entry_s: 3.2 } },
  {
    at: 10_000,
    key: 'edge',
    lifecycle: 'RAISED',
    metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 },
  },
  { at: 12_000, key: 'zone', lifecycle: 'RAISED', metrics: { zone_distance_m: 0 } },
  {
    at: 16_000,
    key: 'edge',
    lifecycle: 'UPDATED',
    metrics: { edge_distance_m: 0.55, height_diff_m: 2.1 },
  },
  {
    at: 20_000,
    key: 'unsure',
    lifecycle: 'RAISED',
    metrics: { zone_distance_m: 0 },
    change: UNSURE,
  },
  { at: 22_000, key: 'zone', lifecycle: 'RESOLVED', metrics: { zone_distance_m: 0.6 } },
  {
    at: 26_000,
    key: 'edge',
    lifecycle: 'RESOLVED',
    metrics: { edge_distance_m: 1.4, height_diff_m: 2.1 },
  },
  // 관측 해소는 관측으로 확인했을 때만이라 해소 단계는 VERIFIED 다(계약 risk_event, 명세 6.4).
  { at: 27_000, key: 'unsure', lifecycle: 'RESOLVED', metrics: { zone_distance_m: 0.5 } },
  {
    at: 31_000,
    key: 'unsure',
    lifecycle: 'UPDATED',
    metrics: { zone_distance_m: 0 },
    change: { ...UNSURE, resume_count: 1, resolved_at: null },
  },
  {
    at: 36_000,
    key: 'unsure',
    lifecycle: 'RESOLVED',
    metrics: { zone_distance_m: 0.7 },
    change: { resume_count: 1 },
  },
];

/** 이벤트별 전이 목록(시나리오 순서). event_sequence 는 이 안의 순번이다. */
const STEPS_BY_KEY = Object.fromEntries(
  KEYS.map((key) => [key, STEPS.filter((step) => step.key === key)]),
) as Record<EventKey, Step[]>;

const EVENT_ID_PREFIX = '0192a6f0-7c1e-7b3a-9f2d-';

/** 바퀴·이벤트마다 다른 event_id(UUIDv7 형식). */
export const mockEventId = (cycle: number, key: EventKey) =>
  `${EVENT_ID_PREFIX}${uuidTail(cycle * KEYS.length + KEYS.indexOf(key) + 1)}`;

/** (from, to] 에 드는 시나리오 시각. 바퀴 순서로, 바퀴 안에서는 steps 순서로 나온다. */
function occurrencesBetween<T extends { at: number }>(
  steps: readonly T[],
  from: number,
  to: number,
) {
  const out: { cycle: number; step: T; at: number }[] = [];
  const lastCycle = Math.floor(to / EVENT_CYCLE_MS);
  for (let cycle = Math.floor(from / EVENT_CYCLE_MS); cycle <= lastCycle; cycle++) {
    for (const step of steps) {
      const at = cycle * EVENT_CYCLE_MS + step.at;
      if (at > from && at <= to) out.push({ cycle, step, at });
    }
  }
  return out;
}

function riskEvent(cycle: number, step: Step, t: number): RiskEventMessage {
  const same = STEPS_BY_KEY[step.key];
  const raisedAt = cycle * EVENT_CYCLE_MS + same[0]!.at;
  return {
    schema_version: '2.1.0',
    msg_type: 'risk_event',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: SOURCE_ID,
    payload: {
      event_id: mockEventId(cycle, step.key),
      event_sequence: same.indexOf(step) + 1,
      rule_version: 3,
      source: 'LIVE',
      verification: 'VERIFIED',
      confidence: 'HIGH',
      needs_check: false,
      ...EVENTS[step.key],
      lifecycle: step.lifecycle,
      metrics: step.metrics,
      rationale: null,
      proposed_severity: null,
      raised_at: raisedAt,
      updated_at: t,
      ...(step.lifecycle === 'RESOLVED' ? { resolved_at: t } : {}),
      ...step.change,
    },
  };
}

/** /topic/events: (from, to] 사이에 일어난 이벤트 전이. 같은 구간을 두 번 넘기지 않으면 전이마다 한 번씩 나온다. */
export function riskEventsBetween(from: number, to: number): RiskEventMessage[] {
  return occurrencesBetween(STEPS, from, to).map(({ cycle, step, at }) =>
    riskEvent(cycle, step, at),
  );
}

/**
 * 조치 불일치(명세 8.4, /topic/admin/events, 관리자 전용): 반장이 조치 티켓을 완료했는데 이벤트가 관측상 아직 활성이다.
 * 한 바퀴에 zone 이벤트(w-0015 R1 HIGH, 12초 발생 → 22초 해소) 하나에 낸다: 18초에 불일치가 생기고, 이벤트가 해소되는 22초에 풀린다.
 */
const MISMATCH_STEPS: readonly { at: number; key: EventKey; cleared: boolean }[] = [
  { at: 18_000, key: 'zone', cleared: false },
  { at: 22_000, key: 'zone', cleared: true },
];

const DEFAULT_TICKET_ID = 'tkt-mock';
/** 바퀴마다 다른 티켓 번호의 자릿수. */
const TICKET_DIGITS = 4;

/** 불일치를 판단한 이벤트의 상태(순번·단계·구역). */
export type MismatchEventState = Pick<Payload, 'event_sequence' | 'lifecycle' | 'zone_id'>;

/**
 * action_mismatch 한 건(계약 예제 contracts/examples/action_mismatch.json 모양).
 * cleared 가 거짓이면 이벤트는 활성(RAISED·UPDATED)이어야 한다(계약). 해소된 상태면 풀린 것으로 보낸다.
 */
export function actionMismatchMessage(
  eventId: string,
  state: MismatchEventState,
  t: number,
  { ticketId = DEFAULT_TICKET_ID, cleared = false }: { ticketId?: string; cleared?: boolean } = {},
): ActionMismatchMessage {
  return {
    schema_version: '1.0.0',
    msg_type: 'action_mismatch',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: SOURCE_ID,
    payload: {
      ticket_id: ticketId,
      event_id: eventId,
      event_sequence: state.event_sequence ?? 1,
      ticket_status: 'DONE',
      event_lifecycle: state.lifecycle,
      zone_id: state.zone_id,
      detected_at: t,
      cleared: cleared || state.lifecycle === 'RESOLVED',
    },
  };
}

/** 시나리오에서 바퀴 안 at 시각에 그 이벤트가 있던 상태: 그 시각까지의 마지막 전이. */
function scenarioStateAt(key: EventKey, at: number): MismatchEventState {
  const same = STEPS_BY_KEY[key];
  const last = same.filter((step) => step.at <= at).at(-1)!;
  return {
    event_sequence: same.indexOf(last) + 1,
    lifecycle: last.lifecycle,
    zone_id: EVENTS[key].zone_id,
  };
}

/**
 * /topic/admin/events: (from, to] 사이의 조치 불일치. riskEventsBetween 과 같은 구간으로 부르면 그 이벤트 전이 뒤의 상태가 맞는다.
 * find 는 저장소(이벤트 mock)가 매긴 지금 순번·단계를 준다(확인(ACK)도 순번을 올린다). 없으면 시나리오의 순번을 쓴다.
 */
export function actionMismatchesBetween(
  from: number,
  to: number,
  find?: (eventId: string) => MismatchEventState | undefined,
): ActionMismatchMessage[] {
  return occurrencesBetween(MISMATCH_STEPS, from, to).map(({ cycle, step, at }) => {
    const eventId = mockEventId(cycle, step.key);
    const state = find?.(eventId) ?? scenarioStateAt(step.key, step.at);
    return actionMismatchMessage(eventId, state, at, {
      ticketId: `tkt-${String(cycle).padStart(TICKET_DIGITS, '0')}`,
      cleared: step.cleared,
    });
  });
}
