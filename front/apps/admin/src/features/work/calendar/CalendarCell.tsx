import type { ChipTone, DayChip, DaySummary } from '@/features/work/calendar/calendarMath';
import type { DateKey } from '@/features/work/workDay';
import { Badge } from '@simai/ui/components/badge';
import { cn } from '@simai/ui/lib/utils';

// 달력 칸 하나: 날짜 + (오늘이면 '오늘' 태그) + 칩. 지난 날은 작업 건수와 위험·경고 수를 더 보인다.

const CHIP_TONE: Record<ChipTone, string> = {
  todo: 'border-primary bg-transparent text-point-soft',
  active: 'border-primary bg-primary/16 text-point-soft',
  ended: 'border-delay bg-delay/14 text-delay',
  done: 'border-ok bg-ok/14 text-ok',
  neutral: 'border-border bg-surface-2 text-muted-foreground',
};

function Chip({ chip, className }: { chip: DayChip; className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn('h-[22px] px-2.5 font-semibold', CHIP_TONE[chip.tone], className)}
    >
      {chip.label}
    </Badge>
  );
}

/** 위험·경고 수. 0 은 회색이고, 둘 다 0 이면 '위험·경고 없음'. */
function EventCounts({ critical, high }: { critical: number; high: number }) {
  if (critical === 0 && high === 0) {
    return <span className="text-[13px] text-muted-foreground">위험·경고 없음</span>;
  }
  return (
    <span className="flex gap-2 font-mono text-xs">
      <span className={critical > 0 ? 'font-bold text-sev-critical' : 'text-muted-foreground'}>
        위험 {critical}
      </span>
      <span className={high > 0 ? 'text-sev-high' : 'text-muted-foreground'}>경고 {high}</span>
    </span>
  );
}

export function CalendarCell({
  dateKey,
  day,
  inMonth,
  isToday,
  chip,
  summary,
  onPick,
}: {
  dateKey: DateKey;
  /** 칸에 보이는 날짜 숫자. */
  day: number;
  /** false 면 앞뒤 달의 날짜라 흐리게 보인다(누르면 그 날로 간다). */
  inMonth: boolean;
  isToday: boolean;
  /** 그 날의 상태 칩. 없으면 칩이 없다. */
  chip: DayChip | null;
  /** 지난 날 요약(작업 건수·위험·경고). 지난 날이 아니거나 작업이 없으면 null. */
  summary: DaySummary | null;
  onPick: (dateKey: DateKey) => void;
}) {
  const [, month] = dateKey.split('-');
  return (
    <button
      type="button"
      aria-label={`${Number(month)}월 ${day}일`}
      onClick={() => onPick(dateKey)}
      className={cn(
        'flex min-w-0 flex-col items-start gap-1 rounded-[12px] border border-border bg-card px-2.5 py-2 text-left text-foreground',
        'transition-colors hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring',
        !inMonth && 'opacity-45',
        isToday && 'border-primary ring-1 ring-primary',
      )}
    >
      <span className="flex w-full items-center gap-1.5">
        <span className="text-[15px] font-bold tabular-nums">{day}</span>
        {isToday && (
          <span className="rounded-[4px] bg-primary px-1.5 text-[11px] leading-[18px] font-bold text-primary-foreground">
            오늘
          </span>
        )}
        {summary && chip && <Chip chip={chip} className="ml-auto" />}
      </span>
      {summary && (
        <>
          <span className="text-[13px] text-muted-foreground">작업 {summary.count}건</span>
          <EventCounts critical={summary.critical} high={summary.high} />
        </>
      )}
      {!summary && chip && <Chip chip={chip} />}
    </button>
  );
}
