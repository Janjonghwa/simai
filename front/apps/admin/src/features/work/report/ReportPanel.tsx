import { SWITCH } from '@/features/tbm/styles';
import { draftErrorMessage } from '@/features/work/preflight/preflightErrors';
import { PaperPrint } from '@/features/work/report/PaperPrint';
import { ReportEditDialog } from '@/features/work/report/ReportEditDialog';
import { ReportLoadError } from '@/features/work/report/ReportLoadError';
import { ReportView } from '@/features/work/report/ReportView';
import { usePaperPrint } from '@/features/work/report/usePaperPrint';
import { useReportPaper } from '@/features/work/report/useReportPaper';
import { formatDateTime, isReportLocked, reportFormOf } from '@/features/work/reportForm';
import type { ReportPanelProps } from '@/features/work/stageProps';
import { useStartReportDraft } from '@/features/work/useWorkReport';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Printer, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';

// TBM 보고서 종이 하나. 작업의 최신 판과 서명 목록을 스스로 읽어 그리고, 고칠 수 있으면 종이를 눌러 편집 창을 연다.
// 출력은 종이 사본만 인쇄한다. 보고서가 아직 없으면(받는 중) 줄무늬 빈 종이다.

/** '저장했습니다' 가 보이는 시간(ms). */
const SAVED_NOTICE_MS = 2_500;

export function ReportPanel({
  workId,
  editable,
  withPrint = false,
  draftable = false,
  className,
}: ReportPanelProps) {
  const { query, latest, paper } = useReportPaper(workId);
  const draft = useStartReportDraft(workId);
  const { printing, print } = usePaperPrint();
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!saved) return undefined;
    const timer = setTimeout(() => setSaved(false), SAVED_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [saved]);

  const canEdit = editable && latest !== null && !isReportLocked(latest);
  const submittedAt = latest?.submitted_at ?? null;
  // 초안이 아직 시작되지 않았거나 실패했을 때만 사람이 요청한다(-624). 작업내용이 없으면 서버가 409 로 거절하므로 미리 끈다
  const draftStatus = latest?.draft_status ?? null;
  const showDraft = draftable && canEdit && (draftStatus === null || draftStatus === 'FAILED');
  const hasTasks = reportFormOf(latest).info.tasks.length > 0;

  return (
    <div className={cn('flex min-h-0 flex-col gap-2.5', className)}>
      {(submittedAt !== null || saved || withPrint || showDraft) && (
        <div className="flex flex-none items-center gap-2.5">
          {submittedAt !== null && (
            <span className="inline-flex items-center rounded-md px-2 py-1 text-[13px] text-ok ring-1 ring-ok/40 ring-inset">
              {`보냄 ${formatDateTime(submittedAt)}`}
            </span>
          )}
          {saved && (
            <span role="status" className="text-[13px] text-muted-foreground">
              저장했습니다
            </span>
          )}
          {showDraft && latest && (
            <Button
              variant="secondary"
              disabled={!hasTasks || draft.pending}
              title={hasTasks ? undefined : '작업내용을 먼저 골라 주세요'}
              onClick={() => draft.mutate(latest.report_id)}
              className={SWITCH}
            >
              <Sparkles />
              {draftStatus === 'FAILED' ? 'AI 초안 다시 만들기' : 'AI 초안 만들기'}
            </Button>
          )}
          {draft.error && (
            <span role="alert" className="text-[13px] text-destructive">
              {draftErrorMessage(draft.error)}
            </span>
          )}
          {withPrint && (
            <Button
              variant="secondary"
              disabled={latest === null}
              onClick={print}
              className={cn(SWITCH, 'ml-auto')}
            >
              <Printer />
              출력
            </Button>
          )}
        </div>
      )}
      {query.error && latest === null && <ReportLoadError onRetry={() => void query.refetch()} />}
      <div className="min-h-0 flex-1 overflow-auto p-1">
        <ReportView {...paper} editable={canEdit} onEdit={() => setEditing(true)} />
      </div>
      {editing && latest && (
        <ReportEditDialog
          report={latest}
          onClose={() => setEditing(false)}
          onSaved={() => setSaved(true)}
        />
      )}
      {printing && (
        <PaperPrint>
          <ReportView {...paper} variant="print" />
        </PaperPrint>
      )}
    </div>
  );
}
