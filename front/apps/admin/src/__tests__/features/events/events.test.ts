import {
  acknowledgedAt,
  bestCamera,
  cardChip,
  confirmationText,
  ec2Now,
  elapsedLabel,
  entryEtaLine,
  firstActiveRaisedAt,
  firstRaisedAt,
  groupIncidents,
  incidentOf,
  incidentTimeline,
  isAlarming,
  mergeEvent,
  metricLabels,
  needsConfirmation,
  placeLine,
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
  it('지표는 단위를 붙여 보인다', () => {
    expect(metricLabels({ edge_distance_m: 0.823, height_diff_m: 2.1, dwell_s: 1.44 })).toEqual([
      '가장자리 0.82m',
      '높이 차 2.10m',
      'dwell 1.4초',
    ]);
  });
});

describe('사건 상세', () => {
  const r2 = entry({ event_id: 'r2', rule_id: 'R2', raised_at: T + 5_000, updated_at: T + 5_000 });
  const r1 = entry({
    event_id: 'r1',
    rule_id: 'R1',
    severity: 'HIGH',
    raised_at: T,
    updated_at: T + 9_000,
    lifecycle: 'RESOLVED',
    resolved_at: T + 9_000,
    acks: [ack(T + 2_000)],
  });

  it('카드 머리줄 시각은 묶음에서 가장 먼저 발생한 시각이다', () => {
    const [incident] = groupIncidents([r2, r1]);
    expect(firstRaisedAt(incident!)).toBe(T);
  });

  it('누른 이벤트가 든 묶음을 찾고, 목록에 없으면 없다', () => {
    expect(incidentOf([r2, r1], 'r1')?.key).toBe('person:w-1');
    expect(incidentOf([r2, r1], 'missing')).toBeUndefined();
  });

  it('타임라인은 규칙마다 발생·확인·해소를 시각 순으로 늘어놓는다', () => {
    const [incident] = groupIncidents([r2, r1]);
    expect(incidentTimeline(incident!)).toEqual([
      { at: T, who: '시스템', text: '위험 구역 진입 발생' },
      { at: T + 2_000, who: '관제', text: expect.stringMatching(/^위험 구역 진입 확인 · /) },
      { at: T + 5_000, who: '시스템', text: '개구부·가장자리 근접 발생' },
      { at: T + 9_000, who: '시스템', text: '위험 구역 진입 해소' },
    ]);
  });

  it('운영자가 끝낸 규칙은 운영자 종료로 적는다', () => {
    const closed = entry({
      lifecycle: 'RESOLVED',
      resolved_at: T + 1_000,
      resolution: { kind: 'OPERATOR_CLOSED', reason: '오탐', requested_at: T },
    });
    const [incident] = groupIncidents([closed]);
    expect(incidentTimeline(incident!).at(-1)).toEqual({
      at: T + 1_000,
      who: '운영자',
      text: '개구부·가장자리 근접 종료',
    });
  });
});

describe('카드 표시 계산', () => {
  const incidentOfEntries = (...entries: EventEntry[]) => groupIncidents(entries)[0]!;
  const HH = /^확인 \d{2}:\d{2}:\d{2}$/;

  it('경과는 초, 1분 넘으면 분과 초로 쓰고 음수는 0초다', () => {
    expect(elapsedLabel(0)).toBe('0초');
    expect(elapsedLabel(12_900)).toBe('12초');
    expect(elapsedLabel(60_000)).toBe('1분');
    expect(elapsedLabel(200_000)).toBe('3분 20초');
    expect(elapsedLabel(-5_000)).toBe('0초');
    expect(ec2Now(T, 250)).toBe(T + 250);
    expect(ec2Now(T, undefined)).toBe(T);
  });

  it('경과 기준은 진행 중인 규칙 중 가장 먼저 발생한 시각이다', () => {
    const incident = incidentOfEntries(
      entry({ event_id: 'a', raised_at: T + 5_000 }),
      entry({ event_id: 'b', rule_id: 'R1', severity: 'HIGH', raised_at: T + 2_000 }),
      entry({ event_id: 'c', rule_id: 'R3', lifecycle: 'RESOLVED', raised_at: T - 9_000 }),
    );
    expect(firstActiveRaisedAt(incident)).toBe(T + 2_000);
    expect(
      firstActiveRaisedAt(incidentOfEntries(entry({ lifecycle: 'RESOLVED', resolved_at: T }))),
    ).toBeNull();
  });

  it('진입 예측만 N초 뒤 진입 예상 줄을 만들고, 개구부 수치는 만들지 않는다', () => {
    const r5 = item({ rule_id: 'R5', severity: 'CAUTION', metrics: { time_to_entry_s: 1.64 } });
    expect(entryEtaLine(r5)).toBe('1.6초 뒤 진입 예상');
    expect(entryEtaLine({ ...r5, lifecycle: 'RESOLVED' })).toBeNull();
    expect(entryEtaLine({ ...r5, metrics: {} })).toBeNull();
    expect(
      entryEtaLine(item({ metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 } })),
    ).toBeNull();
  });

  it('처리 상태 칩: 불일치 > 조치 단계 > 확인 시각 > 미확인, 해소되면 해소 시각', () => {
    const open = incidentOfEntries(entry({ updated_at: T }));
    expect(cardChip(open)).toEqual({ kind: 'unacked', label: '미확인' });
    expect(cardChip(open, { ticketStage: '조치 중' })).toEqual({
      kind: 'ticket',
      label: '조치 중',
    });
    expect(cardChip(open, { ticketStage: '조치 중', mismatches: { 'e-1': {} } })).toEqual({
      kind: 'mismatch',
      label: '불일치',
    });

    const acked = incidentOfEntries(entry({ acks: [ack(T + 3_000)], updated_at: T + 3_000 }));
    expect(cardChip(acked)?.kind).toBe('ack');
    expect(cardChip(acked)?.label).toMatch(HH);
    // 확인했어도 불일치가 나중에 걸리면 불일치가 앞선다.
    expect(cardChip(acked, { mismatches: { 'e-1': {} } })?.kind).toBe('mismatch');

    const done = incidentOfEntries(
      entry({ lifecycle: 'RESOLVED', resolved_at: T + 9_000, updated_at: T + 9_000 }),
    );
    expect(cardChip(done)?.kind).toBe('resolved');
    expect(cardChip(done)?.label).toMatch(/^해소 \d{2}:\d{2}:\d{2}$/);
    // 해소된 규칙의 불일치는 보이지 않는다.
    expect(cardChip(done, { mismatches: { 'e-1': {} } })?.kind).toBe('resolved');
  });

  it('주의·안내만 있는 묶음은 처리 상태 칩이 없다', () => {
    expect(cardChip(incidentOfEntries(entry({ severity: 'CAUTION' })))).toBeNull();
    expect(cardChip(incidentOfEntries(entry({ severity: 'INFO' })))).toBeNull();
  });

  it('장소 줄: 대상 · 첫 구역은 번호와 이름, 나머지 구역은 번호만', () => {
    const names: Record<string, string> = { 'z-04': '개구부', 'z-02': '단상' };
    const name = (id: string) => names[id];
    const incident = incidentOfEntries(
      entry({ event_id: 'a', zone_id: 'z-04' }),
      entry({ event_id: 'b', rule_id: 'R1', severity: 'HIGH', zone_id: 'z-02' }),
      entry({ event_id: 'c', rule_id: 'R3', severity: 'HIGH', zone_id: 'z-04' }),
    );
    expect(placeLine(incident, name)).toBe('w-1 · z-04 개구부 · z-02');
    expect(placeLine(incident)).toBe('w-1 · z-04 · z-02');
  });

  it('구역이 대상인 묶음은 그 구역을 먼저 적고 되풀이하지 않는다', () => {
    const incident = incidentOfEntries(entry({ world_ids: [], zone_id: 'z-04' }));
    expect(placeLine(incident, () => '개구부')).toBe('z-04 개구부');
  });

  it('카메라는 사람이 지금 담긴 카메라 중 첫 번째이고, 사람이 아니거나 없으면 없다', () => {
    const tracks: Parameters<typeof bestCamera>[0] = [
      { world_id: 'w-1', cameras: ['cam2', 'cam1'] },
      { world_id: 'w-2', aliases: ['w-9'], cameras: [] },
      { world_id: 'w-3', aliases: ['w-8'], cameras: ['cam3'] },
    ];
    expect(bestCamera(tracks, { type: 'person', id: 'w-1' })).toBe('cam2');
    expect(bestCamera(tracks, { type: 'person', id: 'w-8' })).toBe('cam3');
    expect(bestCamera(tracks, { type: 'person', id: 'w-2' })).toBeUndefined();
    expect(bestCamera(tracks, { type: 'zone', id: 'z-1' })).toBeUndefined();
    expect(bestCamera(undefined, { type: 'person', id: 'w-1' })).toBeUndefined();
  });

  it('확인 필요: UNVERIFIABLE·needs_check·이유 중 하나면 요청하고, 첫 이유를 붙인다(복원 이벤트)', () => {
    expect(needsConfirmation(item())).toBe(false);
    expect(needsConfirmation(item({ needs_check: true }))).toBe(true);
    expect(needsConfirmation(item({ confidence_reasons: ['PREDICTED'] }))).toBe(true);
    const restored = item({ verification: 'UNVERIFIABLE' });
    expect(needsConfirmation(restored)).toBe(true);
    expect(confirmationText(restored)).toBe('확인 필요');
    expect(
      confirmationText(item({ needs_check: true, confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'] })),
    ).toBe('확인 필요 · 높이 모름');
  });
});
