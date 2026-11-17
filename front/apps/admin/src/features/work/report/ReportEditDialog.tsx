import { ModalDialog } from '@/features/tbm/ModalDialog';
import { ACTION, ALERT, H2, SECONDARY } from '@/features/tbm/styles';
import { ReportFieldsEditor } from '@/features/work/report/ReportFieldsEditor';
import { TaskChecklist } from '@/features/work/report/TaskChecklist';
import { useReportEdit } from '@/features/work/report/useReportEdit';
import {
  applyEdits,
  fieldEditsOf,
  isDrafted,
  missingKeys,
  reportFormOf,
  selectionOfTasks,
} from '@/features/work/reportForm';
import type { ReportForm, WorkReport, WorkTask } from '@/features/work/types';
import { useWorkTasks } from '@/features/work/useWorkDays';
import { Button } from '@simai/ui/components/button';
import { useState } from 'react';

// TBM 보고서 편집 창. 초안 전에는 작업내용만, 초안 후에는 넓게 모든 칸(정보·위험·중점·점검·일일 안전점검·종료 미팅)을 고친다.
// 저장은 받아 둔 판의 form_revision 으로 보내고, 충돌·잠김 처리는 useReportEdit 이 한다.
// 화면에 보이는 양식이 서버 양식과 다르면(시연 계정의 미리 적어 둔 초안) initialForm 으로 그 양식에서 시작한다.

const TITLE = 'TBM 보고서 편집';
const NARROW = 'w-[min(480px,calc(100vw-48px))]';
const WIDE = 'h-[90vh] w-[min(920px,calc(100vw-48px))] overflow-hidden open:flex open:flex-col';
const NO_TASKS: readonly WorkTask[] = [];

export interface ReportEditDialogProps {
  /** 창을 여는 때의 보고서(최신 판). 이후 바뀌어도 창은 이 판을 기준으로 고친다. */
  report: WorkReport;
  /** 저장 단추 글. 기본 '저장'. */
  submitLabel?: string;
  /** true 면 작업내용이 비었을 때 저장하지 않고 빨간 윤곽으로 알린다('다음' 에서 연 창). */
  requireTasks?: boolean;
  /**
   * 창을 처음 열 때 보일 양식(기본은 `report` 의 양식). 있으면 초안이 끝난 것처럼 넓게 모든 칸을 보인다.
   * 저장 충돌로 보고서를 다시 받으면 쓰지 않고 받은 서버 양식으로 채운다.
   */
  initialForm?: ReportForm;
  onClose: () => void;
  /** 저장이 끝난 뒤(창을 닫기 전). */
  onSaved?: () => void;
}

export function ReportEditDialog({
  report,
  submitLabel = '저장',
  requireTasks = false,
  initialForm,
  onClose,
  onSaved,
}: ReportEditDialogProps) {
  const edit = useReportEdit(report, { onSaved, onClose });
  const catalog = useWorkTasks().data ?? NO_TASKS;
  const full = initialForm !== undefined || isDrafted(edit.base);
  return (
    <ModalDialog label={TITLE} onClose={onClose} className={full ? WIDE : NARROW}>
      <EditForm
        key={edit.generation}
        report={edit.base}
        initialForm={edit.generation === 0 ? initialForm : undefined}
        full={full}
        catalog={catalog}
        locked={edit.locked}
        pending={edit.pending}
        notice={edit.notice}
        requireTasks={requireTasks}
        submitLabel={submitLabel}
        onSubmit={edit.submit}
        onCancel={onClose}
      />
    </ModalDialog>
  );
}

/** 입력 상태를 가진 창 안쪽. `key` 가 바뀌면(보고서를 다시 받으면) 처음부터 다시 만든다. */
function EditForm({
  report,
  initialForm,
  full,
  catalog,
  locked,
  pending,
  notice,
  requireTasks,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  report: WorkReport;
  initialForm: ReportForm | undefined;
  full: boolean;
  catalog: readonly WorkTask[];
  locked: boolean;
  pending: boolean;
  notice: string | null;
  requireTasks: boolean;
  submitLabel: string;
  onSubmit: (form: ReportForm) => Promise<void>;
  onCancel: () => void;
}) {
  const form = initialForm ?? reportFormOf(report);
  const [tasks, setTasks] = useState(() => selectionOfTasks(form.info.tasks));
  const [fields, setFields] = useState(() => (full ? fieldEditsOf(form) : undefined));
  const [invalid, setInvalid] = useState(false);

  const submit = () => {
    const next = applyEdits(form, { tasks, fields }, catalog);
    if (requireTasks && missingKeys(next).length > 0) {
      setInvalid(true);
      return;
    }
    void onSubmit(next);
  };

  const checklist = (
    <TaskChecklist
      catalog={catalog}
      selection={tasks}
      invalid={invalid}
      onChange={(next) => {
        setTasks(next);
        setInvalid(false);
      }}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex flex-none items-center border-b border-border px-5 py-3">
        <h2 className={H2}>{TITLE}</h2>
      </header>
      <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
        <fieldset disabled={locked} className="min-w-0">
          {fields ? (
            <ReportFieldsEditor fields={fields} onChange={setFields} tasks={checklist} />
          ) : (
            checklist
          )}
        </fieldset>
      </div>
      <footer className="flex flex-none flex-col gap-2.5 border-t border-border px-5 py-3">
        {notice && (
          <p role="alert" className={ALERT}>
            {notice}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} className={SECONDARY}>
            {locked ? '닫기' : '취소'}
          </Button>
          {!locked && (
            <Button onClick={submit} disabled={pending} className={ACTION}>
              {submitLabel}
            </Button>
          )}
        </div>
      </footer>
    </div>
  );
}
