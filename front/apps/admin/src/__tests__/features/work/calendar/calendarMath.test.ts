import { workDayOf } from '@/__tests__/features/work/workFixture';
import {
  dayMarkOf,
  groupByDate,
  monthCells,
  monthOfKey,
  shiftMonth,
  startWeekday,
  summarizeWorks,
  weeksOfMonth,
} from '@/features/work/calendar/calendarMath';
import { DAY_PROGRESS, type DayProgress } from '@/features/work/types';
import { describe, expect, it } from 'vitest';

describe('weeksOfMonth · startWeekday', () => {
  it('그 달이 걸치는 주 수를 센다(일요일 시작)', () => {
    // 2026-02-01 은 일요일이고 28일이라 꼭 4주다.
    expect(startWeekday({ year: 2026, month: 2 })).toBe(0);
    expect(weeksOfMonth({ year: 2026, month: 2 })).toBe(4);
    // 2026-11-01 은 일요일, 30일 → 5주.
    expect(weeksOfMonth({ year: 2026, month: 11 })).toBe(5);
    // 2026-08-01 은 토요일, 31일 → 6주.
    expect(startWeekday({ year: 2026, month: 8 })).toBe(6);
    expect(weeksOfMonth({ year: 2026, month: 8 })).toBe(6);
  });
});

describe('monthCells', () => {
  it('일요일에 시작하는 달은 그 달 1일부터 시작하고 끝은 다음 달 날짜로 채운다', () => {
    const cells = monthCells({ year: 2026, month: 11 });
    expect(cells).toHaveLength(35);
    expect(cells[0]).toEqual({ key: '2026-11-01', day: 1, inMonth: true });
    expect(cells[29]).toEqual({ key: '2026-11-30', day: 30, inMonth: true });
    expect(cells[30]).toEqual({ key: '2026-12-01', day: 1, inMonth: false });
    expect(cells[34]).toEqual({ key: '2026-12-05', day: 5, inMonth: false });
  });

  it('토요일에 시작하는 달은 앞 달 날짜 6칸이 앞에 오고 6주(42칸)가 된다', () => {
    const cells = monthCells({ year: 2026, month: 8 });
    expect(cells).toHaveLength(42);
    expect(cells[0]).toEqual({ key: '2026-07-26', day: 26, inMonth: false });
    expect(cells[5]).toEqual({ key: '2026-07-31', day: 31, inMonth: false });
    expect(cells[6]).toEqual({ key: '2026-08-01', day: 1, inMonth: true });
    expect(cells.filter((c) => c.inMonth)).toHaveLength(31);
    expect(cells[41]).toEqual({ key: '2026-09-05', day: 5, inMonth: false });
  });

  it('연말·연초 달도 해를 넘겨 맞는 날짜 키를 만든다', () => {
    const cells = monthCells({ year: 2026, month: 12 });
    expect(cells[0]?.key).toBe('2026-11-29');
    expect(cells.at(-1)?.key).toBe('2027-01-02');
  });
});

describe('shiftMonth · monthOfKey', () => {
  it('달을 옮기고 해를 넘는다', () => {
    expect(shiftMonth({ year: 2026, month: 11 }, 1)).toEqual({ year: 2026, month: 12 });
    expect(shiftMonth({ year: 2026, month: 12 }, 1)).toEqual({ year: 2027, month: 1 });
    expect(shiftMonth({ year: 2026, month: 1 }, -1)).toEqual({ year: 2025, month: 12 });
    expect(shiftMonth({ year: 2026, month: 11 }, 0)).toEqual({ year: 2026, month: 11 });
  });

  it('날짜 키가 든 달을 돌려준다', () => {
    expect(monthOfKey('2026-11-30')).toEqual({ year: 2026, month: 11 });
    expect(monthOfKey('2027-01-01')).toEqual({ year: 2027, month: 1 });
  });
});

describe('groupByDate · summarizeWorks', () => {
  it('작업을 날짜 키별로 묶는다', () => {
    const a = workDayOf({ work_id: '1', date: '2026-11-09' });
    const b = workDayOf({ work_id: '2', date: '2026-11-09' });
    const c = workDayOf({ work_id: '3', date: '2026-11-10' });
    const groups = groupByDate([a, b, c]);
    expect(groups.get('2026-11-09')?.map((w) => w.work_id)).toEqual(['1', '2']);
    expect(groups.get('2026-11-10')?.map((w) => w.work_id)).toEqual(['3']);
    expect(groups.get('2026-11-11')).toBeUndefined();
  });

  it('끝난 작업(종료·제출)만 세고, 작업 수와 위험(CRITICAL)·경고(HIGH)를 더한다(없는 칸은 0)', () => {
    expect(
      summarizeWorks([
        workDayOf({ progress: 'SUBMITTED', event_counts: { CRITICAL: 2, HIGH: 1 } }),
        workDayOf({ progress: 'ENDED', event_counts: { CRITICAL: 1 } }),
        workDayOf({ progress: 'SUBMITTED', event_counts: null }),
      ]),
    ).toEqual({ count: 3, critical: 3, high: 1 });
  });

  it('끝나지 않은 작업은 건수에도 위험·경고에도 들어가지 않는다', () => {
    expect(
      summarizeWorks([
        workDayOf({ progress: 'SUBMITTED', event_counts: { CRITICAL: 1, HIGH: 2 } }),
        workDayOf({ progress: 'IN_PROGRESS', event_counts: { CRITICAL: 5, HIGH: 5 } }),
        workDayOf({ progress: 'SETUP', event_counts: { CRITICAL: 7 } }),
        workDayOf({ progress: 'REPORT' }),
      ]),
    ).toEqual({ count: 1, critical: 1, high: 2 });
  });

  it('끝난 작업이 없으면 요약이 없다(빈칸)', () => {
    expect(summarizeWorks([])).toBeNull();
    expect(
      summarizeWorks([workDayOf({ progress: 'IN_PROGRESS' }), workDayOf({ progress: 'REPORT' })]),
    ).toBeNull();
  });
});

describe('dayMarkOf — 지난 날', () => {
  const ENDED = { kind: 'ended', label: '작업 종료' };
  const mark = (progress: DayProgress) => dayMarkOf([workDayOf({ progress })], 'past');

  it('종료(ENDED)와 제출(SUBMITTED)은 둘 다 작업 종료다(종료와 제출을 나누지 않는다)', () => {
    expect(mark('ENDED')).toEqual(ENDED);
    expect(mark('SUBMITTED')).toEqual(ENDED);
  });

  it('끝나지 않은 작업(보고서·세팅·세팅 완료·작업 중)만 있는 지난 날은 빈칸이다', () => {
    for (const progress of ['REPORT', 'SETUP', 'SETUP_DONE', 'IN_PROGRESS'] as const) {
      expect(mark(progress)).toBeNull();
    }
    expect(dayMarkOf([], 'past')).toBeNull();
  });

  it('진행 값을 모르는 옛 응답은 실제로 끝난 작업만 작업 종료이고 아니면 빈칸이다', () => {
    const ended = workDayOf({ work_id: '1', daily: false, progress: null, actual_ended_at: 1_000 });
    const notEnded = workDayOf({
      work_id: '2',
      daily: false,
      progress: null,
      actual_ended_at: null,
    });
    expect(dayMarkOf([ended], 'past')).toEqual(ENDED);
    expect(dayMarkOf([notEnded], 'past')).toBeNull();
    // 끝나지 않은 작업은 건너뛰고 끝난 작업만 본다.
    expect(dayMarkOf([ended, notEnded], 'past')).toEqual(ENDED);
  });

  it('끝난 작업이 하나라도 있으면 제출 여부·순서와 상관없이 작업 종료다', () => {
    const submitted = workDayOf({ work_id: '1', daily: false, progress: 'SUBMITTED' });
    const ended = workDayOf({ work_id: '2', daily: false, progress: 'ENDED' });
    expect(dayMarkOf([submitted, ended], 'past')).toEqual(ENDED);
    expect(dayMarkOf([ended, submitted], 'past')).toEqual(ENDED);
    expect(dayMarkOf([submitted, submitted], 'past')).toEqual(ENDED);
  });

  it('끝나지 않은 작업이 섞여 있어도 표시는 끝난 작업만 따른다', () => {
    const submitted = workDayOf({ work_id: '1', daily: false, progress: 'SUBMITTED' });
    const running = workDayOf({ work_id: '2', daily: true, progress: 'IN_PROGRESS' });
    const report = workDayOf({ work_id: '3', daily: true, progress: 'REPORT' });
    expect(dayMarkOf([submitted, running, report], 'past')).toEqual(ENDED);
  });
});

describe('dayMarkOf — 오늘', () => {
  const action = (label: string) => ({ kind: 'action', label });
  const mark = (progress: DayProgress | null) => dayMarkOf([workDayOf({ progress })], 'today');

  it('진행 값마다 지금 할 일 단추 글이다(제출하면 단추 없이 작업 종료 표시)', () => {
    expect(mark('REPORT')).toEqual(action('작업 시작'));
    expect(mark('SETUP')).toEqual(action('세팅 이어하기'));
    expect(mark('SETUP_DONE')).toEqual(action('작업 전 확인'));
    expect(mark('IN_PROGRESS')).toEqual(action('관제 중'));
    expect(mark('ENDED')).toEqual(action('보고서 보내기'));
    expect(mark('SUBMITTED')).toEqual({ kind: 'ended', label: '작업 종료' });
  });

  it('작업이 없거나 진행 값을 모르는 작업이면 작업 시작이다', () => {
    expect(dayMarkOf([], 'today')).toEqual(action('작업 시작'));
    expect(mark(null)).toEqual(action('작업 시작'));
  });

  it('하루 작업(daily)이 있으면 그것을 따르고, 없으면 첫 작업을 따른다', () => {
    const old = workDayOf({ work_id: '1', daily: false, progress: 'SUBMITTED' });
    const daily = workDayOf({ work_id: '2', daily: true, progress: 'SETUP' });
    expect(dayMarkOf([old, daily], 'today')).toEqual(action('세팅 이어하기'));
    expect(dayMarkOf([old], 'today')).toEqual({ kind: 'ended', label: '작업 종료' });
  });
});

describe('dayMarkOf — 예정인 날', () => {
  it('작업이 있든 없든 언제나 비운다', () => {
    expect(dayMarkOf([], 'future')).toBeNull();
    for (const progress of DAY_PROGRESS) {
      expect(dayMarkOf([workDayOf({ progress })], 'future')).toBeNull();
    }
    expect(dayMarkOf([workDayOf({ progress: null })], 'future')).toBeNull();
  });
});
