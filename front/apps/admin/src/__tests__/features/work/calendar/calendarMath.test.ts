import { workDayOf } from '@/__tests__/features/work/workFixture';
import {
  dayChipOf,
  groupByDate,
  monthCells,
  monthOfKey,
  shiftMonth,
  startWeekday,
  summarizeWorks,
  weeksOfMonth,
} from '@/features/work/calendar/calendarMath';
import type { DayProgress } from '@/features/work/types';
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

  it('작업 수와 위험(CRITICAL)·경고(HIGH)를 더한다(없는 칸은 0)', () => {
    expect(
      summarizeWorks([
        workDayOf({ event_counts: { CRITICAL: 2, HIGH: 1 } }),
        workDayOf({ event_counts: { CRITICAL: 1 } }),
        workDayOf({ event_counts: null }),
      ]),
    ).toEqual({ count: 3, critical: 3, high: 1 });
    expect(summarizeWorks([])).toEqual({ count: 0, critical: 0, high: 0 });
  });
});

describe('dayChipOf', () => {
  it('지난 날은 그날 작업의 진행 상태를 따른다(제출하지 않은 날은 완료가 아니다)', () => {
    const chip = (progress: DayProgress) => dayChipOf([workDayOf({ progress })], 'past');
    expect(chip('SUBMITTED')).toEqual({ label: '완료', tone: 'done' });
    expect(chip('ENDED')).toEqual({ label: '작업 종료', tone: 'ended' });
    expect(chip('REPORT')).toEqual({ label: '보고서 작성', tone: 'neutral' });
    expect(chip('SETUP_DONE')).toEqual({ label: '세팅 완료', tone: 'neutral' });
    expect(chip('IN_PROGRESS')).toEqual({ label: '진행 중', tone: 'active' });
    expect(dayChipOf([], 'past')).toBeNull();
  });

  it('지난 날 진행 값을 모르는 옛 응답은 실제로 끝난 작업만 완료이고 아니면 칩이 없다', () => {
    const ended = workDayOf({ work_id: '1', daily: false, progress: null, actual_ended_at: 1_000 });
    const notEnded = workDayOf({
      work_id: '2',
      daily: false,
      progress: null,
      actual_ended_at: null,
    });
    expect(dayChipOf([ended], 'past')).toEqual({ label: '완료', tone: 'done' });
    expect(dayChipOf([notEnded], 'past')).toBeNull();
    // 덜 끝난 작업이 하나 끼어 있으면 그 날은 완료가 아니다.
    expect(dayChipOf([ended, notEnded], 'past')).toBeNull();
  });

  it('지난 날 하루 작업(daily)이 있으면 그것을, 없으면 가장 덜 진행된 작업을 따른다', () => {
    const submitted = workDayOf({ work_id: '1', daily: false, progress: 'SUBMITTED' });
    const ended = workDayOf({ work_id: '2', daily: false, progress: 'ENDED' });
    const daily = workDayOf({ work_id: '3', daily: true, progress: 'REPORT' });
    // 옛 작업 중 하나만 제출하지 못했어도(순서와 상관없이) 그 날은 작업 종료다.
    expect(dayChipOf([submitted, ended], 'past')?.label).toBe('작업 종료');
    expect(dayChipOf([ended, submitted], 'past')?.label).toBe('작업 종료');
    expect(dayChipOf([submitted, submitted], 'past')?.label).toBe('완료');
    expect(dayChipOf([submitted, ended, daily], 'past')?.label).toBe('보고서 작성');
  });

  it('오늘·예정인 날은 그날 작업의 진행 상태를 따른다', () => {
    const chip = (progress: DayProgress, kind: 'today' | 'future') =>
      dayChipOf([workDayOf({ progress })], kind);
    expect(chip('REPORT', 'today')).toEqual({ label: '보고서 작성', tone: 'neutral' });
    expect(chip('SETUP', 'today')).toEqual({ label: '세팅 중', tone: 'active' });
    expect(chip('SETUP_DONE', 'today')).toEqual({ label: '세팅 완료', tone: 'neutral' });
    expect(chip('IN_PROGRESS', 'today')).toEqual({ label: '진행 중', tone: 'active' });
    expect(chip('ENDED', 'today')).toEqual({ label: '작업 종료', tone: 'ended' });
    expect(chip('SUBMITTED', 'today')).toEqual({ label: '완료', tone: 'done' });
    expect(chip('REPORT', 'future')).toEqual({ label: '보고서 작성', tone: 'neutral' });
  });

  it('작업이 없는 날은 오늘만 보고서 작성 전(주색 윤곽), 예정인 날은 비운다', () => {
    expect(dayChipOf([], 'today')).toEqual({ label: '보고서 작성 전', tone: 'todo' });
    expect(dayChipOf([], 'future')).toBeNull();
    // 진행 값을 모르는 작업(null)도 없는 것과 같다.
    expect(dayChipOf([workDayOf({ progress: null })], 'today')).toEqual({
      label: '보고서 작성 전',
      tone: 'todo',
    });
    expect(dayChipOf([workDayOf({ progress: null })], 'future')).toBeNull();
  });

  it('하루 작업(daily)이 있으면 그것을 따르고, 없으면 첫 작업을 따른다', () => {
    const old = workDayOf({ work_id: '1', daily: false, progress: 'SUBMITTED' });
    const daily = workDayOf({ work_id: '2', daily: true, progress: 'SETUP' });
    expect(dayChipOf([old, daily], 'today')?.label).toBe('세팅 중');
    expect(dayChipOf([old], 'today')?.label).toBe('완료');
  });
});
