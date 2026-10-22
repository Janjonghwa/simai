import {
  acknowledgedAt,
  groupIncidents,
  isAlarming,
  mergeEvent,
  metricLabels,
  severityCounts,
  upsertEvents,
  type EventEntry,
} from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;

function item(change: Partial<EventItem> = {}): EventItem {
  return {
    event_id: 'e-1',
    event_sequence: 1,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity: 'CRITICAL',
    confidence: 'HIGH',
    needs_check: false,
    world_ids: ['w-1'],
    zone_id: 'z-04',
    metrics: { edge_distance_m: 0.82 },
    rationale: null,
    proposed_severity: null,
    raised_at: T,
    updated_at: T,
    ...change,
  };
}

const ack = (at: number) => ({
  ack_id: `a-${at}`,
  channel: 'CONTROL_ROOM',
  actor_role: 'ADMIN',
  actor_id: 'admin',
  acked_at: at,
});

const entry = (change: Partial<EventItem> = {}) => mergeEvent(undefined, item(change));

describe('mergeEvent', () => {
  it('event_sequence 가 같거나 작은 상태는 무시한다', () => {
    const first = entry({ event_sequence: 2, lifecycle: 'UPDATED' });
    expect(mergeEvent(first, item({ event_sequence: 2 }))).toBe(first);
    expect(mergeEvent(first, item({ event_sequence: 1 }))).toBe(first);
    expect(mergeEvent(first, item({ event_sequence: 3 })).event.event_sequence).toBe(3);
  });

  it('sequence 가 없으면 시각으로 순서를 본다', () => {
    const first = entry({ event_sequence: undefined, updated_at: T + 10 });
    expect(mergeEvent(first, item({ event_sequence: undefined, updated_at: T + 5 }))).toBe(first);
    expect(
      mergeEvent(first, item({ event_sequence: undefined, updated_at: T + 20 })).event.updated_at,
    ).toBe(T + 20);
  });

  it('등급이 오르거나 재개되면 경보 단계를 새로 연다', () => {
    const high = entry({ severity: 'HIGH', updated_at: T });
    expect(high.stageStartedAt).toBe(T);
    const same = mergeEvent(high, item({ event_sequence: 2, severity: 'HIGH', updated_at: T + 5 }));
    expect(same.stageStartedAt).toBe(T);
    const raised = mergeEvent(same, item({ event_sequence: 3, updated_at: T + 9 }));
    expect(raised.stageStartedAt).toBe(T + 9);
    const resumed = mergeEvent(
      raised,
      item({ event_sequence: 4, lifecycle: 'UPDATED', resume_count: 1, updated_at: T + 30 }),
    );
    expect(resumed.stageStartedAt).toBe(T + 30);
  });

  it('처음 본 이벤트는 재개·상향 기록까지 보고 단계 시작을 정한다', () => {
    expect(entry({ resume_count: 1, updated_at: T + 40 }).stageStartedAt).toBe(T + 40);
    expect(
      entry({ escalations: [{ target_severity: 'CRITICAL', requested_at: T + 15 }] })
        .stageStartedAt,
    ).toBe(T + 15);
  });

  it('같은 순번이어도 늘어난 확인 기록은 합치고, 같은 메시지가 다시 오면 그대로 둔다', () => {
    const first = entry({ event_sequence: 3, severity: 'HIGH' });
    const acked = mergeEvent(first, item({ event_sequence: 3, acks: [ack(T + 10)] }));
    expect(acked.event.acks).toHaveLength(1);
    expect(acked.event.severity).toBe('HIGH');
    expect(isAlarming(acked)).toBe(false);
    expect(mergeEvent(acked, item({ event_sequence: 3, acks: [ack(T + 10)] }))).toBe(acked);
  });

  it('새 상태에 빠진 확인 기록도 이어 간다', () => {
    const acked = entry({ event_sequence: 3, acks: [ack(T + 10)] });
    const next = mergeEvent(acked, item({ event_sequence: 4, updated_at: T + 20 }));
    expect(next.event.acks?.map((a) => a.ack_id)).toEqual([`a-${T + 10}`]);
  });
});

describe('isAlarming', () => {
  it('LIVE·CRITICAL/HIGH·진행 중이고 지금 단계에서 확인 전이면 울린다', () => {
    expect(isAlarming(entry())).toBe(true);
    expect(isAlarming(entry({ severity: 'HIGH' }))).toBe(true);
    expect(isAlarming(entry({ severity: 'CAUTION' }))).toBe(false);
    expect(isAlarming(entry({ lifecycle: 'RESOLVED' }))).toBe(false);
    expect(isAlarming(entry({ source: 'WHAT_IF' }))).toBe(false);
  });

  it('확인하면 멈추고, 그 뒤 등급이 오르거나 재개되면 다시 울린다', () => {
    const acked = entry({ severity: 'HIGH', acks: [ack(T + 2)] });
    expect(isAlarming(acked)).toBe(false);
    expect(acknowledgedAt(acked)).toBe(T + 2);

    const raised = mergeEvent(
      acked,
      item({ event_sequence: 2, severity: 'CRITICAL', updated_at: T + 5, acks: [ack(T + 2)] }),
    );
    expect(isAlarming(raised)).toBe(true);
    const ackedAgain = mergeEvent(
      raised,
      item({ event_sequence: 3, updated_at: T + 6, acks: [ack(T + 2), ack(T + 7)] }),
    );
    expect(isAlarming(ackedAgain)).toBe(false);
  });

  it('처음 본 이벤트는 마지막 갱신 뒤의 확인만 인정한다(규칙이 올린 등급은 언제 올랐는지 모른다)', () => {
    // HIGH 로 발생(T) → 확인(T+1000) → 규칙이 CRITICAL 로 올림(T+2000). 그 뒤 로그인해 처음 본다.
    const raisedByRule = entry({ updated_at: T + 2000, acks: [ack(T + 1000)] });
    expect(isAlarming(raisedByRule)).toBe(true);
    // 확인이 마지막 갱신과 같거나 뒤면(확인 전이) 확인된 것으로 본다.
    expect(isAlarming(entry({ updated_at: T + 2000, acks: [ack(T + 2000)] }))).toBe(false);
  });
});

describe('upsertEvents', () => {
  it('LIVE 만 담고, 바뀐 것이 없으면 같은 배열을 돌려준다', () => {
    const list = upsertEvents([], [item(), item({ event_id: 'e-2', source: 'REPLAY' })]);
    expect(list.map((e) => e.event.event_id)).toEqual(['e-1']);
    expect(upsertEvents(list, [item()])).toBe(list);
  });

  it('해소된 이벤트는 최근 50개만 남긴다', () => {
    const resolved = Array.from({ length: 60 }, (_, i) =>
      item({ event_id: `r-${i}`, lifecycle: 'RESOLVED', updated_at: T + i }),
    );
    const list = upsertEvents([], [...resolved, item({ event_id: 'live' })]);
    expect(list).toHaveLength(51);
    expect(list.some((e) => e.event.event_id === 'r-0')).toBe(false);
    expect(list.some((e) => e.event.event_id === 'live')).toBe(true);
  });
});

describe('groupIncidents', () => {
  it('같은 사람의 여러 규칙은 한 묶음이고, 묶음 등급은 진행 중 규칙 중 가장 높다', () => {
    const entries: EventEntry[] = [
      entry({ event_id: 'a', rule_id: 'R1', severity: 'HIGH', world_ids: ['w-1'] }),
      entry({
        event_id: 'b',
        rule_id: 'R2',
        severity: 'CRITICAL',
        world_ids: ['w-1'],
        lifecycle: 'RESOLVED',
      }),
      entry({ event_id: 'c', rule_id: 'R5', severity: 'CAUTION', world_ids: ['w-2'] }),
      entry({ event_id: 'd', rule_id: 'R7', severity: 'CAUTION', world_ids: [], zone_id: 'z-02' }),
    ];
    const incidents = groupIncidents(entries);
    expect(incidents.map((i) => i.key)).toEqual(['person:w-1', 'person:w-2', 'zone:z-02']);
    const [first] = incidents;
    expect(first?.severity).toBe('HIGH');
    expect(first?.entries.map((e) => e.event.event_id)).toEqual(['a', 'b']);
    expect(first?.alarming).toBe(true);
  });

  it('진행 중 묶음이 해소된 묶음보다 앞이다', () => {
    const incidents = groupIncidents([
      entry({ event_id: 'old', world_ids: ['w-9'], lifecycle: 'RESOLVED', updated_at: T + 100 }),
      entry({ event_id: 'new', world_ids: ['w-1'], severity: 'CAUTION' }),
    ]);
    expect(incidents.map((i) => i.active)).toEqual([true, false]);
  });

  it('묶음 안의 해소된 규칙은 최근 두 건만 보인다', () => {
    const resolved = [1, 2, 3, 4].map((n) =>
      entry({ event_id: `r${n}`, lifecycle: 'RESOLVED', updated_at: T + n * 100 }),
    );
    const [incident] = groupIncidents([
      ...resolved,
      entry({ event_id: 'live', severity: 'HIGH', updated_at: T }),
    ]);
    expect(incident?.entries.map((e) => e.event.event_id)).toEqual(['live', 'r4', 'r3']);
    expect(incident?.severity).toBe('HIGH');
  });
});

describe('표시 값', () => {
  it('등급 칸은 진행 중 이벤트만 센다', () => {
    expect(
      severityCounts([
        entry({ event_id: 'a' }),
        entry({ event_id: 'b', severity: 'HIGH', confidence_reasons: ['Z_UNKNOWN'] }),
        entry({ event_id: 'c', severity: 'CAUTION', needs_check: true }),
        entry({ event_id: 'd', lifecycle: 'RESOLVED' }),
      ]),
    ).toEqual({ critical: 1, high: 1, caution: 1, needsCheck: 2 });
  });

  it('지표는 단위를 붙여 보인다', () => {
    expect(metricLabels({ edge_distance_m: 0.823, height_diff_m: 2.1, dwell_s: 1.44 })).toEqual([
      '가장자리 0.82m',
      '높이 차 2.10m',
      'dwell 1.4초',
    ]);
  });
});
