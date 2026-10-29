import { TASK_INPUT_DEFAULT, taskInputValid, type TaskInput } from '@/features/tbm/tbm';
import { TBM_LEADS, TBM_ZONES } from '@/features/tbm/tbmDemo';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { Input } from '@simai/ui/components/input';
import { Label } from '@simai/ui/components/label';
import { cn } from '@simai/ui/lib/utils';
import { useState, type ChangeEvent } from 'react';

// 작업 등록(수동 입력): 작업명 · 시작·끝 · 구역 · 인원 · 반장. 배치와 평가는 등록 뒤에 붙는다.

const FIELD = 'flex-col items-stretch gap-1.5 text-xs font-normal text-muted-foreground';
const CONTROL =
  'h-11 rounded-lg border-border bg-surface-2 px-3 text-[15px] text-foreground md:text-[15px] dark:bg-surface-2';
const SELECT = cn(
  CONTROL,
  'w-full min-w-0 border outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
);

export function TaskForm({
  onAdd,
  onCancel,
}: {
  onAdd: (input: TaskInput) => void;
  onCancel: () => void;
}) {
  const [input, setInput] = useState<TaskInput>(TASK_INPUT_DEFAULT);
  const set = (key: keyof TaskInput) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setInput({ ...input, [key]: e.target.value });
  const ok = taskInputValid(input);
  return (
    <Card className="max-w-[520px] gap-3 px-5 py-[18px]">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (ok) onAdd(input);
        }}
      >
        <h2 className="text-[15px] font-bold text-muted-foreground">작업 등록 · 수동 입력</h2>
        <Label className={FIELD}>
          작업명
          <Input
            name="task-name"
            value={input.name}
            onChange={set('name')}
            placeholder="예: 전기 배선"
            className={CONTROL}
          />
        </Label>
        <div className="grid grid-cols-2 gap-2.5">
          <Label className={FIELD}>
            시작
            <Input
              name="task-from"
              type="time"
              value={input.from}
              onChange={set('from')}
              className={CONTROL}
            />
          </Label>
          <Label className={FIELD}>
            끝
            <Input
              name="task-to"
              type="time"
              value={input.to}
              onChange={set('to')}
              className={CONTROL}
            />
          </Label>
        </div>
        <div className="grid grid-cols-2 gap-2.5">
          <Label className={FIELD}>
            구역
            <select name="task-zone" value={input.zone} onChange={set('zone')} className={SELECT}>
              {TBM_ZONES.map((zone) => (
                <option key={zone.id} value={zone.id}>
                  {zone.id} {zone.name}
                </option>
              ))}
              <option value="">구역 밖</option>
            </select>
          </Label>
          <Label className={FIELD}>
            인원
            <Input
              name="task-people"
              type="number"
              min={1}
              max={10}
              value={input.people}
              onChange={set('people')}
              className={CONTROL}
            />
          </Label>
        </div>
        <Label className={FIELD}>
          반장
          <select name="task-lead" value={input.lead} onChange={set('lead')} className={SELECT}>
            {TBM_LEADS.map((lead) => (
              <option key={lead}>{lead}</option>
            ))}
          </select>
        </Label>
        {input.name.trim() && !ok && (
          <p className="text-[13px] text-muted-foreground">끝 시각이 시작보다 늦어야 합니다.</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={!ok} className="h-11 px-[18px] font-bold">
            등록
          </Button>
          <Button type="button" variant="secondary" onClick={onCancel} className="h-11 px-[18px]">
            취소
          </Button>
        </div>
      </form>
    </Card>
  );
}
