import { MAX_PEOPLE, MIN_PEOPLE, OUTSIDE_ZONE_TEXT } from '@/features/tbm/constants';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { TASK_INPUT_DEFAULT, taskInputValid, type TaskInput } from '@/features/tbm/taskInput';
import { TBM_LEADS, TBM_ZONES } from '@/features/tbm/tbmDemo';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import { Label } from '@simai/ui/components/label';
import { cn } from '@simai/ui/lib/utils';
import { useState, type ChangeEvent, type ComponentProps, type ReactNode } from 'react';

// 작업 등록(수동 입력): 작업명 · 시작·끝 · 구역 · 인원 · 반장. 배치와 평가는 등록 뒤에 붙는다.
// 모달 창(Esc · 바깥 누르기로 닫힘)에 띄운다.

const FIELD = 'flex-col items-stretch gap-1.5 text-xs font-normal text-muted-foreground';
const CONTROL =
  'h-11 rounded-lg border-border bg-surface-2 px-3 text-[15px] text-foreground md:text-[15px] dark:bg-surface-2';
const SELECT = cn(
  CONTROL,
  'w-full min-w-0 border outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
);
const BUTTON = 'h-11 px-[18px]';

/** 이름표(label)가 붙은 입력 칸. */
function InputField({ label, ...props }: { label: string } & ComponentProps<typeof Input>) {
  return (
    <Label className={FIELD}>
      {label}
      <Input {...props} className={CONTROL} />
    </Label>
  );
}

/** 이름표(label)가 붙은 고르기 칸. */
function SelectField({
  label,
  children,
  ...props
}: { label: string; children: ReactNode } & ComponentProps<'select'>) {
  return (
    <Label className={FIELD}>
      {label}
      <select {...props} className={SELECT}>
        {children}
      </select>
    </Label>
  );
}

function TaskForm({
  onAdd,
  onCancel,
}: {
  onAdd: (input: TaskInput) => void;
  onCancel: () => void;
}) {
  const [input, setInput] = useState<TaskInput>(TASK_INPUT_DEFAULT);
  const set = (key: keyof TaskInput) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { value } = e.target;
    setInput((prev) => ({ ...prev, [key]: value }));
  };
  const ok = taskInputValid(input);
  return (
    <form
      className="flex flex-col gap-3 px-5 py-[18px]"
      onSubmit={(e) => {
        e.preventDefault();
        if (ok) onAdd(input);
      }}
    >
      <h2 className="text-[15px] font-bold text-muted-foreground">작업 등록 · 수동 입력</h2>
      <InputField
        label="작업명"
        name="task-name"
        value={input.name}
        onChange={set('name')}
        placeholder="예: 전기 배선"
      />
      <div className="grid grid-cols-2 gap-2.5">
        <InputField
          label="시작"
          name="task-from"
          type="time"
          value={input.from}
          onChange={set('from')}
        />
        <InputField label="끝" name="task-to" type="time" value={input.to} onChange={set('to')} />
      </div>
      <div className="grid grid-cols-2 gap-2.5">
        <SelectField label="구역" name="task-zone" value={input.zone} onChange={set('zone')}>
          {TBM_ZONES.map((zone) => (
            <option key={zone.id} value={zone.id}>
              {zone.id} {zone.name}
            </option>
          ))}
          <option value="">{OUTSIDE_ZONE_TEXT}</option>
        </SelectField>
        <InputField
          label="인원"
          name="task-people"
          type="number"
          min={MIN_PEOPLE}
          max={MAX_PEOPLE}
          value={input.people}
          onChange={set('people')}
        />
      </div>
      <SelectField label="반장" name="task-lead" value={input.lead} onChange={set('lead')}>
        {TBM_LEADS.map((lead) => (
          <option key={lead}>{lead}</option>
        ))}
      </SelectField>
      {input.name.trim() && !ok && (
        <p className="text-[13px] text-muted-foreground">끝 시각이 시작보다 늦어야 합니다.</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={!ok} className={cn(BUTTON, 'font-bold')}>
          등록
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel} className={BUTTON}>
          취소
        </Button>
      </div>
    </form>
  );
}

/** 작업 등록 모달. 열 때마다 새로 그려 입력을 비운다. */
export function TaskAddDialog({
  onAdd,
  onCancel,
}: {
  onAdd: (input: TaskInput) => void;
  onCancel: () => void;
}) {
  return (
    <ModalDialog
      label="작업 등록"
      onClose={onCancel}
      closeOnBackdrop
      className="w-[min(480px,calc(100vw-48px))]"
    >
      <TaskForm onAdd={onAdd} onCancel={onCancel} />
    </ModalDialog>
  );
}
