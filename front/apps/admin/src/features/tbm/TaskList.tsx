import { SeverityBadge } from '@/features/events/SeverityBadge';
import { SEVERITY_TEXT, taskPeak, taskPlace } from '@/features/tbm/tbm';
import { TBM_DAY, type TbmTask } from '@/features/tbm/tbmDemo';
import { cn } from '@simai/ui/lib/utils';
import type { ReactNode } from 'react';

// 내일 작업 목록: 작업마다 한 줄짜리 알약(번호 · 이름 · 평가 최고 등급)과 + 작업 등록 단추. 시간대 · 장소 · 인원 · 반장은
// 마우스를 올리면 말풍선으로 나온다. 알약은 화면 위쪽 한 줄을 다른 단추(children)와 나눠 쓴다.

const CHIP =
  'inline-flex h-9 items-center gap-2 rounded-full px-3.5 text-sm font-semibold whitespace-nowrap ring-1 ring-inset outline-none transition-[background-color,box-shadow] duration-150 ease-out focus-visible:ring-3 focus-visible:ring-ring/50';

/** 알약에 올려 보는 설명. 반장 PWA 로 보낸 작업은 보낸 시각이 붙는다. */
const chipTitle = (task: TbmTask, sentAt?: string) =>
  [
    `${task.from}–${task.to}`,
    taskPlace(task),
    `${task.people}명`,
    `반장 ${task.lead}`,
    sentAt && `보고서 보냄 ${sentAt}`,
  ]
    .filter(Boolean)
    .join(' · ');

function TaskChip({
  task,
  pressed,
  sentAt,
  onSelect,
}: {
  task: TbmTask;
  pressed: boolean;
  sentAt?: string;
  onSelect: () => void;
}) {
  const peak = task.status === 'DONE' ? null : taskPeak(task);
  const summary = task.status === 'DONE' ? '작업 종료' : peak ? SEVERITY_TEXT[peak] : '평가 없음';
  return (
    <button
      type="button"
      title={chipTitle(task, sentAt)}
      aria-label={`${task.id} ${task.name} · ${task.from}–${task.to} · ${summary}`}
      aria-pressed={pressed}
      onClick={onSelect}
      className={cn(
        CHIP,
        'bg-card ring-border hover:bg-surface-2 aria-pressed:bg-surface-3 aria-pressed:ring-2 aria-pressed:ring-primary',
      )}
    >
      {task.id} · {task.name} ·{' '}
      {task.status === 'IN_PROGRESS'
        ? '진행'
        : task.status === 'DONE'
          ? '종료'
          : task.status === 'PLANNED'
            ? '계획'
            : '상태 미확인'}
      {peak && <SeverityBadge severity={peak} />}
    </button>
  );
}

export function TaskList({
  tasks,
  selectedId,
  sentAt,
  onSelect,
  onAdd,
  children,
}: {
  tasks: readonly TbmTask[];
  selectedId: string;
  /** 작업마다 반장 PWA 로 보낸 시각. */
  sentAt: Record<string, string>;
  onSelect: (id: string) => void;
  onAdd: () => void;
  /** 알약 뒤에 이어 붙는 단추들. */
  children?: ReactNode;
}) {
  return (
    <>
      <div
        role="group"
        aria-label={`내일 작업 ${TBM_DAY} · ${tasks.length}건`}
        className="contents"
      >
        {tasks.map((t) => (
          <TaskChip
            key={t.id}
            task={t}
            pressed={t.id === selectedId}
            sentAt={sentAt[t.id]}
            onSelect={() => onSelect(t.id)}
          />
        ))}
      </div>
      <button
        type="button"
        aria-haspopup="dialog"
        onClick={onAdd}
        className={cn(CHIP, 'text-point-soft ring-faint hover:bg-surface-2')}
      >
        + 작업 등록
      </button>
      {children}
    </>
  );
}
