import { H2, NOTE, PANEL, SWITCH } from '@/features/tbm/styles';
import { interferencesOf, overlapsTime, type Interference } from '@/features/tbm/tbm';
import { TbmAssessment } from '@/features/tbm/TbmAssessment';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPlan } from '@/features/tbm/TbmPlan';
import type { TbmReport } from '@/features/tbm/types';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';

// 왼쪽 블록: 기본은 가상 레이아웃을 크게 보이고, 오른쪽 위 단추 하나로 평가 · 간섭 보기와 바꾼다(토글이 아니라 화면 전환).
// 머리에 3D 레이아웃 편집 단추를 둔다.

export function TbmLeftPanel({
  task,
  tasks,
  interferences,
  detail,
  report,
  onSwitch,
  onEditLayout,
}: {
  task: TbmTask;
  /** 같은 날 작업. */
  tasks: readonly TbmTask[];
  /** 같은 날 작업의 동시 작업 간섭. */
  interferences: readonly Interference[];
  /** 레이아웃 대신 평가 · 간섭을 보이는지. */
  detail: boolean;
  report?: TbmReport | null;
  onSwitch: () => void;
  onEditLayout: () => void;
}) {
  const mine = interferencesOf(interferences, task.id);
  const title = detail ? '평가 · 간섭' : '가상 레이아웃';
  return (
    <Card role="region" aria-label={`${task.id} ${title}`} className={cn(PANEL, 'min-h-0 min-w-0')}>
      <div className="flex flex-none items-center gap-2">
        <h2 className={cn(H2, 'whitespace-nowrap')}>{title}</h2>
        <span className="ml-auto" />
        <Button
          variant="secondary"
          size="sm"
          aria-haspopup="dialog"
          onClick={onEditLayout}
          className={SWITCH}
        >
          3D 레이아웃 편집
        </Button>
        <Button variant="secondary" size="sm" onClick={onSwitch} className={SWITCH}>
          {detail ? '레이아웃 보기' : '평가 · 간섭 보기'}
        </Button>
      </div>
      {detail ? (
        <TbmAssessment task={task} tasks={tasks} mine={mine} report={report} />
      ) : (
        <div className="relative min-h-0 flex-1 rounded-xl bg-background ring-1 ring-faint ring-inset">
          <div className="absolute inset-3.5">
            <TbmPlan
              task={task}
              others={tasks.filter((t) => t.id !== task.id && overlapsTime(t, task))}
              spots={mine.flatMap((x) => (x.spot ? [x.spot] : []))}
            />
          </div>
          {!task.items.length && (
            <p className={cn(NOTE, 'absolute bottom-3 left-4')}>배치 항목 없음</p>
          )}
        </div>
      )}
    </Card>
  );
}
