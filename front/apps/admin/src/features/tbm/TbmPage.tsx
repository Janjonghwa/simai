import { PageTitle } from '@/features/layout/PageTitle';
import { WRAP_UP_GENERATING_TEXT } from '@/features/tbm/constants';
import { SignaturePanel, useSignatures } from '@/features/tbm/SignaturePanel';
import { ALERT, PILL, SWITCH } from '@/features/tbm/styles';
import { TaskAddDialog, TaskEditDialog } from '@/features/tbm/TaskForm';
import { createTask, taskToInput, todayIsoDate, type TaskInput } from '@/features/tbm/taskInput';
import { TaskList } from '@/features/tbm/TaskList';
import { tbmInterferences } from '@/features/tbm/tbm';
import { TBM_TASKS, type TbmTask } from '@/features/tbm/tbmDemo';
import { TbmPrint } from '@/features/tbm/TbmDocument';
import { TbmEditDialog } from '@/features/tbm/TbmEditDialog';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import { TbmLayoutEditorDialog } from '@/features/tbm/TbmLayoutEditorDialog';
import { TbmLeftPanel } from '@/features/tbm/TbmLeftPanel';
import { TbmLegalPanel } from '@/features/tbm/TbmLegalPanel';
import { TbmMinutesPanel } from '@/features/tbm/TbmMinutesPanel';
import { displayTaskId, workTypeLabelOf, type LayoutDetail } from '@/features/tbm/types';
import { useMinutesForm } from '@/features/tbm/useMinutesForm';
import { usePrint } from '@/features/tbm/usePrint';
import { useSetupLookup } from '@/features/tbm/useSetupLookup';
import { useTbmData } from '@/features/tbm/useTbmData';
import { useTbmReport } from '@/features/tbm/useTbmReport';
import { useWorkSession } from '@/features/tbm/useWorkSession';
import { confirmWorkDelete, confirmWorkEnd, confirmWorkStart } from '@/features/tbm/workConfirm';
import { dayText, WorkDateNav } from '@/features/tbm/WorkDateNav';
import { deriveWorkStages, unfinishedBeforeStart, type StageKey } from '@/features/tbm/workStage';
import { WorkStageBar } from '@/features/tbm/WorkStageBar';
import { WorkRunningCard } from '@/features/tbm/WorkStatusCards';
import { createQueryClient } from '@/lib/queryClient';
import { Button } from '@simai/ui/components/button';
import { Card } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { QueryClientContext, QueryClientProvider } from '@tanstack/react-query';
import { useContext, useMemo, useRef, useState } from 'react';

const EMPTY_PLACEHOLDER_TASK: TbmTask = TBM_TASKS[0]!;

/**
 * 그날의 기본 선택: 진행 중 → 예정 → 상태 값이 없는 작업(데모·폴백). 지난 날은 끝난 작업을 돌아보는 곳이라 끝난 작업도
 * 고른다. 오늘·앞날에 끝난 작업만 있으면 고르지 않는다(새 작업 등록 빈 상태).
 */
function defaultSelectedTask(tasks: readonly TbmTask[], past: boolean): TbmTask | undefined {
  return (
    tasks.find((t) => t.status === 'IN_PROGRESS') ??
    tasks.find((t) => t.status === 'PLANNED') ??
    tasks.find((t) => t.status === undefined) ??
    (past ? tasks[0] : undefined)
  );
}

// 작업 관리(/work). 위쪽 두 줄: 날짜(◀ 날짜 ▶ · 오늘)와 그날 작업, 고른 작업의 단계 줄(세팅 → 구역 → TBM 보고서 → 시작 → 종료 →
// 마무리·서명)과 다음 행동 단추. 본문은 고른 작업의 상태에 따라 바뀐다:
// - 계획: 가상 레이아웃 · 법령 근거(RAG) · TBM 회의록 3블록(1:1:1)
// - 진행 중: 시작 · 경과 · 사건 수와 실시간 관제로 가기 · 작업 종료(TBM 3블록은 접어 둔다)
// - 종료: 3블록. 서명 · 결재 패널은 단계 줄의 서명하기로 펼친다.
// 3블록은 화면 높이에 맞춰 줄어들고(최소 높이 없음) 블록 안 내용이 넘치면 블록 안에서만 스크롤한다.
// 서버 API 실패(네트워크 끊김·5xx) 때만 tbmDemo 로 대체 화면을 그린다.

export interface TbmPageProps {
  /** 처음 선택할 작업 id(세팅 화면에서 돌아올 때 /work?workUnitId=). */
  initialWorkId?: string;
  /**
   * 이 작업의 현장 세팅 화면으로 간다(/setup?workUnitId=). 구역 단계도 세팅 화면의 마지막 단계라 같은 곳으로 간다(맵이
   * 준비된 세션이면 세팅 화면이 구역 단계로 연다).
   */
  onOpenSetup?: (workUnitId: number) => void;
  /** 실시간 관제로 간다. */
  onOpenMonitor?: () => void;
}

function TbmPageContent({ initialWorkId, onOpenSetup, onOpenMonitor }: TbmPageProps) {
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
  const today = todayIsoDate(new Date());
  /** 고른 날짜. 고르기 전에는 처음 고른 작업(?workUnitId=)의 날짜, 그것도 없으면 오늘. */
  const [pickedDay, setDay] = useState<string | null>(null);
  const [selected, setSelected] = useState(initialWorkId ? displayTaskId(initialWorkId) : null);
  const [adding, setAdding] = useState(false);
  const [editingTask, setEditingTask] = useState(false);
  const [editingLayout, setEditingLayout] = useState(false);
  const [editingLayoutInitialId, setEditingLayoutInitialId] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  /** 왼쪽 블록을 레이아웃 대신 평가 · 간섭으로 바꿔 보는지(기본은 레이아웃). */
  const [detail, setDetail] = useState(false);
  /** 종료한 작업의 서명 · 결재 패널을 펼쳤는지. */
  const [signOpen, setSignOpen] = useState(false);
  /** 진행 중 작업의 접어 둔 TBM 3블록을 펼쳤는지. */
  const [showTbm, setShowTbm] = useState(false);
  const [message, setMessage] = useState('');

  const initialTask = initialWorkId
    ? tasks.find((t) => t.id === displayTaskId(initialWorkId))
    : undefined;
  const day = pickedDay ?? initialTask?.date ?? today;

  // 그날 작업(계획 시작 시각순). 날짜가 없는 대체 화면 작업은 오늘로 본다.
  const dayTasks = useMemo(
    () =>
      tasks.filter((t) => (t.date ?? today) === day).sort((a, b) => a.from.localeCompare(b.from)),
    [tasks, day, today],
  );
  const defaultTask = defaultSelectedTask(dayTasks, day < today);
  const currentTask =
    (selected !== null ? dayTasks.find((t) => t.id === selected) : undefined) ??
    defaultTask ??
    null;
  // 그날 목록이 뜬 첫 순간의 기본 선택을 굳힌다(렌더 중 갱신). 그 뒤 작업 상태가 바뀌어도 선택이 옮겨 가지 않는다.
  if (selected === null && defaultTask) setSelected(defaultTask.id);
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

  // 세팅 화면은 서버 작업(숫자 id)만 연다.
  const numericWorkId = workId !== undefined && /^\d+$/.test(workId) ? Number(workId) : null;
  const canOpenSetup = numericWorkId !== null && numericWorkId > 0;

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
  const legalRef = useRef<HTMLDivElement>(null);

  const startWork = () => {
    // 세팅·구역이 끝나지 않았어도 막지 않고 확인만 한다.
    if (confirmWorkStart(unfinishedBeforeStart(stageResult.stages))) void session.run('start');
  };
  const endWork = () => {
    if (confirmWorkEnd()) void session.run('end');
  };

  const interferences = useMemo(() => tbmInterferences(dayTasks), [dayTasks]);
  const { edits, filled, canUndo, save, undo } = useMinutesForm(task, report);
  const signatures = useSignatures(report);
  const { printing, items, print, error: printError } = usePrint(dayTasks, task, edits, report);

  /** 작업이나 날짜를 바꾸면 열려 있던 창과 펼친 패널을 닫는다. */
  const resetView = () => {
    setEditing(false);
    setEditingTask(false);
    setEditingLayout(false);
    setAdding(false);
    setSignOpen(false);
    setShowTbm(false);
    setMessage('');
    clearError();
    clearMutationError();
  };

  const select = (id: string) => {
    setSelected(id);
    resetView();
  };

  const changeDay = (next: string) => {
    setDay(next);
    setSelected(null);
    resetView();
  };

  const openAdd = () => {
    setAdding(true);
    setEditingTask(false);
    setEditingLayout(false);
    setMessage('');
    clearMutationError();
  };

  const openLayoutEditor = (layoutId?: number | null) => {
    // 진행 중인 작업의 레이아웃은 관제 구역이 되어 있어 여기서 고치지 않는다(관제에서 고친다).
    if (currentTask?.status === 'IN_PROGRESS') return;
    const targetId = layoutId !== undefined ? layoutId : (currentTask?.layoutId ?? null);
    setEditingLayoutInitialId(targetId);
    setEditingLayout(true);
    setEditingTask(false);
    setAdding(false);
    setMessage('');
    clearMutationError();
  };

  const add = (input: TaskInput) => {
    const previousSelected = currentTask?.id ?? null;
    const optimistic = createTask(tasks, input, day);
    setAdding(false);
    setSelected(optimistic.id);
    clearError();
    clearMutationError();
    setMessage(
      `${optimistic.id} 등록됨 · ${
        optimistic.layoutId ? `가상 레이아웃 #${optimistic.layoutId} 연결` : '배치는 아직 없음'
      }`,
    );
    void addTask(input, day)
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
    if (!currentTask || !confirmWorkDelete(`${currentTask.id} · ${currentTask.name}`)) return;
    const deletedId = currentTask.id;
    setEditingTask(false);
    void deleteTask(currentTask)
      .then(() => {
        setSelected(null);
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

  const openSetup = () => {
    if (canOpenSetup) onOpenSetup?.(numericWorkId);
  };

  /** 단계 줄의 단계를 누르면 그 단계 화면으로 간다. */
  const openStage = (key: StageKey) => {
    if (key === 'setup' || key === 'zones') openSetup();
    else if (key === 'start' || key === 'end') onOpenMonitor?.();
    else if (key === 'sign') setSignOpen(task.status === 'DONE');
    else {
      setShowTbm(true);
      legalRef.current?.scrollIntoView?.({ block: 'nearest' });
    }
  };

  const closeEditor = () => setEditing(false);

  // 세팅·구역이 아직이고 보고서도 없으면 3블록은 빈 칸뿐이다. 흐리게 덮고 가운데에 다음 단계 단추를 크게 둔다(둘 다 세팅
  // 화면을 연다. 구역은 세팅 화면의 마지막 단계다).
  const gate =
    isFallback || report
      ? null
      : stageResult.current === 'setup'
        ? setupSessionId !== null
          ? '세팅 이어가기'
          : '세팅 시작'
        : stageResult.current === 'zones'
          ? '구역 설정'
          : null;

  // 서명 패널이 열려 있으면 그 패널이 화면을 크게 차지한다. 이때만 3블록에 최소 높이를 두어 0 으로 줄지 않게 하고 페이지가 스크롤한다.
  const signReport = task.status === 'DONE' && signOpen && !isFallback ? report : null;

  const blocks = (
    <div className={cn('relative flex min-h-0 flex-1 flex-col', signReport && 'min-h-[450px]')}>
      <div
        inert={gate !== null}
        className={cn(
          'grid min-h-0 flex-1 grid-cols-3 grid-rows-[minmax(0,1fr)] gap-3.5',
          gate && 'pointer-events-none opacity-50 blur-[3px] select-none',
        )}
      >
        <TbmLeftPanel
          task={task}
          tasks={dayTasks}
          interferences={interferences}
          detail={detail}
          report={report}
          onSwitch={() => setDetail(!detail)}
          onEditLayout={() => openLayoutEditor(task.layoutId ?? null)}
        />
        <div ref={legalRef} className="flex min-h-0 min-w-0 flex-col">
          <TbmLegalPanel
            report={report}
            versions={versions}
            selectedReportId={selectedReportId}
            generating={isGenerating}
            onSelectVersion={selectVersion}
            onGenerate={handleGenerate}
          />
        </div>
        <TbmMinutesPanel
          task={task}
          form={filled}
          blank={!report}
          version={report?.report_version}
          canUndo={canUndo}
          onEdit={() => setEditing(true)}
          onUndo={undo}
          onPrint={() => print(false, closeEditor)}
        />
      </div>
      {gate && (
        <div className="absolute inset-0 flex items-center justify-center">
          <Button
            disabled={!canOpenSetup}
            onClick={openSetup}
            className="h-14 rounded-xl px-9 text-lg font-bold shadow-float"
          >
            {gate}
          </Button>
        </div>
      )}
    </div>
  );

  return (
    <>
      <PageTitle>작업 관리</PageTitle>
      <div className="flex flex-none items-center gap-3">
        <WorkDateNav day={day} today={today} onChange={changeDay} />
        <TaskList
          tasks={dayTasks}
          selectedId={currentTask?.id ?? null}
          dayLabel={dayText(day)}
          onSelect={select}
          onAdd={openAdd}
          onEdit={() => {
            setEditingTask(true);
            setAdding(false);
          }}
          onDelete={handleDeleteTask}
        />
        {isFallback && (
          <span
            role="status"
            data-testid="tbm-demo-badge"
            className={cn(PILL, 'bg-amber-500/20 font-semibold text-amber-300 ring-amber-400/50')}
          >
            데모 데이터
          </span>
        )}
        <Button
          variant="secondary"
          size="sm"
          disabled={dayTasks.length === 0}
          onClick={() => print(true, closeEditor)}
          className={SWITCH}
        >
          묶음 출력
        </Button>
      </div>
      {currentTask ? (
        <>
          {!isFallback && (
            <WorkStageBar
              result={stageResult}
              status={task.status}
              hasSetupSession={setupSessionId !== null}
              canOpenSetup={canOpenSetup}
              generating={isGenerating}
              pending={session.pending}
              onStep={openStage}
              onOpenSetup={openSetup}
              onGenerate={handleGenerate}
              onStart={startWork}
              onSign={() => setSignOpen(true)}
              hideNext={gate !== null}
              canSign={Boolean(report)}
            />
          )}
          <p role="status" className="flex-none text-sm empty:hidden">
            {session.message ||
              (report?.wrap_up_status === 'GENERATING' ? WRAP_UP_GENERATING_TEXT : '')}
          </p>
          <p role="status" className="flex-none text-[13px] text-point-soft empty:hidden">
            {message}
          </p>
          {printError && <p role="alert">{printError}</p>}
          {mutationError && (
            <div role="alert" data-testid="tbm-mutation-error" className={ALERT}>
              {mutationError}
            </div>
          )}
          {requestError && (
            <div role="alert" data-testid="tbm-request-error" className={ALERT}>
              {requestError}
            </div>
          )}
          {task.status === 'IN_PROGRESS' && (
            <WorkRunningCard
              task={task}
              pending={session.pending}
              showTbm={showTbm}
              onMonitor={() => onOpenMonitor?.()}
              onEnd={endWork}
              onToggleTbm={() => setShowTbm(!showTbm)}
            />
          )}
          {signReport && (
            <div className="relative flex-none">
              <SignaturePanel
                key={`${signReport.report_id}-${signReport.report_version}`}
                report={signReport}
                signatures={signatures}
              />
              <button
                type="button"
                aria-label="서명 닫기"
                onClick={() => setSignOpen(false)}
                className="absolute top-3 right-3 inline-flex size-8 items-center justify-center rounded-md bg-surface-2 text-muted-foreground hover:bg-surface-3 hover:text-foreground"
              >
                ×
              </button>
            </div>
          )}
          {(task.status !== 'IN_PROGRESS' || showTbm) && blocks}
        </>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          {mutationError && (
            <div role="alert" data-testid="tbm-mutation-error" className={ALERT}>
              {mutationError}
            </div>
          )}
          <Card
            data-testid="tbm-empty-state"
            className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center"
          >
            {isLoading ? (
              <p className="text-sm text-muted-foreground">작업 목록을 불러오는 중입니다…</p>
            ) : dayTasks.length > 0 ? (
              <>
                <p className="text-base font-semibold text-foreground">진행 중인 작업이 없습니다</p>
                <Button type="button" onClick={openAdd} className="font-bold">
                  새 작업 등록
                </Button>
              </>
            ) : (
              <>
                <p className="text-base font-semibold text-foreground">등록된 작업이 없습니다</p>
                <Button type="button" onClick={openAdd} className="font-bold">
                  + 작업 등록
                </Button>
              </>
            )}
          </Card>
        </div>
      )}
      {adding && (
        <TaskAddDialog workTypes={workTypes} onAdd={add} onCancel={() => setAdding(false)} />
      )}
      {editingTask && currentTask && (
        <TaskEditDialog
          task={currentTask}
          workTypes={workTypes}
          onSave={saveTaskEdit}
          onCancel={() => setEditingTask(false)}
          onDelete={handleDeleteTask}
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
