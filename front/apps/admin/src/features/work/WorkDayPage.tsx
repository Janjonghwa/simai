import { isWorkError, WORK_ERROR } from '@/features/work/api';
import { PastDayPage } from '@/features/work/past/PastDayPage';
import { PreflightStage } from '@/features/work/preflight/PreflightStage';
import { ReportStage } from '@/features/work/report/ReportStage';
import { RunStage } from '@/features/work/run/RunStage';
import { SetupStage } from '@/features/work/SetupStage';
import type { WorkDayItem } from '@/features/work/types';
import { useOpenDay } from '@/features/work/useWorkDays';
import { WorkActionProvider } from '@/features/work/WorkActionContext';
import { WorkActionLayer } from '@/features/work/WorkActionLayer';
import {
  formatDayTitle,
  isEndedWork,
  stageIndexOf,
  stageOfProgress,
  type DayKind,
  type WorkStageId,
} from '@/features/work/workDay';
import { WorkStageNav } from '@/features/work/WorkStageNav';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { ChevronLeft } from 'lucide-react';
import { useState, type ReactNode } from 'react';

// 날짜 화면(오늘·예정인 날): 머리줄(달력 단추 · 날짜 · 오늘/예정 · 단계 줄) 아래에 단계 화면 하나를 보인다.
// 단계는 서버 progress 가 정한다. 보고서 → 세팅(세션을 만들기 전이라 서버가 아직 REPORT)만 로컬 상태가 먼저 맡는다.
// 보고서를 보내(제출·잠금) 달력으로 나간 뒤 그 날짜를 다시 열면(SUBMITTED) 단계 화면 대신 지난 날과 같은 다시 보기 화면(PastDayPage)을 보인다.

/**
 * 로컬에서 먼저 넘기는 이동은 보고서 → 세팅뿐이다. 작업 전 확인(작업 시작 단추)은 서버가 세팅 완료(work_unit.setup_completed_at,
 * progress SETUP_DONE)를 알린 뒤에만 연다(-642). 세팅 완료는 세팅 화면이 하루 작업을 다시 받은 뒤 끝나므로 기다림이 없다.
 */
const NEXT_STAGE: Partial<Record<WorkStageId, WorkStageId>> = {
  report: 'setup',
};

const KIND_CHIP: Partial<Record<DayKind, string>> = { today: '오늘', future: '예정' };

/** 머리줄. 단계 줄은 날짜 작업을 열고 난 뒤에 children 으로 들어온다. */
function DayHeader({
  date,
  kind,
  onBack,
  children,
}: {
  date: string;
  kind: DayKind;
  onBack: () => void;
  children?: ReactNode;
}) {
  const chip = KIND_CHIP[kind];
  return (
    <header className="flex flex-none items-center gap-x-3.5 gap-y-1">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ChevronLeft aria-hidden />
        달력
      </Button>
      <h2 className="text-[17px] font-bold whitespace-nowrap">{formatDayTitle(date)}</h2>
      {chip && <Badge variant="outline">{chip}</Badge>}
      {children}
    </header>
  );
}

/** 하루 작업을 못 열었을 때. 지난 날짜는 다시 시도해도 안 되므로 단추 없이 알린다. */
function OpenFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (isWorkError(error, WORK_ERROR.DATE_PAST)) {
    return <p className="text-muted-foreground">지난 날짜는 새로 열 수 없어요.</p>;
  }
  return (
    <p className="flex items-center gap-2 text-muted-foreground">
      작업을 불러오지 못했습니다.
      <Button variant="secondary" size="sm" onClick={onRetry}>
        다시 시도
      </Button>
    </p>
  );
}

/** 지금 단계: 로컬 단계와 서버 progress 가 가리키는 단계 중 앞선 쪽. 서버가 앞서면 따라간다. */
const furtherStage = (local: WorkStageId, server: WorkStageId): WorkStageId =>
  stageIndexOf(server) > stageIndexOf(local) ? server : local;

function OpenedDay({
  day,
  date,
  kind,
  todayKey,
  localStage,
  onLocalStage,
  onRefresh,
  onBack,
  onSubmitted,
}: {
  day: WorkDayItem;
  date: string;
  kind: DayKind;
  todayKey: string;
  localStage: WorkStageId;
  onLocalStage: (stage: WorkStageId) => void;
  /** 하루 작업을 서버에서 다시 받는다(단계를 서버 progress 에 다시 맞춘다). */
  onRefresh: () => void;
  onBack: () => void;
  onSubmitted: () => void;
}) {
  const progress = day.progress ?? 'REPORT';
  const stage = furtherStage(localStage, stageOfProgress(progress));
  const stageProps = {
    day,
    date,
    todayKey,
    onNext: () => onLocalStage(NEXT_STAGE[stage] ?? stage),
    // 작업 시작이 세팅 완료 전이라 막혔을 때 세팅으로 돌아간다. 로컬은 세팅까지만 앞서므로 서버 progress 를 다시 받으면 따라간다
    onBackToReport: () => onLocalStage('report'),
    onBackToSetup: () => {
      onLocalStage('setup');
      onRefresh();
    },
  };

  return (
    <>
      <DayHeader date={date} kind={kind} onBack={onBack}>
        <WorkStageNav stage={stage} finished={isEndedWork(day)} />
      </DayHeader>
      {stage === 'report' && <ReportStage {...stageProps} />}
      {stage === 'setup' && <SetupStage {...stageProps} />}
      {stage === 'preflight' && <PreflightStage {...stageProps} />}
      {stage === 'run' && <RunStage {...stageProps} onSubmitted={onSubmitted} />}
    </>
  );
}

export function WorkDayPage({
  date,
  todayKey,
  onBack,
  onSubmitted,
}: {
  /** 날짜 키(YYYY-MM-DD). 오늘이거나 예정인 날이다(지난 날은 `PastDayPage`). */
  date: string;
  todayKey: string;
  /** '달력' 단추. */
  onBack: () => void;
  /** 보고서를 보내(제출·잠금) 달력으로 나가야 할 때. */
  onSubmitted: () => void;
}) {
  // 서버 progress 가 아직 못 따라온 이동(보고서 → 세팅)을 로컬이 먼저 맡는다. 서버가 따라잡을 때까지 다시 받는다.
  const [localStage, setLocalStage] = useState<WorkStageId>('report');
  const { kind, data, pending, error, refetch } = useOpenDay(date, todayKey, localStage);

  if (data?.progress === 'SUBMITTED') return <PastDayPage date={date} />;

  return (
    // 아래 70px 은 우측 아래 단추 줄(WorkActionLayer)이 본문을 가리지 않게 비운다.
    <div className="flex min-h-[calc(100vh-140px)] flex-none flex-col gap-3.5 pb-24">
      <WorkActionProvider>
        {data ? (
          <OpenedDay
            day={data}
            date={date}
            kind={kind}
            todayKey={todayKey}
            localStage={localStage}
            onLocalStage={setLocalStage}
            onRefresh={() => void refetch()}
            onBack={onBack}
            onSubmitted={onSubmitted}
          />
        ) : (
          <>
            <DayHeader date={date} kind={kind} onBack={onBack} />
            {pending && <p className="text-muted-foreground">불러오는 중입니다…</p>}
            {!pending && error && <OpenFailure error={error} onRetry={() => void refetch()} />}
          </>
        )}
        <WorkActionLayer />
      </WorkActionProvider>
    </div>
  );
}
