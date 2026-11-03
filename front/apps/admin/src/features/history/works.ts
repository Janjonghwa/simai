import { workUnitSchema, type WorkUnit } from '@/features/history/types';

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

/** 받은 작업 목록에서 형식이 맞는 것만 고른다. */
export function readWorks(items: readonly unknown[]): WorkUnit[] {
  return items.flatMap((item) => {
    const parsed = workUnitSchema.safeParse(item);
    return parsed.success && parsed.data.to_ms > parsed.data.from_ms ? [parsed.data] : [];
  });
}

/**
 * 시작한 작업만 남긴다. 서버에 작업 시작 기록이 아직 없어 계획 시작 시각이 지난 것을 시작한 것으로 본다(임시 기준).
 * 서버 시작 기록이 생기면 이 판정만 바꾼다. 작업은 같은 객체로 돌려줘 다시 보기가 다시 만들어지지 않는다.
 */
export const startedWorks = (works: readonly WorkUnit[], now: number): WorkUnit[] =>
  works.filter((work) => work.from_ms <= now);

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
    .map(([date, list]) => ({ date, works: list.sort((a, b) => a.from_ms - b.from_ms) }));
}
