import { PageTitle } from '@/features/layout/PageTitle';
import { PILL, SWITCH } from '@/features/tbm/styles';
import { TaskAddDialog, TaskEditDialog } from '@/features/tbm/TaskForm';
import { createTask, type TaskInput } from '@/features/tbm/taskInput';
import { TaskList } from '@/features/tbm/TaskList';
import { clockText, tbmInterferences } from '@/features/tbm/tbm';
import { TbmPrint } from '@/features/tbm/TbmDocument';
import { TbmEditDialog } from '@/features/tbm/TbmEditDialog';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import { TbmLeftPanel } from '@/features/tbm/TbmLeftPanel';
import { TbmMinutesPanel } from '@/features/tbm/TbmMinutesPanel';
import { tbmFailureReasonText, workTypeLabelOf } from '@/features/tbm/types';
import { useMinutesForm } from '@/features/tbm/useMinutesForm';
import { usePrint } from '@/features/tbm/usePrint';
import { useReceiving } from '@/features/tbm/useReceiving';
import { useTbmData } from '@/features/tbm/useTbmData';
import { useTbmReport } from '@/features/tbm/useTbmReport';
import { createQueryClient } from '@/lib/queryClient';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { QueryClientContext, QueryClientProvider } from '@tanstack/react-query';
import { useContext, useMemo, useState } from 'react';

// 레이아웃 · TBM 보고서(계획 모드). 작업마다 가상 레이아웃 · 위험성 평가 · 법령 지식 그래프 · 동시 작업 간섭을 보이고
// TBM 회의록을 채워 반장에게 보낸다. 서버 API 실패(네트워크 끊김·5xx) 때만 tbmDemo 로 대체 화면을 그린다.

function TbmPageContent() {
  const { tasks, workTypes, layouts, isFallback, addTask, editTask } = useTbmData();
  const [selected, setSelected] = useState(tasks[0]?.id ?? 'T1');
  const [adding, setAdding] = useState(false);
  const [editingTask, setEditingTask] = useState(false);
  const [editing, setEditing] = useState(false);
  /** 왼쪽 패널을 레이아웃 대신 평가 · 간섭으로 바꿔 보는지(기본은 레이아웃). */
  const [detail, setDetail] = useState(false);
  /** 작업마다 반장 PWA 로 보낸 시각. */
  const [sentAt, setSentAt] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');

  const task = tasks.find((t) => t.id === selected) ?? tasks[0]!;
  const workId = task.workId ?? task.id.replace(/^T/, '');
  const {
    report,
    versions,
    selectedReportId,
    isGenerating,
    requestError,
    selectVersion,
    generate,
    clearError,
  } = useTbmReport(workId, !isFallback);

  const interferences = useMemo(() => tbmInterferences(tasks), [tasks]);
  const { receiving, restart } = useReceiving(task.id);
  const { edits, filled, canUndo, save, undo } = useMinutesForm(task, report);
  const { printing, items, print } = usePrint(tasks, task, edits, report);

  const select = (id: string) => {
    if (id !== task.id) {
      setSelected(id);
      restart();
    }
    setEditing(false);
    setEditingTask(false);
    setAdding(false);
    setMessage('');
    clearError();
  };

  const openAdd = () => {
    setAdding(true);
    setEditingTask(false);
    setMessage('');
  };

  const add = (input: TaskInput) => {
    const optimistic = createTask(tasks, input);
    select(optimistic.id);
    setMessage(
      `${optimistic.id} 등록됨 · ${
        optimistic.layoutId ? `가상 레이아웃 #${optimistic.layoutId} 연결` : '배치는 아직 없음'
      }`,
    );
    void addTask(input).then((created) => {
      if (created.id !== optimistic.id) {
        setSelected(created.id);
      }
    });
  };

  const saveTaskEdit = (input: TaskInput) => {
    setEditingTask(false);
    void editTask(task, input).then((updated) => {
      const wtLabel = workTypeLabelOf(updated.workType, workTypes);
      setMessage(
        `${updated.id} 수정됨${wtLabel ? ` · 공종 ${wtLabel}` : ''}${
          updated.layoutId ? ` · 레이아웃 #${updated.layoutId}` : ' · 레이아웃 미연결'
        }`,
      );
    });
  };

  const handleGenerate = () => {
    setMessage('');
    void generate();
  };

  const send = () => {
    setSentAt({ ...sentAt, [task.id]: clockText(new Date()) });
    setMessage(`${task.id} 회의록을 반장 ${task.lead} PWA로 보냄`);
  };

  const closeEditor = () => setEditing(false);

  const reportButtonLabel = isGenerating
    ? '보고서 생성 중…'
    : report
      ? '보고서 재생성'
      : '보고서 생성';

  return (
    <>
      <PageTitle>레이아웃 · TBM 보고서</PageTitle>
      <div className="flex flex-none flex-wrap items-center gap-2.5">
        <TaskList
          tasks={tasks}
          selectedId={task.id}
          sentAt={sentAt}
          onSelect={select}
          onAdd={openAdd}
        >
          <Button
            type="button"
            variant="secondary"
            size="sm"
            aria-haspopup="dialog"
            onClick={() => {
              setEditingTask(true);
              setAdding(false);
            }}
            className={SWITCH}
          >
            작업 수정
          </Button>
          {isFallback && (
            <span
              role="status"
              data-testid="tbm-demo-badge"
              className={cn(PILL, 'bg-amber-500/20 font-semibold text-amber-300 ring-amber-400/50')}
            >
              데모 데이터
            </span>
          )}
          <span className={cn(PILL, 'ml-auto')}>계획 모드</span>
          {versions.length > 0 && (
            <select
              aria-label="보고서 버전"
              value={selectedReportId ?? versions[0]!.report_id}
              onChange={(e) => selectVersion(Number(e.target.value))}
              className="h-9 rounded-lg border border-border bg-surface-2 px-2.5 text-xs font-semibold text-foreground outline-none"
            >
              {versions.map((v) => (
                <option key={v.report_id} value={v.report_id}>
                  v{v.report_version} ({v.status})
                </option>
              ))}
            </select>
          )}
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={handleGenerate}
            className={cn(SWITCH, isGenerating && 'animate-pulse')}
          >
            {reportButtonLabel}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => print(true, closeEditor)}
            className={SWITCH}
          >
            하루치 묶음 PDF
          </Button>
        </TaskList>
      </div>
      <p role="status" className="text-[13px] text-point-soft empty:hidden">
        {message}
      </p>
      {requestError && (
        <div
          role="alert"
          data-testid="tbm-request-error"
          className="rounded-lg bg-destructive/15 px-3.5 py-2 text-[13px] text-destructive ring-1 ring-destructive/40 ring-inset"
        >
          {requestError}
        </div>
      )}
      {report?.status === 'FAILED' && (
        <div
          role="alert"
          data-testid="tbm-report-failed"
          className="rounded-lg bg-destructive/15 px-3.5 py-2 text-[13px] text-destructive ring-1 ring-destructive/40 ring-inset"
        >
          {tbmFailureReasonText(report.failure_reason)}
        </div>
      )}
      <div className="[container-type:size] flex min-h-[560px] flex-1 gap-3.5">
        <TbmLeftPanel
          task={task}
          tasks={tasks}
          interferences={interferences}
          detail={detail}
          report={report}
          onSwitch={() => setDetail(!detail)}
        />
        <TbmMinutesPanel
          task={task}
          form={filled}
          receiving={receiving}
          canUndo={canUndo}
          onEdit={() => setEditing(true)}
          onUndo={undo}
          onPrint={() => print(false, closeEditor)}
          onSend={send}
        />
      </div>
      {adding && (
        <TaskAddDialog
          workTypes={workTypes}
          layouts={layouts}
          onAdd={add}
          onCancel={() => setAdding(false)}
        />
      )}
      {editingTask && (
        <TaskEditDialog
          task={task}
          workTypes={workTypes}
          layouts={layouts}
          onSave={saveTaskEdit}
          onCancel={() => setEditingTask(false)}
        />
      )}
      {editing && (
        <TbmEditDialog onClose={closeEditor} onUndo={undo} canUndo={canUndo}>
          <TbmFormSheet form={filled} editable onSave={save} people={task.people} />
        </TbmEditDialog>
      )}
      {printing && <TbmPrint items={items} />}
    </>
  );
}

export function TbmPage() {
  const existingClient = useContext(QueryClientContext);
  const [fallbackClient] = useState(() => createQueryClient());
  if (existingClient) {
    return <TbmPageContent />;
  }
  return (
    <QueryClientProvider client={fallbackClient}>
      <TbmPageContent />
    </QueryClientProvider>
  );
}
