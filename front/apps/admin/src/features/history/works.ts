import { workUnitSchema, type Span, type WorkUnit } from '@/features/history/types';

// 작업 목록의 계산: 받은 목록 읽기, 날짜로 묶기, 날짜·시각 보이기.

const DATE = new Intl.DateTimeFormat('ko-KR', {
  month: 'long',
  day: 'numeric',
  weekday: 'short',
});
const CLOCK = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

const DAY_MS = 86_400_000;
/** 작업 구간에 기록이 없을 때 그날 관제 기록으로 넓혔음을 알리는 안내 한 줄. */
export const DAY_FALLBACK_NOTICE = '작업 구간에 기록이 없어 그날 관제 기록을 보여 줍니다.';

/** 받은 작업 목록에서 형식이 맞는 것만 고른다. */
export function readWorks(items: readonly unknown[]): WorkUnit[] {
  return items.flatMap((item) => {
    const parsed = workUnitSchema.safeParse(item);
    return parsed.success && parsed.data.to_ms > parsed.data.from_ms ? [parsed.data] : [];
  });
}

/**
 * 시작한 작업만 남긴다(서버 시작 기록 actual_started_at 이 있는 것: 진행 중·끝난 작업).
 * 작업은 같은 객체로 돌려줘 다시 보기가 다시 만들어지지 않는다.
 */
export const startedWorks = (works: readonly WorkUnit[]): WorkUnit[] =>
  works.filter((work) => work.actual_started_at != null);

/** 세팅 완료(setup_completed_at)와 실제 시작(actual_started_at) 중 있는 것의 이른 쪽. 둘 다 없으면 null. */
function earliestStartOf(
  work: Pick<WorkUnit, 'setup_completed_at' | 'actual_started_at'>,
): number | null {
  const setup = work.setup_completed_at ?? null;
  const started = work.actual_started_at ?? null;
  if (setup === null) return started;
  if (started === null) return setup;
  return Math.min(setup, started);
}

/**
 * 다시 볼 구간: 시작 = min(setup_completed_at, actual_started_at)(둘 중 있는 것), 끝 = actual_ended_at(진행 중이면 now).
 * 시작 기록이 둘 다 없거나 길이가 없으면 null 이다(방금 시작했거나 시작과 같은 시각에 끝난 작업은 서버도 구간으로 받지 않는다: from < to).
 */
export function replayRange(
  work: Pick<WorkUnit, 'setup_completed_at' | 'actual_started_at' | 'actual_ended_at'>,
  now: number,
): Span | null {
  const from = earliestStartOf(work);
  if (from === null) return null;
  const to = work.actual_ended_at ?? now;
  return to > from ? { from, to } : null;
}

/**
 * 작업 날짜(현장 날짜 Asia/Seoul YYYY-MM-DD)의 하루 구간: 00:00~24:00, 오늘(now 가 그날 24:00 전)이면 지금까지(now).
 * 날짜 형식이 틀리거나 아직 오지 않은 날(now <= 00:00)이면 null 이다.
 */
export function dayReplayRange(date: string, now: number): Span | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const from = Date.parse(`${date}T00:00:00+09:00`);
  if (!Number.isFinite(from)) return null;
  const to = Math.min(from + DAY_MS, now);
  return to > from ? { from, to } : null;
}

/** 작업을 시작한 시각(정렬용). 시작 기록이 없으면 계획 시작 시각이다. */
const startOf = (work: WorkUnit) => work.actual_started_at ?? work.from_ms;

/** 작업 목록을 받기 전의 안내 한 줄. */
export const worksLoadingLine = (failed: boolean) =>
  failed ? '작업 목록을 불러오지 못했습니다.' : '불러오는 중입니다…';

/** 작업 날짜(YYYY-MM-DD)를 "10월 29일 (수)"처럼 보인다. 형식이 틀리면 그대로 둔다. */
export function formatDate(date: string): string {
  const [y, m, d] = date.split('-').map(Number);
  if (!y || !m || !d) return date;
  const parts = DATE.formatToParts(new Date(y, m - 1, d));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? '';
  return `${part('month')} ${part('day')}일 (${part('weekday')})`;
}

/** 시:분 "09:00". */
export const formatClock = (ms: number) => CLOCK.format(ms);

/** 작업 시간대 "09:00–12:00". */
export const timeRange = (from: number, to: number) => `${formatClock(from)}–${formatClock(to)}`;

/** 실제 작업 시간대 "09:02–11:48". 아직 끝나지 않았으면 "09:02–진행 중". */
export const workTimeRange = (work: WorkUnit) =>
  work.actual_started_at == null
    ? timeRange(work.from_ms, work.to_ms)
    : `${formatClock(work.actual_started_at)}–${
        work.actual_ended_at == null ? '진행 중' : formatClock(work.actual_ended_at)
      }`;

/** 날짜별로 묶는다(새 날짜부터, 날짜 안에서는 시작 시각순). */
export function groupByDate(works: readonly WorkUnit[]): { date: string; works: WorkUnit[] }[] {
  const byDate = new Map<string, WorkUnit[]>();
  for (const work of works) {
    const sameDay = byDate.get(work.date);
    if (sameDay) sameDay.push(work);
    else byDate.set(work.date, [work]);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => b.localeCompare(a))
    .map(([date, list]) => ({ date, works: list.sort((a, b) => startOf(a) - startOf(b)) }));
}
