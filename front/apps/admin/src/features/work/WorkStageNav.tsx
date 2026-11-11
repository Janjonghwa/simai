import { stageIndexOf, WORK_STAGES, type WorkStageId } from '@/features/work/workDay';
import { cn } from '@simai/ui/lib/utils';
import { Check } from 'lucide-react';

// 날짜 화면 머리줄 오른쪽의 단계 줄: 번호 + 이름 네 칸. 표시만 한다(눌러도 이동하지 않는다).
// 지난 단계는 초록 ✓, 지금 단계는 굵게, 남은 단계는 흐리게 보인다.

export function WorkStageNav({ stage }: { stage: WorkStageId }) {
  const current = stageIndexOf(stage);
  return (
    <nav aria-label="작업 단계" className="flex min-w-0 flex-1 justify-end">
      <ol className="flex gap-2">
        {WORK_STAGES.map(({ id, label }, index) => {
          const state = index < current ? 'done' : index === current ? 'current' : 'todo';
          return (
            <li
              key={id}
              data-state={state}
              aria-current={state === 'current' ? 'step' : undefined}
              className={cn(
                'flex items-center gap-1.5 px-1.5 py-1 text-sm whitespace-nowrap',
                state === 'todo' ? 'text-muted-foreground' : 'text-foreground',
              )}
            >
              {state === 'done' ? (
                <Check aria-hidden className="size-4 text-ok" />
              ) : (
                <span className={cn('tabular-nums', state === 'current' && 'text-point-soft')}>
                  {index + 1}
                </span>
              )}
              <span className={cn(state === 'current' && 'font-bold')}>{label}</span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
