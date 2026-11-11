import { ApiError } from '@/lib/http';

export const SETUP_LIVE_WORK_BLOCKED_TEXT =
  '진행 중인 작업이 있어 세팅을 시작할 수 없습니다. 작업을 종료한 뒤 다시 시도하세요';

export const SETUP_WORKER_UNAVAILABLE_TEXT =
  'GPU 서버(분석 워커)가 연결돼 있지 않아 세팅을 시작할 수 없습니다. 서버 연결을 확인한 뒤 다시 시도하세요';

/** 세팅 시작·재시도가 진행 중 작업(409 SETUP_BLOCKED_BY_LIVE_WORK) 때문에 거절됐는지 판정한다. */
export function isSetupBlockedByLiveWork(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'SETUP_BLOCKED_BY_LIVE_WORK';
}

/** 서버 409 응답 본문에 진행 중 작업 식별자(work_id)가 실려 있으면 돌려준다. */
export function blockedLiveWorkId(error: unknown): number | null {
  if (!isSetupBlockedByLiveWork(error) || !(error instanceof ApiError)) return null;
  return error.workId ?? null;
}

/** 세팅 시작(POST /api/setup-sessions)·재시도·취소 실패를 화면 안내 문구로 바꾼다. */
export function setupErrorMessage(error: unknown): string {
  if (isSetupBlockedByLiveWork(error)) {
    const workName = error instanceof ApiError ? error.workName?.trim() : undefined;
    return workName
      ? `진행 중인 작업(${workName})이 있어 세팅을 시작할 수 없습니다. 작업을 종료한 뒤 다시 시도하세요`
      : SETUP_LIVE_WORK_BLOCKED_TEXT;
  }
  if (error instanceof ApiError && error.code === 'SETUP_WORKER_UNAVAILABLE') {
    return SETUP_WORKER_UNAVAILABLE_TEXT;
  }
  if (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.serverMessage
  ) {
    return error.serverMessage;
  }
  return '현장 세팅을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요';
}
