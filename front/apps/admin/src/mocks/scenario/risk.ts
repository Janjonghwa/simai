import { EVENT_CYCLE_MS, nextMsgId, SOURCE_ID, uuidTail } from '@/mocks/scenario/shared';
import type { RiskEventMessage } from '@simai/contracts';

// /topic/events 시나리오: 한 바퀴(EVENT_CYCLE_MS)마다 같은 위험 이벤트 전이가 일어난다.
// 계약 예제(contracts/examples)를 바탕으로 시각과 위치만 바꾼다.

type Payload = RiskEventMessage['payload'];
type Base = Pick<Payload, 'rule_id' | 'severity' | 'world_ids' | 'zone_id'>;

/**
 * - edge: w-0015 개구부 근접 CRITICAL(발생 → 가까워짐 → 해소)
 * - zone: 같은 사람 w-0015 의 위험 구역 진입 HIGH(한 묶음 표시, VLM 상향 의견 CRITICAL 포함)
 * - predict: w-0012 진입 예측 CAUTION(안내만, 소리 없음)
 * - unsure: w-0021 높이를 몰라 확인 필요인 HIGH(해소 → 재개 → 해소, VLM 상향 의견 CRITICAL 포함)
 */
const EVENTS: Record<'edge' | 'zone' | 'predict' | 'unsure', Base> = {
  edge: { rule_id: 'R2', severity: 'CRITICAL', world_ids: ['w-0015'], zone_id: 'z-04' },
  zone: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0015'], zone_id: 'z-02' },
  predict: { rule_id: 'R5', severity: 'CAUTION', world_ids: ['w-0012'], zone_id: 'z-02' },
  unsure: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0021'], zone_id: 'z-04' },
};
type EventKey = keyof typeof EVENTS;
const KEYS = Object.keys(EVENTS) as EventKey[];

const ZONE_VLM: Partial<Payload> = {
  rationale: '작업자가 위험 구역 경계 안쪽 단상 위에 서 있다. 난간이 보이지 않는다.',
  proposed_severity: 'CRITICAL',
};

const UNSURE: Partial<Payload> = {
  verification: 'UNVERIFIABLE',
  confidence: 'LOW',
  needs_check: true,
  confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
  rationale: '한쪽 카메라 시야가 가려져 높이를 확정할 수 없으나 개구부 경계 안쪽 진입이 관측된다.',
  proposed_severity: 'CRITICAL',
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
  {
    at: 12_000,
    key: 'zone',
    lifecycle: 'RAISED',
    metrics: { zone_distance_m: 0 },
    change: ZONE_VLM,
  },
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

/** (from, to] 에 드는 시나리오 시각. 바퀴 순서로, 바퀴 안에서는 STEPS 순서로 나온다. */
function occurrencesBetween(from: number, to: number) {
  const out: { cycle: number; step: Step; at: number }[] = [];
  const lastCycle = Math.floor(to / EVENT_CYCLE_MS);
  for (let cycle = Math.floor(from / EVENT_CYCLE_MS); cycle <= lastCycle; cycle++) {
    for (const step of STEPS) {
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
  return occurrencesBetween(from, to).map(({ cycle, step, at }) => riskEvent(cycle, step, at));
}
