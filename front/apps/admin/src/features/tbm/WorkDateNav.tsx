import { todayIsoDate } from '@/features/tbm/taskInput';
import { cn } from '@simai/ui/lib/utils';
import { useRef } from 'react';

// 작업 관리 날짜 고르기: ◀ 날짜 ▶ · 오늘. 날짜를 누르면 브라우저 달력이 열린다. 날짜는 YYYY-MM-DD(작업 date 와 같은 꼴)다.

const WEEKDAY = ['일', '월', '화', '수', '목', '금', '토'];
const STEP =
  'inline-flex size-[34px] items-center justify-center rounded-lg bg-surface-2 text-[13px] text-foreground ring-1 ring-faint ring-inset hover:bg-surface-3';

const parseDay = (day: string) => {
  const [y, m, d] = day.split('-').map(Number);
  return new Date(y!, m! - 1, d!);
};

/** 화면에 보이는 날짜: 11월 5일 (목). */
export const dayText = (day: string) => {
  const d = parseDay(day);
  return `${d.getMonth() + 1}월 ${d.getDate()}일 (${WEEKDAY[d.getDay()]})`;
};

/** day 에서 delta 일 옮긴 날짜(YYYY-MM-DD). */
const shiftDay = (day: string, delta: number) => {
  const d = parseDay(day);
  d.setDate(d.getDate() + delta);
  return todayIsoDate(d);
};

export function WorkDateNav({
  day,
  today,
  onChange,
}: {
  day: string;
  today: string;
  onChange: (day: string) => void;
}) {
  const picker = useRef<HTMLInputElement>(null);
  const openPicker = () => {
    const el = picker.current;
    if (!el) return;
    try {
      el.showPicker();
    } catch {
      el.focus();
    }
  };
  return (
    <div
      role="group"
      aria-label="날짜"
      className="flex flex-none items-center gap-1 border-r border-border pr-3"
    >
      <button
        type="button"
        aria-label="전날"
        onClick={() => onChange(shiftDay(day, -1))}
        className={STEP}
      >
        ◀
      </button>
      <span className="relative">
        <button
          type="button"
          onClick={openPicker}
          className="h-[34px] rounded-lg bg-surface-2 px-3 text-[15px] font-bold whitespace-nowrap text-foreground hover:bg-surface-3"
        >
          {dayText(day)}
        </button>
        <input
          ref={picker}
          type="date"
          tabIndex={-1}
          aria-label="날짜 고르기"
          value={day}
          onChange={(e) => {
            if (e.target.value) onChange(e.target.value);
          }}
          className="pointer-events-none absolute inset-x-0 bottom-0 h-0 opacity-0"
        />
      </span>
      <button
        type="button"
        aria-label="다음 날"
        onClick={() => onChange(shiftDay(day, 1))}
        className={STEP}
      >
        ▶
      </button>
      <button
        type="button"
        disabled={day === today}
        onClick={() => onChange(today)}
        className={cn(
          STEP,
          // 이미 오늘이면 누를 일이 없지만 흐리게 감추지 않고 '오늘' 상태로 보인다.
          'w-auto px-3 disabled:cursor-default disabled:bg-primary/15 disabled:text-point-soft disabled:opacity-100 disabled:ring-primary/50',
        )}
      >
        오늘
      </button>
    </div>
  );
}
