import { workDayOf } from '@/__tests__/features/work/workFixture';
import type { ReplayIndex } from '@/features/history/replayIndex';
import { dayWorksOf, firstMoment, workChoiceLabel } from '@/features/work/past/pastDay';
import { describe, expect, it } from 'vitest';

const at = (h: number, m: number, s = 0) => new Date(2026, 10, 10, h, m, s).getTime();

describe('dayWorksOf', () => {
  it('그날 작업만 계획 시작 시각순으로 고른다', () => {
    const late = workDayOf({ work_id: 'late', date: '2026-11-10', from_ms: at(14, 0) });
    const early = workDayOf({ work_id: 'early', date: '2026-11-10', from_ms: at(9, 0) });
    const other = workDayOf({ work_id: 'other', date: '2026-11-09', from_ms: at(8, 0) });
    expect(dayWorksOf([late, other, early], '2026-11-10').map((w) => w.work_id)).toEqual([
      'early',
      'late',
    ]);
    expect(dayWorksOf([other], '2026-11-10')).toEqual([]);
  });
});

describe('workChoiceLabel', () => {
  it('이름 · 실제 시간대, 시작하지 않았으면 계획 시간대', () => {
    const started = workDayOf({
      name: '철근 배근',
      actual_started_at: at(9, 2),
      actual_ended_at: at(11, 48),
    });
    expect(workChoiceLabel(started)).toBe('철근 배근 · 09:02–11:48');
    expect(workChoiceLabel({ ...started, actual_ended_at: null })).toBe(
      '철근 배근 · 09:02–진행 중',
    );
    expect(
      workChoiceLabel(
        workDayOf({
          name: '거푸집',
          from_ms: at(10, 0),
          to_ms: at(12, 0),
          actual_started_at: null,
        }),
      ),
    ).toBe('거푸집 · 10:00–12:00');
  });

  it('이름이 비어 있으면 `작업` 이다', () => {
    expect(workChoiceLabel(workDayOf({ name: '', from_ms: at(10, 0), to_ms: at(12, 0) }))).toBe(
      '작업 · 10:00–12:00',
    );
  });
});

describe('firstMoment', () => {
  const event = (eventId: string, raisedAt: number) => ({
    eventId,
    ruleId: 'R2',
    severity: 'HIGH' as const,
    worldIds: [],
    zoneId: null,
    raisedAt,
    resolvedAt: null,
  });
  const index = (events: ReplayIndex['events'], frames: number[] = [at(9, 0, 5)]): ReplayIndex => ({
    frames: frames.map((t) => ({ t, ids: [], tracks: [] })),
    workers: [],
    events,
    gaps: [],
  });

  it('첫 기록 뒤의 첫 사건 시각이다', () => {
    expect(firstMoment(index([event('a', at(9, 10)), event('b', at(9, 20))]))).toBe(at(9, 10));
  });

  it('첫 기록 전의 사건은 건너뛴다(그때는 CCTV 가 빈다)', () => {
    expect(firstMoment(index([event('a', at(9, 0, 1)), event('b', at(9, 20))]))).toBe(at(9, 20));
  });

  it('사건이 없으면 첫 기록 시각이고, 기록이 없으면 null', () => {
    expect(firstMoment(index([]))).toBe(at(9, 0, 5));
    expect(firstMoment(index([event('a', at(9, 10))], []))).toBeNull();
  });
});
