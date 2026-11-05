import { MAX_PEOPLE, MIN_PEOPLE } from '@/features/tbm/constants';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import {
  TASK_INPUT_DEFAULT,
  taskInputValid,
  taskToInput,
  type TaskInput,
} from '@/features/tbm/taskInput';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import { WORK_TYPE_OPTIONS, type WorkTypeOption } from '@/features/tbm/types';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import { Label } from '@simai/ui/components/label';
import { cn } from '@simai/ui/lib/utils';
import { useState, type ChangeEvent, type ComponentProps, type ReactNode } from 'react';

// 작업 등록·수정(수동 입력): 작업명 · 시작·끝 · 공종(work_type) · 인원. 날짜는 작업 관리에서 고른 날이다.
// 구역은 등록한 뒤 세팅 → 구역 단계에서 그리고, 가상 레이아웃은 3D 레이아웃 편집으로 만들어 그 작업에 잇는다.
// 수정할 때 폼에 없는 값(구역·반장·레이아웃)은 그대로 둔다. 모달 창(Esc · 바깥 누르기로 닫힘)에 띄운다.

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
  heading = '작업 등록 · 수동 입력',
  submitLabel = '등록',
  initial = TASK_INPUT_DEFAULT,
  workTypes = WORK_TYPE_OPTIONS,
  onSubmit,
  onCancel,
  onDelete,
}: {
  heading?: string;
  submitLabel?: string;
  initial?: TaskInput;
  workTypes?: readonly WorkTypeOption[];
  onSubmit: (input: TaskInput) => void;
  onCancel: () => void;
  onDelete?: () => void;
}) {
  const [input, setInput] = useState<TaskInput>(initial);
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
        if (ok) onSubmit(input);
      }}
    >
      <h2 className="text-[15px] font-bold text-muted-foreground">{heading}</h2>
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
        <SelectField
          label="공종"
          name="task-work-type"
          value={input.workType ?? ''}
          onChange={set('workType')}
        >
          <option value="">미지정</option>
          {workTypes.map((wt) => (
            <option key={wt.code} value={wt.code}>
              {wt.label} ({wt.code})
            </option>
          ))}
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
      {input.name.trim() && !ok && (
        <p className="text-[13px] text-muted-foreground">끝 시각이 시작보다 늦어야 합니다.</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!ok} className={cn(BUTTON, 'font-bold')}>
          {submitLabel}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel} className={BUTTON}>
          취소
        </Button>
        {onDelete && (
          <Button
            type="button"
            variant="destructive"
            onClick={onDelete}
            className={cn(BUTTON, 'ml-auto')}
          >
            작업 삭제
          </Button>
        )}
      </div>
    </form>
  );
}

/** 작업 등록 모달. 열 때마다 새로 그려 입력을 비운다. */
export function TaskAddDialog({
  workTypes = WORK_TYPE_OPTIONS,
  onAdd,
  onCancel,
}: {
  workTypes?: readonly WorkTypeOption[];
  onAdd: (input: TaskInput) => void;
  onCancel: () => void;
}) {
  return (
    <ModalDialog
      label="작업 등록"
      onClose={onCancel}
      closeOnBackdrop
      className="w-[min(520px,calc(100vw-48px))]"
    >
      <TaskForm
        heading="작업 등록 · 수동 입력"
        submitLabel="등록"
        workTypes={workTypes}
        onSubmit={onAdd}
        onCancel={onCancel}
      />
    </ModalDialog>
  );
}

/** 작업 수정 모달. 선택한 작업의 작업명·시간·공종·인원을 고친다(구역·반장·레이아웃은 그대로 둔다). */
export function TaskEditDialog({
  task,
  workTypes = WORK_TYPE_OPTIONS,
  onSave,
  onCancel,
  onDelete,
}: {
  task: TbmTask;
  workTypes?: readonly WorkTypeOption[];
  onSave: (input: TaskInput) => void;
  onCancel: () => void;
  onDelete?: () => void;
}) {
  return (
    <ModalDialog
      label="작업 수정"
      onClose={onCancel}
      closeOnBackdrop
      className="w-[min(520px,calc(100vw-48px))]"
    >
      <TaskForm
        heading={`${task.id} 작업 수정`}
        submitLabel="저장"
        initial={taskToInput(task)}
        workTypes={workTypes}
        onSubmit={onSave}
        onCancel={onCancel}
        onDelete={onDelete}
      />
    </ModalDialog>
  );
}
