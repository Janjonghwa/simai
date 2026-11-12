import type { DayProgress, WorkDayItem } from '@/features/work/types';
import {
  isEndedWork,
  parseDateKey,
  toDateKey,
  TODAY_ACTION,
  WEEKDAY_NAMES,
  type DateKey,
  type DayKind,
} from '@/features/work/workDay';

// 작업 관리 달력의 순수 계산: 그 달이 걸치는 주·칸, 달 이동, 칸 표시, 그날 작업 요약. 시계에 기대지 않는다.

/** 달력이 보이는 달. month 는 1~12 다. */
export interface CalendarMonth {
  year: number;
  month: number;
}

/** 한 주의 칸 수(일~토). */
export const DAYS_PER_WEEK = WEEKDAY_NAMES.length;

/** 그 달의 날 수. */
const daysInMonth = ({ year, month }: CalendarMonth): number => new Date(year, month, 0).getDate();

/** 그 달 1일의 요일 번호(0 = 일요일). 첫 줄에서 앞의 달 밖 칸이 몇 개인지와 같다. */
export const startWeekday = ({ year, month }: CalendarMonth): number =>
  new Date(year, month - 1, 1).getDay();

/** 그 달이 걸치는 주 수(일요일 시작, 4~6). */
export const weeksOfMonth = (month: CalendarMonth): number =>
  Math.ceil((startWeekday(month) + daysInMonth(month)) / DAYS_PER_WEEK);

/** 달력 칸 하나: 날짜 키·날짜 숫자·그 달 안의 날인지. */
export interface CalendarDay {
  key: DateKey;
  day: number;
  inMonth: boolean;
}

/** 그 달이 걸치는 모든 칸(앞뒤 달 밖 날짜 포함). 일요일부터 한 줄에 7칸씩이다. */
export function monthCells(month: CalendarMonth): CalendarDay[] {
  const lead = startWeekday(month);
  return Array.from({ length: weeksOfMonth(month) * DAYS_PER_WEEK }, (_, index) => {
    const date = new Date(month.year, month.month - 1, 1 - lead + index);
    return {
      key: toDateKey(date),
      day: date.getDate(),
      inMonth: date.getMonth() === month.month - 1,
    };
  });
}

/** 달을 delta 달 옮긴다(음수면 이전 달). 해를 넘어도 맞는다. */
export function shiftMonth(month: CalendarMonth, delta: number): CalendarMonth {
  const date = new Date(month.year, month.month - 1 + delta, 1);
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
}

/** 날짜 키가 든 달. 형식이 틀리면 오늘이 든 달이다. */
export function monthOfKey(key: DateKey): CalendarMonth {
  const date = parseDateKey(key) ?? new Date();
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
}

/** 작업을 날짜 키별로 묶는다. */
export function groupByDate(works: readonly WorkDayItem[]): Map<DateKey, WorkDayItem[]> {
  const groups = new Map<DateKey, WorkDayItem[]>();
  for (const work of works) groups.set(work.date, [...(groups.get(work.date) ?? []), work]);
  return groups;
}

// ── 칸 표시 ──────────────────────────────────────────────────────────────────

/**
 * 칸에 보이는 표시. 작업 종료(ended)는 끝난 날의 상태라 알약 없이 아이콘과 글자로만 보이고(작업을 끝내면 보고서 제출까지 하므로
 * 종료와 제출을 나누지 않는다), 단추(action)는 오늘 칸에서 지금 할 일을 알린다.
 */
export type DayMark = { kind: 'ended' | 'action'; label: string };

const ENDED_MARK: DayMark = { kind: 'ended', label: '작업 종료' };

/** 오늘 작업이 없거나 진행 값을 모르면 보고서 단계로 본다(오늘 칸은 '작업 시작' 단추가 된다). */
const IDLE_PROGRESS: DayProgress = 'REPORT';

/** 오늘 칸 표시에 쓸 작업: 하루 작업(daily)이 있으면 그것, 없으면 첫 작업. */
const primaryWorkOf = (works: readonly WorkDayItem[]): WorkDayItem | undefined =>
  works.find((work) => work.daily) ?? works[0];

/** 지난 날 표시: 끝난 작업(`isEndedWork`)이 하나라도 있으면 작업 종료, 없으면 null(빈칸)이다. */
const pastMarkOf = (works: readonly WorkDayItem[]): DayMark | null =>
  works.some(isEndedWork) ? ENDED_MARK : null;

/** 오늘 표시: 그 날 작업(`primaryWorkOf`)의 진행 값이 정한 지금 할 일 단추. 제출했으면 할 일이 없어 작업 종료 표시다. */
function todayMarkOf(works: readonly WorkDayItem[]): DayMark {
  const action = TODAY_ACTION[primaryWorkOf(works)?.progress ?? IDLE_PROGRESS];
  return action === null ? ENDED_MARK : { kind: 'action', label: action };
}

/** 칸 표시. 지난 날은 끝난 작업의 상태, 오늘은 지금 할 일 단추이고, 예정인 날은 늘 비운다. */
export function dayMarkOf(works: readonly WorkDayItem[], kind: DayKind): DayMark | null {
  switch (kind) {
    case 'past':
      return pastMarkOf(works);
    case 'today':
      return todayMarkOf(works);
    case 'future':
      return null;
  }
}

// ── 지난 날 요약 ─────────────────────────────────────────────────────────────

/** 그날 작업 요약: 작업 수와 위험(CRITICAL)·경고(HIGH) 이벤트 합계. */
export interface DaySummary {
  count: number;
  critical: number;
  high: number;
}

/** 지난 날 요약. 끝난 작업(`isEndedWork`)만 세고, 끝난 작업이 없으면 null(빈칸)이다. */
export function summarizeWorks(works: readonly WorkDayItem[]): DaySummary | null {
  const ended = works.filter(isEndedWork);
  if (ended.length === 0) return null;
  return {
    count: ended.length,
    critical: ended.reduce((sum, work) => sum + (work.event_counts?.CRITICAL ?? 0), 0),
    high: ended.reduce((sum, work) => sum + (work.event_counts?.HIGH ?? 0), 0),
  };
}
