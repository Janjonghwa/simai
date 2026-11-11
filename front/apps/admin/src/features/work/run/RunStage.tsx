import { ALERT } from '@/features/tbm/styles';
import { ConfirmDialog } from '@/features/work/ConfirmDialog';
import { ReportPanel } from '@/features/work/report/ReportPanel';
import { AppliedStatusCard } from '@/features/work/run/AppliedStatusCard';
import { EventListCard } from '@/features/work/run/EventListCard';
import { RunStatusCard } from '@/features/work/run/RunStatusCard';
import { useEndRun, useSendReport } from '@/features/work/run/useRunActions';
import type { RunStageProps } from '@/features/work/stageProps';
import type { WorkDayItem } from '@/features/work/types';
import { useSignatures } from '@/features/work/useSignatures';
import { useWorkReport } from '@/features/work/useWorkReport';
import { useWorkAction, type WorkActionConfig } from '@/features/work/WorkActionContext';
import { isEndingGenerating } from '@/features/work/workDay';
import { useMemo } from 'react';

// 단계 4 '작업 종료'. 서버 진행(progress)이 세 모양을 정한다: 진행 중(IN_PROGRESS) → 종료(ENDED) → 보냄(SUBMITTED).
// 서명은 작업 전 확인에서 이미 받았다. 단추 줄은 useWorkAction 으로 등록한다(`다음` → `완료`, 보낸 뒤에는 없음).

const SPLIT =
  'grid grid-cols-[minmax(0,1.7fr)_minmax(300px,1fr)] items-start gap-4 max-[960px]:grid-cols-1';

function FailureNote({ text }: { text: string }) {
  return (
    <p role="alert" className={ALERT}>
      {text}
    </p>
  );
}

/** 1. 진행 중: 왼쪽 보고서(보내기 전까지 고칠 수 있다), 오른쪽 작업 현황·사건·관제 적용 상태. `다음` 이 종료를 묻는다. */
function RunningView({ day }: { day: WorkDayItem }) {
  const end = useEndRun(day.work_id);
  const { data: report } = useWorkReport(day.work_id);
  const signers = useSignatures(report?.report_id).data;
  const action = useMemo<WorkActionConfig>(
    () => ({ label: '다음', enabled: !end.pending, pending: end.pending, onClick: end.request }),
    [end.pending, end.request],
  );
  useWorkAction(action);
  return (
    <>
      <div className={SPLIT}>
        <div className="min-w-0">
          <ReportPanel workId={day.work_id} editable withPrint />
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <RunStatusCard day={day} signerCount={signers?.length} />
          <EventListCard startedAt={day.actual_started_at} />
          <AppliedStatusCard />
        </div>
      </div>
      {end.open && (
        <ConfirmDialog
          title="작업을 종료할까요?"
          confirmLabel="종료"
          pending={end.pending}
          onConfirm={end.confirm}
          onCancel={end.cancel}
        >
          {end.failed && <FailureNote text="작업을 종료하지 못했습니다. 다시 시도해 주세요." />}
        </ConfirmDialog>
      )}
    </>
  );
}

/**
 * 2. 종료: 종료 미팅이 든 보고서(보내기 전까지 고칠 수 있다). `완료` 가 보낼지 묻는다.
 * 종료 미팅은 서버가 채운다(`wrap_up_status` GENERATING 인 동안 보고서를 다시 받는다). 채우는 중에 보내면 빈 채 잠기므로 `완료` 는 그동안 꺼 둔다.
 */
function EndedView({ day, onSubmitted }: { day: WorkDayItem; onSubmitted: () => void }) {
  const { data: report } = useWorkReport(day.work_id);
  const send = useSendReport(onSubmitted);
  const reportId = report?.report_id;
  const filling = isEndingGenerating(report);
  const action = useMemo<WorkActionConfig>(
    () => ({
      label: '완료',
      enabled: reportId !== undefined && !filling && !send.pending,
      pending: send.pending,
      onClick: send.request,
    }),
    [reportId, filling, send.pending, send.request],
  );
  useWorkAction(action);
  return (
    <>
      <div className="min-w-0">
        <ReportPanel workId={day.work_id} editable withPrint />
      </div>
      {send.open && reportId !== undefined && (
        <ConfirmDialog
          title="보고서를 보낼까요?"
          description="보낸 뒤에는 고칠 수 없습니다."
          confirmLabel="보내기"
          pending={send.pending}
          onConfirm={() => send.confirm(reportId)}
          onCancel={send.cancel}
        >
          {send.failed && <FailureNote text="보고서를 보내지 못했습니다. 다시 시도해 주세요." />}
        </ConfirmDialog>
      )}
    </>
  );
}

/** 3. 보냄: 잠긴 보고서. 단추는 없다. */
function SubmittedView({ day }: { day: WorkDayItem }) {
  useWorkAction(null);
  return (
    <div className="min-w-0">
      <ReportPanel workId={day.work_id} editable={false} withPrint />
    </div>
  );
}

export function RunStage({ day, onSubmitted }: RunStageProps) {
  switch (day.progress) {
    case 'IN_PROGRESS':
      return <RunningView day={day} />;
    case 'ENDED':
      return <EndedView day={day} onSubmitted={onSubmitted} />;
    case 'SUBMITTED':
      return <SubmittedView day={day} />;
    default:
      return null;
  }
}
