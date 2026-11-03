import { MAX_PEOPLE, MIN_PEOPLE, OUTSIDE_ZONE_TEXT } from '@/features/tbm/constants';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import {
  TASK_INPUT_DEFAULT,
  taskInputValid,
  taskToInput,
  type TaskInput,
} from '@/features/tbm/taskInput';
import { TBM_LEADS, TBM_ZONES, type TbmTask } from '@/features/tbm/tbmDemo';
import { WORK_TYPE_OPTIONS, type LayoutSummary, type WorkTypeOption } from '@/features/tbm/types';
import { useZones } from '@/features/zones/useZones';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import { Label } from '@simai/ui/components/label';
import { cn } from '@simai/ui/lib/utils';
import { useMemo, useState, type ChangeEvent, type ComponentProps, type ReactNode } from 'react';

/** 고르기 칸에 보일 구역(id·이름). 서버 모드는 /api/zones, 데모 폴백은 tbmDemo 구역. */
export interface TaskZoneOption {
  id: string;
  name: string;
}

const DEMO_ZONE_OPTIONS: readonly TaskZoneOption[] = TBM_ZONES.map((z) => ({
  id: z.id,
  name: z.name,
}));

/** 서버 모드면 /api/zones 의 구역(데모 구역으로 등록하면 서버가 "없는 구역"으로 거부한다), 폴백이면 데모 구역. */
function useTaskZoneOptions(server: boolean): readonly TaskZoneOption[] {
  const { zones } = useZones();
  return useMemo(
    () => (server ? zones.map((z) => ({ id: z.zoneId, name: z.name })) : DEMO_ZONE_OPTIONS),
    [server, zones],
  );
}

// 작업 등록·수정(수동 입력): 작업명 · 시작·끝 · 구역 · 인원 · 반장 · 공종(work_type) · 가상 레이아웃(layout_id).
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
  heading = '작업 등록 · 수동 입력',
  submitLabel = '등록',
  initial = TASK_INPUT_DEFAULT,
  workTypes = WORK_TYPE_OPTIONS,
  layouts = [],
  zones = DEMO_ZONE_OPTIONS,
  onSubmit,
  onCancel,
  onDelete,
  onOpenLayoutEditor,
}: {
  heading?: string;
  submitLabel?: string;
  initial?: TaskInput;
  workTypes?: readonly WorkTypeOption[];
  layouts?: readonly LayoutSummary[];
  zones?: readonly TaskZoneOption[];
  onSubmit: (input: TaskInput) => void;
  onCancel: () => void;
  onDelete?: () => void;
  onOpenLayoutEditor?: (currentLayoutId: number | null) => void;
}) {
  // 처음 값의 구역이 목록에 없으면(예: 데모 기본값) 첫 구역, 구역이 없으면 구역 밖으로 둔다.
  const [input, setInput] = useState<TaskInput>(() =>
    initial.zone === '' || zones.some((z) => z.id === initial.zone)
      ? initial
      : { ...initial, zone: zones[0]?.id ?? '' },
  );
  const set = (key: keyof TaskInput) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { value } = e.target;
    setInput((prev) => ({ ...prev, [key]: value }));
  };
  const ok = taskInputValid(input);
  const parsedLayoutId =
    input.layoutId && input.layoutId.trim() !== '' ? Number(input.layoutId) : null;
  const currentLayoutId =
    parsedLayoutId !== null && Number.isFinite(parsedLayoutId) ? parsedLayoutId : null;

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
        <SelectField label="구역" name="task-zone" value={input.zone} onChange={set('zone')}>
          {zones.map((zone) => (
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
      <div className="grid grid-cols-2 gap-2.5">
        <SelectField label="반장" name="task-lead" value={input.lead} onChange={set('lead')}>
          {TBM_LEADS.map((lead) => (
            <option key={lead}>{lead}</option>
          ))}
        </SelectField>
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
      </div>
      <div className="flex flex-col gap-1.5">
        <SelectField
          label="가상 레이아웃"
          name="task-layout"
          value={input.layoutId ?? ''}
          onChange={set('layoutId')}
        >
          <option value="">연결 안 함</option>
          {layouts.map((layout) => (
            <option key={layout.layout_id} value={String(layout.layout_id)}>
              #{layout.layout_id} {layout.name} (rev.{layout.revision})
            </option>
          ))}
        </SelectField>
        {onOpenLayoutEditor && (
          <div className="flex justify-end">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => onOpenLayoutEditor(currentLayoutId)}
              className="h-8 px-3 text-xs font-semibold"
            >
              {currentLayoutId !== null ? '3D 레이아웃 편집' : '3D로 새 레이아웃 만들기'}
            </Button>
          </div>
        )}
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
  layouts = [],
  serverZones = false,
  onAdd,
  onCancel,
  onOpenLayoutEditor,
}: {
  workTypes?: readonly WorkTypeOption[];
  layouts?: readonly LayoutSummary[];
  serverZones?: boolean;
  onAdd: (input: TaskInput) => void;
  onCancel: () => void;
  onOpenLayoutEditor?: (currentLayoutId: number | null) => void;
}) {
  const zoneOptions = useTaskZoneOptions(serverZones);
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
        layouts={layouts}
        zones={zoneOptions}
        onSubmit={onAdd}
        onCancel={onCancel}
        onOpenLayoutEditor={onOpenLayoutEditor}
      />
    </ModalDialog>
  );
}

/** 작업 수정 모달. 선택한 작업의 공종·가상 레이아웃·시간·구역·인원 등을 수정한다. */
export function TaskEditDialog({
  task,
  workTypes = WORK_TYPE_OPTIONS,
  layouts = [],
  serverZones = false,
  onSave,
  onCancel,
  onDelete,
  onOpenLayoutEditor,
}: {
  task: TbmTask;
  workTypes?: readonly WorkTypeOption[];
  layouts?: readonly LayoutSummary[];
  serverZones?: boolean;
  onSave: (input: TaskInput) => void;
  onCancel: () => void;
  onDelete?: () => void;
  onOpenLayoutEditor?: (currentLayoutId: number | null) => void;
}) {
  const zoneOptions = useTaskZoneOptions(serverZones);
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
        layouts={layouts}
        zones={zoneOptions}
        onSubmit={onSave}
        onCancel={onCancel}
        onDelete={onDelete}
        onOpenLayoutEditor={onOpenLayoutEditor}
      />
    </ModalDialog>
  );
}
