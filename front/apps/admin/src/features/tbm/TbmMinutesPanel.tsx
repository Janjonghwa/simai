import { ACTION, H2, MINUTES_WIDTH, PANEL, PILL, SECONDARY } from '@/features/tbm/styles';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPreview } from '@/features/tbm/TbmDocument';
import { tbmFill, type TbmForm } from '@/features/tbm/tbmForm';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';

// 오른쪽 패널: 작업의 TBM 회의록을 A4 쪽으로 미리 보이고, 편집 · 되돌리기 · 출력 · 반장 PWA 로 보내기를 한다.

/** 받는 중에 흐리게 보이는 빈 양식. */
const EMPTY_FORM = tbmFill(null);

export function TbmMinutesPanel({
  task,
  form,
  receiving,
  canUndo,
  onEdit,
  onUndo,
  onPrint,
  onSend,
}: {
  task: TbmTask;
  /** 받은 내용 위에 고친 칸을 덮은 양식. */
  form: TbmForm;
  receiving: boolean;
  canUndo: boolean;
  onEdit: () => void;
  onUndo: () => void;
  onPrint: () => void;
  onSend: () => void;
}) {
  return (
    <Card
      role="region"
      aria-label={`${task.id} TBM 회의록`}
      className={cn(PANEL, 'min-h-0 max-w-[62%] min-w-[520px]', MINUTES_WIDTH)}
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
        <h2 className={H2}>TBM 회의록 · {task.id}</h2>
        {receiving && (
          <span role="status" className={PILL}>
            받는 중
          </span>
        )}
        <span className="ml-auto" />
        <Button
          variant="secondary"
          aria-haspopup="dialog"
          disabled={receiving}
          onClick={onEdit}
          className={SECONDARY}
        >
          편집
        </Button>
        <Button
          variant="secondary"
          disabled={receiving || !canUndo}
          onClick={onUndo}
          className={SECONDARY}
        >
          되돌리기
        </Button>
        <Button variant="secondary" disabled={receiving} onClick={onPrint} className={SECONDARY}>
          출력
        </Button>
        <Button disabled={receiving} onClick={onSend} className={cn(ACTION, 'font-bold')}>
          반장 {task.lead} PWA로 보내기
        </Button>
      </div>
      <TbmPreview
        key={task.id}
        form={receiving ? EMPTY_FORM : form}
        people={task.people}
        receiving={receiving}
      />
    </Card>
  );
}
