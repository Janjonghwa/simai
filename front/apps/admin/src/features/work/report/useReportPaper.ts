import { isDrafted, reportFormOf } from '@/features/work/reportForm';
import type { WorkSignature, WorkTask } from '@/features/work/types';
import { useSignatures } from '@/features/work/useSignatures';
import { useWorkTasks } from '@/features/work/useWorkDays';
import { useWorkReport } from '@/features/work/useWorkReport';

// 작업의 보고서 종이(ReportView)를 그리는 데 필요한 값 전부. 종이를 그리는 화면(본문 종이·작은 미리보기)이 같이 쓴다.

const NO_TASKS: readonly WorkTask[] = [];
const NO_SIGNATURES: readonly WorkSignature[] = [];

export function useReportPaper(workId: string) {
  const query = useWorkReport(workId);
  const latest = query.data ?? null;
  const signatures = useSignatures(latest?.report_id).data ?? NO_SIGNATURES;
  const catalog = useWorkTasks().data ?? NO_TASKS;
  return {
    query,
    /** 작업의 최신 판. 아직 받는 중이면 null. */
    latest,
    paper: {
      form: reportFormOf(latest),
      leader: latest?.leader ?? null,
      signatures,
      catalog,
      drafted: isDrafted(latest),
    },
  };
}
