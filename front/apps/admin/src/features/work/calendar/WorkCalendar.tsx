import { CalendarCell } from '@/features/work/calendar/CalendarCell';
import {
  dayMarkOf,
  groupByDate,
  monthCells,
  monthOfKey,
  shiftMonth,
  summarizeWorks,
  weeksOfMonth,
  type CalendarMonth,
} from '@/features/work/calendar/calendarMath';
import { useWorkDays } from '@/features/work/useWorkDays';
import { dayKind, WEEKDAY_NAMES, type DateKey } from '@/features/work/workDay';
import { Button } from '@simai/ui/components/button';
import { Check, ChevronLeft, ChevronRight } from 'lucide-react';
import { useMemo, useState } from 'react';

// 작업 관리 첫 화면: 큰 월 달력. 날짜를 누르면 그 날로 간다(지난 날 = 다시 보기, 오늘·예정 = 작업 흐름).
// 지난 날 칸은 끝난 작업(종료·제출)만 보이고 작업하지 않은 지난 날은 누를 수 없다. 오늘 칸은 지금 할 일 단추를 보이고, 예정인 날 칸은 비어 있다.
// 한 화면에 맞춘다: 폭 1080 가운데, 아래 80px 비움(5주는 높이 720 에 맞고 6주는 스크롤).

export function WorkCalendar({
  todayKey,
  focusDate,
  notice,
  onPick,
}: {
  /** 오늘 날짜 키. */
  todayKey: DateKey;
  /** 처음 보일 달이 든 날짜(마지막에 연 날짜, 없으면 오늘). */
  focusDate: DateKey;
  /** 머리줄 오른쪽에 잠깐 보이는 알림. 없으면 null. */
  notice: string | null;
  onPick: (date: DateKey) => void;
}) {
  const [month, setMonth] = useState<CalendarMonth>(() => monthOfKey(focusDate));
  const cells = useMemo(() => monthCells(month), [month]);
  // 달 밖 칸에도 칩이 보이도록 그 달이 걸치는 칸 전체의 날짜 범위를 받는다.
  const range = { from: cells[0]!.key, to: cells[cells.length - 1]!.key };
  const { data, error, refetch } = useWorkDays(range);
  const byDate = useMemo(() => groupByDate(data ?? []), [data]);

  return (
    <div className="mx-auto flex min-h-[calc(100vh-140px)] w-full max-w-[1080px] flex-none flex-col gap-3.5 pb-20">
      <header className="flex flex-wrap items-center gap-2.5">
        <h2 className="min-w-[150px] text-xl font-bold tabular-nums">
          {month.year}년 {month.month}월
        </h2>
        <Button
          variant="ghost"
          size="icon"
          aria-label="이전 달"
          onClick={() => setMonth(shiftMonth(month, -1))}
        >
          <ChevronLeft aria-hidden />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label="다음 달"
          onClick={() => setMonth(shiftMonth(month, 1))}
        >
          <ChevronRight aria-hidden />
        </Button>
        <Button variant="secondary" size="sm" onClick={() => setMonth(monthOfKey(todayKey))}>
          오늘
        </Button>
        {error && (
          <span className="flex items-center gap-2 text-sm text-muted-foreground">
            작업 목록을 불러오지 못했습니다.
            <Button variant="secondary" size="sm" onClick={() => void refetch()}>
              다시 시도
            </Button>
          </span>
        )}
        {notice && (
          <span
            role="status"
            className="ml-auto flex items-center gap-1.5 text-sm font-semibold text-ok"
          >
            <Check aria-hidden className="size-4" />
            {notice}
          </span>
        )}
      </header>
      <div
        className="grid flex-1 grid-cols-7 gap-2"
        style={{ gridTemplateRows: `auto repeat(${weeksOfMonth(month)}, minmax(88px, 1fr))` }}
      >
        {WEEKDAY_NAMES.map((name) => (
          <div key={name} className="px-1 py-0.5 text-[13px] font-semibold text-muted-foreground">
            {name}
          </div>
        ))}
        {cells.map(({ key, day, inMonth }) => {
          const kind = dayKind(key, todayKey);
          const works = byDate.get(key) ?? [];
          const summary = kind === 'past' ? summarizeWorks(works) : null;
          return (
            <CalendarCell
              key={key}
              dateKey={key}
              day={day}
              inMonth={inMonth}
              isToday={kind === 'today'}
              mark={dayMarkOf(works, kind)}
              summary={summary}
              disabled={kind === 'past' && summary === null}
              onPick={onPick}
            />
          );
        })}
      </div>
    </div>
  );
}
