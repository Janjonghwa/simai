import {
  requestGenerateTbmReport,
  requestTbmReportById,
  requestTbmReportsByWork,
  TBM_REPORT_POLL_INTERVAL_MS,
  tbmReportDetailKey,
  tbmReportsByWorkKey,
} from '@/features/tbm/api';
import {
  tbmReportRequestErrorText,
  type TbmReport,
  type TbmReportVersionSummary,
} from '@/features/tbm/types';
import { api } from '@/lib/api';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useLayoutEffect, useState } from 'react';

export interface UseTbmReportResult {
  /** 현재 화면에 표시 중인 보고서(선택한 버전 또는 최신 버전). */
  report: TbmReport | null;
  /** 작업에 속한 보고서 버전 목록(최신순). */
  versions: readonly TbmReportVersionSummary[];
  /** 선택된 보고서 ID. */
  selectedReportId: number | null;
  /** 보고서 생성/폴링 진행 중 여부. */
  isGenerating: boolean;
  /** POST /api/reports/tbm 요청 실패(409 WORK_HAS_NO_LAYOUT, REPORT_IN_PROGRESS 등) 안내 문구. */
  requestError: string | null;
  /** 보고서 버전 선택. */
  selectVersion: (reportId: number) => void;
  /** 보고서 생성/재생성 요청(POST /api/reports/tbm). */
  generate: () => Promise<void>;
  /** 안내 문구 초기화. */
  clearError: () => void;
}

interface ScopedReportState {
  workId: string | undefined;
  selectedReportId: number | null;
  pollingReport: TbmReport | null;
  requestError: string | null;
}

/**
 * 선택된 작업(workId)의 TBM 보고서 조회 · 버전 전환 · 생성 요청(202) · 2초 폴링 수명주기 훅.
 */
export function useTbmReport(
  workId: string | undefined,
  enabled: boolean = true,
): UseTbmReportResult {
  const queryClient = useQueryClient();
  const [scoped, setScoped] = useState<ScopedReportState>({
    workId,
    selectedReportId: null,
    pollingReport: null,
    requestError: null,
  });

  // workId 가 바뀌면 부수효과(useEffect) 없이 파생 값을 바로 비운다.
  const isSameWork = scoped.workId === workId;
  const selectedReportId = isSameWork ? scoped.selectedReportId : null;
  const pollingReport = isSameWork ? scoped.pollingReport : null;
  const requestError = isSameWork ? scoped.requestError : null;

  const byWorkQuery = useQuery({
    queryKey: tbmReportsByWorkKey(workId ?? ''),
    queryFn: ({ signal }) => requestTbmReportsByWork(api, workId!, signal),
    enabled: enabled && Boolean(workId),
    retry: false,
  });

  const latest = byWorkQuery.data?.latest ?? null;
  const versions = byWorkQuery.data?.versions ?? [];

  // 사용자가 과거 버전을 골랐을 때 단건 조회
  const targetVersionId =
    selectedReportId !== null && selectedReportId !== latest?.report_id ? selectedReportId : null;

  const versionDetailQuery = useQuery({
    queryKey: tbmReportDetailKey(targetVersionId ?? 0),
    queryFn: ({ signal }) => requestTbmReportById(api, targetVersionId!, signal),
    enabled: enabled && targetVersionId !== null,
    retry: false,
  });

  const activeReport: TbmReport | null =
    pollingReport && pollingReport.work_id === workId
      ? pollingReport
      : targetVersionId !== null
        ? (versionDetailQuery.data ?? latest)
        : latest;

  // 서버에서 받은 latest 자체가 GENERATING 인 경우에도 2초 폴링 대상에 포함한다.
  const generatingReportId =
    pollingReport?.status === 'GENERATING'
      ? pollingReport.report_id
      : latest?.status === 'GENERATING'
        ? latest.report_id
        : null;

  const pollOnce = useCallback(
    async (reportId: number) => {
      try {
        const polled = await requestTbmReportById(api, reportId);
        setScoped((prev) => ({
          workId,
          selectedReportId:
            polled.status !== 'GENERATING'
              ? polled.report_id
              : prev.workId === workId
                ? prev.selectedReportId
                : null,
          pollingReport: polled,
          requestError: prev.workId === workId ? prev.requestError : null,
        }));
        if (polled.status !== 'GENERATING' && workId) {
          await queryClient.invalidateQueries({ queryKey: tbmReportsByWorkKey(workId) });
        }
      } catch {
        // 폴링 중 일시 오류는 다음 2초 주기에서 다시 확인한다.
      }
    },
    [queryClient, workId],
  );

  useLayoutEffect(() => {
    if (!enabled || generatingReportId === null) return;
    const timer = window.setInterval(() => {
      void pollOnce(generatingReportId);
    }, TBM_REPORT_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, generatingReportId, pollOnce]);

  const generate = useCallback(async () => {
    if (!workId) return;
    setScoped((prev) => ({
      workId,
      selectedReportId: prev.workId === workId ? prev.selectedReportId : null,
      pollingReport: prev.workId === workId ? prev.pollingReport : null,
      requestError: null,
    }));
    try {
      const accepted = await requestGenerateTbmReport(api, workId);
      const placeholder: TbmReport = {
        report_id: accepted.report_id,
        work_id: accepted.work_id,
        report_version: accepted.report_version,
        status: accepted.status,
        failure_reason: null,
        layout_id: latest?.layout_id ?? null,
        layout_revision: latest?.layout_revision ?? null,
        work_type: latest?.work_type ?? null,
        header: latest?.header ?? null,
        rule_version: latest?.rule_version ?? null,
        risk_items: [],
        zone_history: [],
        sections: [],
        citations: [],
        subgraph: { nodes: [], edges: [] },
        steps: [],
        llm_model: null,
        created_at_ms: Date.now(),
        completed_at_ms: null,
      };
      setScoped({
        workId,
        selectedReportId: accepted.report_id,
        pollingReport: placeholder,
        requestError: null,
      });
      if (accepted.status !== 'GENERATING') {
        await pollOnce(accepted.report_id);
      }
    } catch (err) {
      setScoped((prev) => ({
        workId,
        selectedReportId: prev.workId === workId ? prev.selectedReportId : null,
        pollingReport: prev.workId === workId ? prev.pollingReport : null,
        requestError: tbmReportRequestErrorText(err),
      }));
    }
  }, [workId, latest, pollOnce]);

  const selectVersion = useCallback(
    (reportId: number) => {
      setScoped({
        workId,
        selectedReportId: reportId,
        pollingReport: null,
        requestError: null,
      });
    },
    [workId],
  );

  const clearError = useCallback(() => {
    setScoped((prev) => ({
      workId,
      selectedReportId: prev.workId === workId ? prev.selectedReportId : null,
      pollingReport: prev.workId === workId ? prev.pollingReport : null,
      requestError: null,
    }));
  }, [workId]);

  const isGenerating = generatingReportId !== null || activeReport?.status === 'GENERATING';

  return {
    report: activeReport,
    versions,
    selectedReportId: activeReport?.report_id ?? selectedReportId,
    isGenerating,
    requestError,
    selectVersion,
    generate,
    clearError,
  };
}
