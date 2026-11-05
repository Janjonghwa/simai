import { H2, PANEL, SECONDARY } from '@/features/tbm/styles';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPreview } from '@/features/tbm/TbmDocument';
import type { TbmForm } from '@/features/tbm/tbmForm';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';

// 오른쪽 블록: 작업의 TBM 회의록을 A4 쪽으로 미리 보이고, 편집 · 되돌리기 · 출력을 한다.
// 보고서가 없으면(blank) 작업 정보만 넣은 양식을 흐리게 보이고 단추는 막는다.

export function TbmMinutesPanel({
  task,
  form,
  blank,
  version,
  canUndo,
  onEdit,
  onUndo,
  onPrint,
}: {
  task: TbmTask;
  /** 받은 내용 위에 고친 칸을 덮은 양식. */
  form: TbmForm;
  /** 아직 보고서가 없다. */
  blank: boolean;
  /** 보이는 보고서 판 번호. */
  version?: number;
  canUndo: boolean;
  onEdit: () => void;
  onUndo: () => void;
  onPrint: () => void;
}) {
  return (
    <Card
      role="region"
      aria-label={`${task.id} TBM 회의록`}
      className={cn(PANEL, 'min-h-0 min-w-0')}
    >
      <div className="flex flex-none items-center gap-2">
        <h2 className={cn(H2, 'whitespace-nowrap')}>
          TBM 회의록{version !== undefined ? ` · v${version}` : ''}
        </h2>
        <span className="ml-auto" />
        <Button
          variant="secondary"
          aria-haspopup="dialog"
          disabled={blank}
          onClick={onEdit}
          className={SECONDARY}
        >
          편집
        </Button>
        <Button
          variant="secondary"
          disabled={blank || !canUndo}
          onClick={onUndo}
          className={SECONDARY}
        >
          되돌리기
        </Button>
        <Button variant="secondary" disabled={blank} onClick={onPrint} className={SECONDARY}>
          출력
        </Button>
      </div>
      <TbmPreview key={task.id} form={form} people={task.people} receiving={blank} />
    </Card>
  );
}
