import { endWork, startWork, submitReport } from '@/features/work/api';
import { invalidateAfterRun, invalidateWork, mutationResult } from '@/features/work/workQuery';
import { api } from '@/lib/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';

// 작업 시작·종료와 보고서 제출. 성공하면 작업 관리 캐시(달력·하루 작업·보고서·서명)와 히스토리 목록을 다시 받게 한다.
// 변수는 작업 id(문자열) 또는 보고서 id(숫자) 하나다. 요청은 한 번에 하나만 보내도록 화면이 pending 으로 단추를 막는다.

/**
 * 작업 시작. 날짜가 오늘이 아니면 409 WORK_NOT_TODAY, 서명이 없으면 409 SIGNATURE_REQUIRED(임시),
 * 진행 중인 다른 작업이 있으면 409 WORK_ALREADY_IN_PROGRESS(serverMessage 에 그 작업 이름).
 */
export function useStartWork() {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (workId: string) => startWork(api, workId),
    onSuccess: () => invalidateAfterRun(client),
  });
  return mutationResult(mutation);
}

/** 작업 종료. 날짜 작업은 새 판 없이 같은 보고서의 종료 미팅을 서버가 채운다(보고서 wrap_up_status GENERATING → SUCCEEDED, 보고서는 다시 받아 latest 를 쓴다). */
export function useEndWork() {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (workId: string) => endWork(api, workId),
    onSuccess: () => invalidateAfterRun(client),
  });
  return mutationResult(mutation);
}

/** 보고서 제출·잠금. 작업이 끝난 뒤에만(아니면 409 WORK_INVALID_STATE), 이미 제출했으면 409 REPORT_LOCKED. */
export function useSubmitReport() {
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (reportId: number) => submitReport(api, reportId),
    onSuccess: () => invalidateWork(client),
  });
  return mutationResult(mutation);
}
