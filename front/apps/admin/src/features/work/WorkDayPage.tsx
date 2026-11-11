import { isWorkError, WORK_ERROR } from '@/features/work/api';
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
// 단계는 서버 progress 가 정한다. 보고서 → 세팅처럼 서버 progress 가 아직 못 따라온 이동만 로컬 상태가 먼저 맡는다.

/** 보고서 → 세팅 → 작업 전 확인까지만 로컬에서 넘긴다(그 뒤 단계는 서버 progress 가 따라온다). */
const NEXT_STAGE: Partial<Record<WorkStageId, WorkStageId>> = {
  report: 'setup',
  setup: 'preflight',
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
  onBack,
  onSubmitted,
}: {
  day: WorkDayItem;
  date: string;
  kind: DayKind;
  todayKey: string;
  localStage: WorkStageId;
  onLocalStage: (stage: WorkStageId) => void;
  onBack: () => void;
  onSubmitted: () => void;
}) {
  const stage = furtherStage(localStage, stageOfProgress(day.progress ?? 'REPORT'));
  const stageProps = {
    day,
    date,
    todayKey,
    onNext: () => onLocalStage(NEXT_STAGE[stage] ?? stage),
  };

  return (
    <>
      <DayHeader date={date} kind={kind} onBack={onBack}>
        <WorkStageNav stage={stage} />
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
  // 서버 progress 가 아직 못 따라온 이동(보고서 → 세팅 → 작업 전 확인)을 로컬이 먼저 맡는다. 서버가 따라잡을 때까지 다시 받는다.
  const [localStage, setLocalStage] = useState<WorkStageId>('report');
  const { kind, data, pending, error, refetch } = useOpenDay(date, todayKey, localStage);

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
