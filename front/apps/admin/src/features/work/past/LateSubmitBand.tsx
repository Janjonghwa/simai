import { ALERT } from '@/features/tbm/styles';
import { ConfirmDialog } from '@/features/work/ConfirmDialog';
import { submitBlockReason } from '@/features/work/run/run';
import { sendErrorMessage } from '@/features/work/run/runErrors';
import { useSendReport } from '@/features/work/run/useRunActions';
import { useWorkReport } from '@/features/work/useWorkReport';
import { draftWarning, isReportGenerating } from '@/features/work/workDay';
import { Button } from '@simai/ui/components/button';
import { FileWarning } from 'lucide-react';

// 지난 날짜 화면 위쪽 띠(S15P31A101-750): 작업은 끝났는데 보고서를 안 낸 날(진행 ENDED)에 「보고서 미제출」과 「보고서 제출」을 둔다.
// 자정이 지나면 날짜 화면이 지난 날 다시 보기로 바뀌어 종료 화면의 제출 단추가 사라지므로, 뒤늦게라도 낼 길을 여기에 둔다.
// 제출은 오늘 흐름과 같은 확인창·같은 서버 규칙(1회·잠금·해시)을 쓴다. 「한 번 더 실행」은 두지 않는다(관제는 오늘만).

export function LateSubmitBand({
  workId,
  onSubmitted,
}: {
  workId: string;
  onSubmitted?: () => void;
}) {
  const { data: report, error: reportError } = useWorkReport(workId);
  const send = useSendReport(onSubmitted ?? (() => undefined));
  const reportId = report?.report_id;
  const blockedBy = submitBlockReason(report, reportError !== null);
  const enabled = reportId !== undefined && !isReportGenerating(report) && !send.pending;
  return (
    <>
      <div
        role="status"
        data-testid="late-submit-band"
        className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-sev-caution/60 bg-card px-3.5 py-2.5 text-sm"
      >
        <FileWarning aria-hidden className="size-4 shrink-0 text-sev-caution" />
        <span className="font-semibold">보고서 미제출</span>
        <span className="text-muted-foreground">
          작업은 끝났지만 보고서를 아직 내지 않았습니다.
        </span>
        <span className="ml-auto flex items-center gap-2.5">
          {blockedBy && <span className="text-[13px] text-muted-foreground">{blockedBy}</span>}
          <Button size="sm" disabled={!enabled} onClick={send.request}>
            보고서 제출
          </Button>
        </span>
      </div>
      {send.open && reportId !== undefined && (
        <ConfirmDialog
          title="보고서를 제출할까요?"
          description="제출한 뒤에는 고칠 수 없습니다."
          confirmLabel="제출"
          pending={send.pending}
          onConfirm={() => send.confirm(reportId)}
          onCancel={send.cancel}
        >
          {draftWarning(report) && (
            <p className="text-[13px] text-sev-caution">{draftWarning(report)}</p>
          )}
          {send.error && (
            <p role="alert" className={ALERT}>
              {sendErrorMessage(send.error)}
            </p>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}
