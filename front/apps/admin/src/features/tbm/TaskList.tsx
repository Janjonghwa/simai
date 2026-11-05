import { SeverityBadge } from '@/features/events/SeverityBadge';
import { SEVERITY_TEXT, taskPeak, taskPlace } from '@/features/tbm/tbm';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { cn } from '@simai/ui/lib/utils';
import { useEffect, useRef, useState } from 'react';

// 고른 날짜의 작업 목록: 작업마다 한 줄짜리 알약(번호 · 이름 · 상태 · 평가 최고 등급)과 + 작업 등록 단추.
// 진행 중인 작업은 초록 테두리다. 고른 알약 옆 ⋯ 단추로 작업 수정 · 삭제를 연다. 시간대 · 장소 · 인원 · 반장은 말풍선으로 나온다.

const CHIP =
  'inline-flex h-9 items-center gap-2 rounded-full px-3.5 text-sm font-semibold whitespace-nowrap ring-1 ring-inset outline-none transition-[background-color,box-shadow] duration-150 ease-out focus-visible:ring-3 focus-visible:ring-ring/50';

const STATUS_TEXT: Record<NonNullable<TbmTask['status']>, string> = {
  PLANNED: '계획',
  IN_PROGRESS: '진행 중',
  DONE: '종료',
};

const chipTitle = (task: TbmTask) =>
  [`${task.from}–${task.to}`, taskPlace(task), `${task.people}명`, task.lead && `반장 ${task.lead}`]
    .filter(Boolean)
    .join(' · ');

function TaskChip({
  task,
  pressed,
  onSelect,
}: {
  task: TbmTask;
  pressed: boolean;
  onSelect: () => void;
}) {
  const peak = task.status === 'DONE' ? null : taskPeak(task);
  const summary = task.status === 'DONE' ? '작업 종료' : peak ? SEVERITY_TEXT[peak] : '평가 없음';
  const live = task.status === 'IN_PROGRESS';
  return (
    <button
      type="button"
      title={chipTitle(task)}
      aria-label={`${task.id} ${task.name} · ${task.from}–${task.to} · ${summary}`}
      aria-pressed={pressed}
      onClick={onSelect}
      className={cn(
        CHIP,
        'bg-card ring-border hover:bg-surface-2 aria-pressed:bg-surface-3 aria-pressed:ring-2 aria-pressed:ring-primary',
        live && 'ring-2 ring-ok aria-pressed:ring-ok',
      )}
    >
      {task.id} · {task.name}
      <span className={cn('text-[13px] font-medium text-muted-foreground', live && 'text-ok')}>
        {task.status ? STATUS_TEXT[task.status] : '상태 미확인'}
      </span>
      {peak && <SeverityBadge severity={peak} />}
    </button>
  );
}

/** 진행 중 → 예정(상태 미확인 포함) → 끝난 작업 순. 같은 묶음 안에서는 원래 순서. */
const STATUS_RANK = (t: TbmTask) => (t.status === 'IN_PROGRESS' ? 0 : t.status === 'DONE' ? 2 : 1);
export const sortTasksForChips = (tasks: readonly TbmTask[]): TbmTask[] =>
  [...tasks].sort((a, b) => STATUS_RANK(a) - STATUS_RANK(b));

/** 고른 작업의 ⋯ 메뉴: 작업 수정 · 작업 삭제. 바깥을 누르면 닫힌다. */
function TaskMenu({
  task,
  onEdit,
  onDelete,
}: {
  task: TbmTask;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: PointerEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [open]);
  const pick = (run: () => void) => () => {
    setOpen(false);
    run();
  };
  return (
    <span ref={box} className="relative inline-flex">
      <button
        type="button"
        aria-label={`${task.id} 작업 메뉴`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-surface-2 hover:text-foreground aria-expanded:bg-surface-2"
      >
        ⋯
      </button>
      {open && (
        <span
          role="menu"
          className="absolute top-[calc(100%+6px)] left-0 z-20 flex min-w-[140px] flex-col gap-0.5 rounded-xl bg-surface-2 p-1.5 shadow-float ring-1 ring-border ring-inset"
        >
          <button
            type="button"
            role="menuitem"
            onClick={pick(onEdit)}
            className="rounded-lg px-3 py-2 text-left text-sm hover:bg-surface-3"
          >
            작업 수정
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={pick(onDelete)}
            className="rounded-lg px-3 py-2 text-left text-sm text-destructive hover:bg-surface-3"
          >
            작업 삭제
          </button>
        </span>
      )}
    </span>
  );
}

export function TaskList({
  tasks,
  selectedId,
  dayLabel,
  onSelect,
  onAdd,
  onEdit,
  onDelete,
}: {
  tasks: readonly TbmTask[];
  selectedId: string | null;
  /** 고른 날짜(목록 이름에 쓴다). */
  dayLabel: string;
  onSelect: (id: string) => void;
  onAdd: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      role="group"
      aria-label={`${dayLabel} 작업 ${tasks.length}건`}
      className="flex min-w-0 flex-1 flex-wrap items-center gap-2"
    >
      {sortTasksForChips(tasks).map((t) => (
        <span key={t.id} className="inline-flex items-center gap-0.5">
          <TaskChip task={t} pressed={t.id === selectedId} onSelect={() => onSelect(t.id)} />
          {t.id === selectedId && <TaskMenu task={t} onEdit={onEdit} onDelete={onDelete} />}
        </span>
      ))}
      <button
        type="button"
        aria-haspopup="dialog"
        onClick={onAdd}
        className={cn(CHIP, 'text-point-soft ring-faint hover:bg-surface-2')}
      >
        + 작업 등록
      </button>
    </div>
  );
}
