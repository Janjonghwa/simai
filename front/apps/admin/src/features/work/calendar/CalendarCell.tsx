import type { DayMark, DaySummary } from '@/features/work/calendar/calendarMath';
import type { DateKey } from '@/features/work/workDay';
import { buttonVariants } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Flag } from 'lucide-react';

// 달력 칸 하나: 날짜 + (오늘이면 '오늘' 태그) + 표시. 지난 날은 작업 건수와 위험·경고 수를 더 보인다.

/** 끝난 날의 표시: 알약 없이 작은 아이콘 + 글자(주황은 지연 전용이라 쓰지 않는다). */
function EndedMark({ label, className }: { label: string; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-xs font-semibold text-muted-foreground',
        className,
      )}
    >
      <Flag aria-hidden className="size-3.5" />
      {label}
    </span>
  );
}

/**
 * 오늘 칸의 지금 할 일. 칸 전체가 이미 `<button>` 이라 그 안에 단추를 또 두지 못하므로 단추 모양만 한 `span` 이고,
 * 누르면 칸을 누른 것과 같다(그 날로 간다).
 */
function ActionLabel({ label }: { label: string }) {
  return <span className={buttonVariants({ size: 'sm' })}>{label}</span>;
}

function Mark({ mark, className }: { mark: DayMark; className?: string }) {
  if (mark.kind === 'action') return <ActionLabel label={mark.label} />;
  return <EndedMark label={mark.label} className={className} />;
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
  mark,
  summary,
  disabled,
  onPick,
}: {
  dateKey: DateKey;
  /** 칸에 보이는 날짜 숫자. */
  day: number;
  /** false 면 앞뒤 달의 날짜라 흐리게 보인다(누를 수 있으면 그 날로 간다). */
  inMonth: boolean;
  isToday: boolean;
  /** 그 날의 표시(작업 종료·오늘의 단추). 없으면 비어 있다. */
  mark: DayMark | null;
  /** 지난 날 요약(작업 건수·위험·경고). 지난 날이 아니거나 끝난 작업이 없으면 null. */
  summary: DaySummary | null;
  /** 작업하지 않은 지난 날이라 누를 수 없다. */
  disabled: boolean;
  onPick: (dateKey: DateKey) => void;
}) {
  const [, month] = dateKey.split('-');
  return (
    <button
      type="button"
      aria-label={`${Number(month)}월 ${day}일`}
      disabled={disabled}
      onClick={() => onPick(dateKey)}
      className={cn(
        'flex min-w-0 flex-col items-start gap-1 rounded-[12px] border border-border bg-card px-2.5 py-2 text-left text-foreground',
        'transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring enabled:hover:bg-surface-2',
        !inMonth && 'opacity-45',
        isToday && 'border-2 border-primary bg-primary/10 enabled:hover:bg-primary/16',
      )}
    >
      <span className="flex w-full items-center gap-1.5">
        <span className="text-[15px] font-bold tabular-nums">{day}</span>
        {isToday && (
          <span className="rounded-[4px] bg-primary px-1.5 text-[11px] leading-[18px] font-bold text-primary-foreground">
            오늘
          </span>
        )}
        {summary && mark && <Mark mark={mark} className="ml-auto" />}
      </span>
      {summary && (
        <>
          <span className="text-[13px] text-muted-foreground">작업 {summary.count}건</span>
          <EventCounts critical={summary.critical} high={summary.high} />
        </>
      )}
      {!summary && mark && <Mark mark={mark} />}
    </button>
  );
}
