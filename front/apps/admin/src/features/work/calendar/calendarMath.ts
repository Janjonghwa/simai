import { DAY_PROGRESS, type DayProgress, type WorkDayItem } from '@/features/work/types';
import {
  dayStatusOf,
  parseDateKey,
  toDateKey,
  WEEKDAY_NAMES,
  type DateKey,
  type DayKind,
  type DayStatusKey,
} from '@/features/work/workDay';

// 작업 관리 달력의 순수 계산: 그 달이 걸치는 주·칸, 달 이동, 칸 칩, 그날 작업 요약. 시계에 기대지 않는다.

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

// ── 칸 칩 ────────────────────────────────────────────────────────────────────

/** 칩 색 계열. todo 주색 윤곽 · active 주색 옅은 면 · ended 경고색 · done 초록 · neutral 중립. */
export type ChipTone = 'todo' | 'active' | 'ended' | 'done' | 'neutral';

export interface DayChip {
  label: string;
  tone: ChipTone;
}

const TONE_OF_STATUS: Record<DayStatusKey, ChipTone> = {
  none: 'neutral',
  report: 'neutral',
  setting: 'active',
  setup: 'neutral',
  running: 'active',
  ended: 'ended',
  done: 'done',
};

/** 오늘 칸은 아직 아무것도 안 했어도 칩을 보여, 눌러서 시작하는 칸이라는 걸 알린다. */
const TODAY_IDLE_CHIP: DayChip = { label: '보고서 작성 전', tone: 'todo' };

/** 서버 진행 값(`dayStatusOf` 의 글)으로 만든 칩. 진행 값을 모르면 null. */
function chipOfProgress(progress: DayProgress | null | undefined): DayChip | null {
  const status = dayStatusOf(progress);
  return status.label === '' ? null : { label: status.label, tone: TONE_OF_STATUS[status.key] };
}

/** 오늘·예정인 날 칩에 쓸 작업: 하루 작업(daily)이 있으면 그것, 없으면 첫 작업. */
const primaryWorkOf = (works: readonly WorkDayItem[]): WorkDayItem | undefined =>
  works.find((work) => work.daily) ?? works[0];

/** 지난 날 작업의 진행 값. 진행 값을 모르는 옛 응답은 실제로 끝난 작업이면 제출한 것(완료)으로, 아니면 null(칩 없음)로 본다. */
const pastProgressOf = (work: WorkDayItem): DayProgress | null =>
  work.progress ?? (work.actual_ended_at !== null ? 'SUBMITTED' : null);

/** 진행 순서(작을수록 덜 진행됐다). 진행 값을 모르면(null) 가장 앞이다. */
const progressRank = (progress: DayProgress | null): number =>
  progress === null ? -1 : DAY_PROGRESS.indexOf(progress);

/**
 * 지난 날 칩의 진행 값: 하루 작업(daily)이 있으면 그것, 없으면 가장 덜 진행된 작업의 것이다
 * (옛 작업이 여럿인 날은 하나라도 제출하지 못했으면 '완료' 로 보이지 않는다).
 */
function pastDayProgress(works: readonly WorkDayItem[]): DayProgress | null {
  const daily = works.find((work) => work.daily);
  const progresses = (daily ? [daily] : works).map(pastProgressOf);
  return progresses.reduce((least, next) =>
    progressRank(next) < progressRank(least) ? next : least,
  );
}

/**
 * 칸 칩은 서버가 준 진행 값(`progress`)의 글을 그대로 보인다(다시 계산하지 않는다). 지난 날은 `pastDayProgress` 를 따르고
 * 작업이 없으면 칩이 없다. 오늘·예정인 날은 그 날 작업(`primaryWorkOf`)의 진행 값이고, 작업이 없으면 오늘만
 * '보고서 작성 전', 예정인 날은 비운다.
 */
export function dayChipOf(works: readonly WorkDayItem[], kind: DayKind): DayChip | null {
  if (kind === 'past') return works.length > 0 ? chipOfProgress(pastDayProgress(works)) : null;
  return (
    chipOfProgress(primaryWorkOf(works)?.progress) ?? (kind === 'today' ? TODAY_IDLE_CHIP : null)
  );
}

// ── 지난 날 요약 ─────────────────────────────────────────────────────────────

/** 그날 작업 요약: 작업 수와 위험(CRITICAL)·경고(HIGH) 이벤트 합계. */
export interface DaySummary {
  count: number;
  critical: number;
  high: number;
}

export function summarizeWorks(works: readonly WorkDayItem[]): DaySummary {
  return {
    count: works.length,
    critical: works.reduce((sum, work) => sum + (work.event_counts?.CRITICAL ?? 0), 0),
    high: works.reduce((sum, work) => sum + (work.event_counts?.HIGH ?? 0), 0),
  };
}
