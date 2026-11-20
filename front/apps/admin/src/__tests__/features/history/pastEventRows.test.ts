import { isRowActiveAt, pastEventRows } from '@/features/history/pastEventRows';
import type { EventRow } from '@/features/history/replayIndex';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;
const row = (change: Partial<EventRow>): EventRow => ({
  eventId: 'e-1',
  ruleId: 'R7',
  severity: 'HIGH',
  worldIds: [],
  zoneId: 'z-1',
  raisedAt: T,
  resolvedAt: null,
  ...change,
});

describe('pastEventRows (관제 카드와 같은 묶음 함수, S15P31A101-751)', () => {
  it('생존 여부를 모르는 지난 경보는 번호가 달라도 별도 줄로 표시한다', () => {
    const rows = pastEventRows([
      row({ eventId: 'a', worldIds: ['w-1'], resolvedAt: T + 10_000 }),
      row({ eventId: 'b', worldIds: ['w-2'], raisedAt: T + 30_000, resumeCount: 2 }),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.occurrences)).toEqual([1, 3]);
    expect(rows.map((r) => r.people)).toEqual([['w-1'], ['w-2']]);
    expect(rows[0]!.first.eventId).toBe('a');
  });

  it('같은 사람과 위험이라도 알림별로 표시한다', () => {
    const groups = pastEventRows([
      row({ eventId: 'a', worldIds: ['w-1'] }),
      row({ eventId: 'b', worldIds: ['w-1'], raisedAt: T + 3_600_000 }),
      row({ eventId: 'c', worldIds: ['w-2'], raisedAt: T + 7_200_000 }),
      row({ eventId: 'd', worldIds: ['w-1'], ruleId: 'R2' }),
      row({ eventId: 'e', worldIds: ['w-1'], zoneId: 'z-2' }),
    ]).map((r) => r.members.map((m) => m.eventId));
    expect(groups).toHaveLength(5);
    expect(groups).toContainEqual(['a']);
    expect(groups).toContainEqual(['b']);
    expect(groups).toContainEqual(['c']);
  });

  it('각 줄은 자기 알림의 등급과 활성 시각을 따른다', () => {
    const [only, second] = pastEventRows([
      row({ eventId: 'a', severity: 'HIGH', resolvedAt: T + 5_000 }),
      row({ eventId: 'b', severity: 'CRITICAL', raisedAt: T + 20_000 }),
    ]);
    expect(only!.severity).toBe('HIGH');
    expect(second!.severity).toBe('CRITICAL');
    expect(isRowActiveAt(only!, T + 1_000)).toBe(true);
    expect(isRowActiveAt(only!, T + 10_000)).toBe(false);
    expect(isRowActiveAt(only!, T + 25_000)).toBe(false);
    expect(isRowActiveAt(second!, T + 25_000)).toBe(true);
  });
});
