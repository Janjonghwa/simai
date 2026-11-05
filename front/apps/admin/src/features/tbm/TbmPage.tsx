import { PageTitle } from '@/features/layout/PageTitle';
import { setSetupWorkUnitId } from '@/features/setup/setup.store';
import { WRAP_UP_GENERATING_TEXT } from '@/features/tbm/constants';
import { SignaturePanel, useSignatures } from '@/features/tbm/SignaturePanel';
import { PILL, SWITCH } from '@/features/tbm/styles';
import { TaskAddDialog, TaskEditDialog } from '@/features/tbm/TaskForm';
import { createTask, taskToInput, type TaskInput } from '@/features/tbm/taskInput';
import { TaskList } from '@/features/tbm/TaskList';
import { clockText, tbmInterferences } from '@/features/tbm/tbm';
import { TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPrint } from '@/features/tbm/TbmDocument';
import { TbmEditDialog } from '@/features/tbm/TbmEditDialog';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import { TbmLayoutEditorDialog } from '@/features/tbm/TbmLayoutEditorDialog';
import { TbmLeftPanel } from '@/features/tbm/TbmLeftPanel';
import { TbmMinutesPanel } from '@/features/tbm/TbmMinutesPanel';
import {
  displayTaskId,
  tbmFailureReasonText,
  workTypeLabelOf,
  type LayoutDetail,
} from '@/features/tbm/types';
import { useMinutesForm } from '@/features/tbm/useMinutesForm';
import { usePrint } from '@/features/tbm/usePrint';
import { useReceiving } from '@/features/tbm/useReceiving';
import { useSetupLookup } from '@/features/tbm/useSetupLookup';
import { useTbmData } from '@/features/tbm/useTbmData';
import { useTbmReport } from '@/features/tbm/useTbmReport';
import { useWorkSession } from '@/features/tbm/useWorkSession';
import { confirmWorkEnd, confirmWorkStart } from '@/features/tbm/workConfirm';
import { deriveWorkStages, unfinishedBeforeStart } from '@/features/tbm/workStage';
import { WorkStageBar } from '@/features/tbm/WorkStageBar';
import { createQueryClient } from '@/lib/queryClient';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { QueryClientContext, QueryClientProvider } from '@tanstack/react-query';
import { useContext, useEffect, useMemo, useRef, useState } from 'react';

const EMPTY_PLACEHOLDER_TASK: TbmTask = TBM_TASKS[0]!;

// 작업 관리(/work). 작업마다 단계(세팅 → 구역 → TBM 보고서 → 시작 → 종료 → 마무리·서명)를 보이고, 가상 레이아웃 · 위험성 평가 ·
// 법령 지식 그래프 · 동시 작업 간섭을 보이며 TBM 회의록을 채워 반장에게 보낸다. 예전 이름은 레이아웃 · TBM 보고서(계획 모드). 작업마다 가상 레이아웃 · 위험성 평가 · 법령 지식 그래프 · 동시 작업 간섭을 보이고
// TBM 회의록을 채워 반장에게 보낸다. 서버 API 실패(네트워크 끊김·5xx) 때만 tbmDemo 로 대체 화면을 그린다.

export interface TbmPageProps {
  /** 처음 선택할 작업 id(세팅 화면에서 돌아올 때 /work?workUnitId=). */
  initialWorkId?: string;
  /** 이 작업의 현장 세팅 화면으로 간다(/setup?workUnitId=). */
  onOpenSetup?: (workUnitId: number) => void;
  /** 구역 설정(관제 화면 구역 편집)으로 간다. */
  onOpenZones?: () => void;
}

function TbmPageContent({ initialWorkId, onOpenSetup, onOpenZones }: TbmPageProps) {
  const {
    tasks,
    workTypes,
    layouts,
    isFallback,
    isLoading,
    mutationError,
    clearMutationError,
    addTask,
    editTask,
    deleteTask,
  } = useTbmData();
  const [selected, setSelected] = useState(
    initialWorkId ? displayTaskId(initialWorkId) : (tasks[0]?.id ?? 'T1'),
  );
  const [adding, setAdding] = useState(false);
  const [editingTask, setEditingTask] = useState(false);
  const [editingLayout, setEditingLayout] = useState(false);
  const [editingLayoutInitialId, setEditingLayoutInitialId] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  /** 왼쪽 패널을 레이아웃 대신 평가 · 간섭으로 바꿔 보는지(기본은 레이아웃). */
  const [detail, setDetail] = useState(false);
  /** 작업마다 반장 PWA 로 보낸 시각. */
  const [sentAt, setSentAt] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');

  const currentTask = tasks.find((t) => t.id === selected) ?? tasks[0] ?? null;
  const task = currentTask ?? EMPTY_PLACEHOLDER_TASK;
  const workId = currentTask ? (currentTask.workId ?? currentTask.id.replace(/^T/, '')) : undefined;
  const {
    report,
    latest: latestReport,
    isLoaded: reportLoaded,
    versions,
    selectedReportId,
    isGenerating,
    requestError,
    selectVersion,
    generate,
    clearError,
  } = useTbmReport(workId, !isFallback && Boolean(currentTask));

  const session = useWorkSession(workId, selectVersion);

  // 세팅 화면·구역 편집에서 어느 작업인지 알 수 있게 선택한 작업을 남긴다(숫자 id 만).
  const numericWorkId = workId !== undefined && /^\d+$/.test(workId) ? Number(workId) : null;
  useEffect(() => {
    if (!isFallback && numericWorkId !== null && numericWorkId > 0) {
      setSetupWorkUnitId(numericWorkId);
    }
  }, [isFallback, numericWorkId]);

  // 단계 판정은 서버 값으로만: 작업(active_setup_session_id·status·actual_*), 세팅 세션, 최신 보고서.
  const setupSessionId = currentTask?.activeSetupSessionId ?? null;
  const { setup: setupLookup, zonesCommittedHere } = useSetupLookup(setupSessionId, !isFallback);
  const stageResult = deriveWorkStages({
    task: {
      status: currentTask?.status,
      actualStartedAt: currentTask?.actualStartedAt,
      actualEndedAt: currentTask?.actualEndedAt,
    },
    setup: setupLookup,
    zonesCommittedHere,
    report: latestReport,
    reportKnown: reportLoaded,
  });
  const signRef = useRef<HTMLDivElement>(null);

  const startWork = () => {
    // 세팅·구역이 끝나지 않았어도 막지 않고 확인만 한다.
    if (confirmWorkStart(unfinishedBeforeStart(stageResult.stages))) void session.run('start');
  };
  const endWork = () => {
    if (confirmWorkEnd()) void session.run('end');
  };

  const interferences = useMemo(() => tbmInterferences(tasks), [tasks]);
  const { receiving, restart } = useReceiving(task.id);
  const { edits, filled, canUndo, save, undo } = useMinutesForm(task, report);
  const signatures = useSignatures(report);
  const { printing, items, print, error: printError } = usePrint(tasks, task, edits, report);

  const select = (id: string) => {
    if (id !== task.id) {
      setSelected(id);
      restart();
    }
    setEditing(false);
    setEditingTask(false);
    setEditingLayout(false);
    setAdding(false);
    setMessage('');
    clearError();
    clearMutationError();
  };

  const openAdd = () => {
    setAdding(true);
    setEditingTask(false);
    setEditingLayout(false);
    setMessage('');
    clearMutationError();
  };

  const openLayoutEditor = (layoutId?: number | null) => {
    const targetId = layoutId !== undefined ? layoutId : (currentTask?.layoutId ?? null);
    setEditingLayoutInitialId(targetId);
    setEditingLayout(true);
    setEditingTask(false);
    setAdding(false);
    setMessage('');
    clearMutationError();
  };

  const add = (input: TaskInput) => {
    const previousSelected = currentTask?.id ?? 'T1';
    const optimistic = createTask(tasks, input);
    setAdding(false);
    setSelected(optimistic.id);
    restart();
    clearError();
    clearMutationError();
    setMessage(
      `${optimistic.id} 등록됨 · ${
        optimistic.layoutId ? `가상 레이아웃 #${optimistic.layoutId} 연결` : '배치는 아직 없음'
      }`,
    );
    void addTask(input)
      .then((created) => {
        if (created.id !== optimistic.id) {
          setSelected(created.id);
        }
      })
      .catch(() => {
        setSelected(previousSelected);
        setMessage('');
      });
  };

  const saveTaskEdit = (input: TaskInput) => {
    if (!currentTask) return;
    setEditingTask(false);
    void editTask(currentTask, input)
      .then((updated) => {
        const wtLabel = workTypeLabelOf(updated.workType, workTypes);
        setMessage(
          `${updated.id} 수정됨${wtLabel ? ` · 공종 ${wtLabel}` : ''}${
            updated.layoutId ? ` · 레이아웃 #${updated.layoutId}` : ' · 레이아웃 미연결'
          }`,
        );
      })
      .catch(() => {
        setMessage('');
      });
  };

  const handleDeleteTask = () => {
    if (!currentTask) return;
    const deletedId = currentTask.id;
    const remaining = tasks.filter((t) => t.id !== deletedId);
    setEditingTask(false);
    void deleteTask(currentTask)
      .then(() => {
        if (remaining[0]) {
          setSelected(remaining[0].id);
        }
        setMessage(`${deletedId} 삭제됨`);
      })
      .catch(() => {
        setMessage('');
      });
  };

  const handleLayoutSaved = (savedLayout: LayoutDetail) => {
    setEditingLayout(false);
    if (!currentTask) {
      setMessage(`가상 레이아웃 #${savedLayout.layout_id} (rev.${savedLayout.revision}) 저장됨`);
      return;
    }
    const nextInput: TaskInput = {
      ...taskToInput(currentTask),
      layoutId: String(savedLayout.layout_id),
    };
    void editTask(currentTask, nextInput)
      .then(() => {
        setMessage(
          `${currentTask.id} 가상 레이아웃 #${savedLayout.layout_id} (rev.${savedLayout.revision}) 저장 · 연결됨`,
        );
      })
      .catch(() => {
        setMessage(`가상 레이아웃 #${savedLayout.layout_id} 저장됨 (작업 연결 실패)`);
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
      <PageTitle>작업 관리</PageTitle>
      {currentTask ? (
        <>
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
              <Button
                type="button"
                variant="secondary"
                size="sm"
                aria-haspopup="dialog"
                onClick={() => openLayoutEditor(task.layoutId ?? null)}
                className={SWITCH}
              >
                3D 레이아웃 편집
              </Button>
              {isFallback && (
                <span
                  role="status"
                  data-testid="tbm-demo-badge"
                  className={cn(
                    PILL,
                    'bg-amber-500/20 font-semibold text-amber-300 ring-amber-400/50',
                  )}
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
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span>
              상태:{' '}
              {task.status === 'IN_PROGRESS'
                ? '진행'
                : task.status === 'DONE'
                  ? '종료'
                  : task.status === 'PLANNED'
                    ? '계획'
                    : '알 수 없음'}
            </span>
            <span>
              실제 시작:{' '}
              {task.actualStartedAt ? new Date(task.actualStartedAt).toLocaleString() : '—'}
            </span>
            <span>
              실제 종료: {task.actualEndedAt ? new Date(task.actualEndedAt).toLocaleString() : '—'}
            </span>
          </div>
          {!isFallback && (
            <WorkStageBar
              result={stageResult}
              status={task.status}
              hasSetupSession={setupSessionId !== null}
              canOpenSetup={numericWorkId !== null && numericWorkId > 0}
              generating={isGenerating}
              pending={session.pending}
              onOpenSetup={() => {
                if (numericWorkId !== null) onOpenSetup?.(numericWorkId);
              }}
              onOpenZones={() => onOpenZones?.()}
              onGenerate={handleGenerate}
              onStart={startWork}
              onEnd={endWork}
              onSign={() => signRef.current?.scrollIntoView?.({ block: 'start' })}
            />
          )}
          <p role="status">
            {report?.wrap_up_summary
              ? `작업 마무리: ${report.wrap_up_summary}`
              : report?.wrap_up_status === 'GENERATING'
                ? WRAP_UP_GENERATING_TEXT
                : session.message}
          </p>
          <p role="status" className="text-[13px] text-point-soft empty:hidden">
            {message}
          </p>
          {printError && <p role="alert">{printError}</p>}
          {mutationError && (
            <div
              role="alert"
              data-testid="tbm-mutation-error"
              className="rounded-lg bg-destructive/15 px-3.5 py-2 text-[13px] text-destructive ring-1 ring-destructive/40 ring-inset"
            >
              {mutationError}
            </div>
          )}
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
          {report && !isFallback && (
            <div ref={signRef}>
              <SignaturePanel
                key={`${report.report_id}-${report.report_version}`}
                report={report}
                signatures={signatures}
              />
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
        </>
      ) : (
        <div className="flex min-h-[560px] flex-1 flex-col gap-3">
          {mutationError && (
            <div
              role="alert"
              data-testid="tbm-mutation-error"
              className="rounded-lg bg-destructive/15 px-3.5 py-2 text-[13px] text-destructive ring-1 ring-destructive/40 ring-inset"
            >
              {mutationError}
            </div>
          )}
          <Card
            data-testid="tbm-empty-state"
            className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center"
          >
            {isLoading ? (
              <p className="text-sm text-muted-foreground">작업 목록을 불러오는 중입니다…</p>
            ) : (
              <>
                <p className="text-base font-semibold text-foreground">
                  등록된 작업이 없습니다 — 작업 추가
                </p>
                <p className="text-xs text-muted-foreground">
                  새 작업을 등록하고 가상 레이아웃을 배치해 TBM 보고서를 생성하세요.
                </p>
                <Button type="button" onClick={openAdd} className="font-bold">
                  + 작업 등록
                </Button>
              </>
            )}
          </Card>
        </div>
      )}
      {adding && (
        <TaskAddDialog
          workTypes={workTypes}
          layouts={layouts}
          serverZones={!isFallback}
          onAdd={add}
          onCancel={() => setAdding(false)}
          onOpenLayoutEditor={(layoutId) => openLayoutEditor(layoutId)}
        />
      )}
      {editingTask && currentTask && (
        <TaskEditDialog
          task={currentTask}
          workTypes={workTypes}
          layouts={layouts}
          serverZones={!isFallback}
          onSave={saveTaskEdit}
          onCancel={() => setEditingTask(false)}
          onDelete={handleDeleteTask}
          onOpenLayoutEditor={(layoutId) => openLayoutEditor(layoutId)}
        />
      )}
      {editingLayout && (
        <TbmLayoutEditorDialog
          task={task}
          layouts={layouts}
          initialLayoutId={editingLayoutInitialId}
          onSaved={handleLayoutSaved}
          onClose={() => setEditingLayout(false)}
        />
      )}
      {editing && currentTask && (
        <TbmEditDialog onClose={closeEditor} onUndo={undo} canUndo={canUndo}>
          <TbmFormSheet form={filled} editable onSave={save} people={task.people} />
        </TbmEditDialog>
      )}
      {printing && <TbmPrint items={items} />}
    </>
  );
}

export function TbmPage(props: TbmPageProps = {}) {
  const existingClient = useContext(QueryClientContext);
  const [fallbackClient] = useState(() => createQueryClient());
  if (existingClient) {
    return <TbmPageContent {...props} />;
  }
  return (
    <QueryClientProvider client={fallbackClient}>
      <TbmPageContent {...props} />
    </QueryClientProvider>
  );
}
