import { isWorkError, SignatureUploadError, WORK_ERROR } from '@/features/work/api';
import { SIGNATURE_MAX_COUNT } from '@/features/work/types';

// 작업 전 확인의 실패 안내 글. 서버가 준 문구는 보이지 않고 오류 코드로 가른다.

/** 작업 시작이 거절됐을 때. */
export function startErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.NOT_TODAY)) return '오늘 작업만 시작할 수 있어요';
  if (isWorkError(error, WORK_ERROR.NO_SIGNATURE)) return '참석자 서명이 필요합니다';
  if (isWorkError(error, WORK_ERROR.ALREADY_IN_PROGRESS)) return '진행 중인 다른 작업이 있어요';
  if (isWorkError(error, WORK_ERROR.SETUP_NOT_READY)) {
    // 현장 단위로 막으므로 멈춘 세팅 하나가 모든 작업 시작을 막을 수 있다. 빠져나갈 길을 함께 알린다(-625 S9)
    return '현장 세팅이 아직 진행 중이에요. 미리보기 맵이 나온 뒤 시작해 주세요. 세팅이 멈췄다면 현장 세팅 단계에서 세팅을 취소한 뒤 다시 하세요';
  }
  if (isWorkError(error, WORK_ERROR.INVALID_STATE)) {
    return '지금은 작업을 시작할 수 없어요. 세팅이 끝났는지 확인해 주세요.';
  }
  return '시작하지 못했어요';
}

/** 서명 인원이 상한에 닿았을 때(서버가 거절했거나 목록이 이미 가득 찼을 때). */
export const SIGNER_LIMIT_MESSAGE = `서명은 ${SIGNATURE_MAX_COUNT}명까지 받을 수 있어요`;

/** 서명 추가가 실패했을 때: 인원 상한·이미지 크기·올리기 실패·서명 값 틀림·보고서 판 바뀜·제출 잠금, 그 밖. */
export function addSignatureErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.SIGNATURE_LIMIT_REACHED)) return SIGNER_LIMIT_MESSAGE;
  if (isWorkError(error, WORK_ERROR.INVALID_SIGNATURE)) {
    return '서명이 올바르지 않아요. 지우고 다시 써 주세요';
  }
  if (error instanceof SignatureUploadError) {
    return error.reason === 'TOO_LARGE'
      ? '서명이 너무 커요. 지우고 다시 써 주세요'
      : '서명을 올리지 못했어요';
  }
  if (isWorkError(error, WORK_ERROR.REPORT_VERSION_STALE)) {
    return '보고서가 새로 바뀌었어요. 다시 서명해 주세요';
  }
  if (isWorkError(error, WORK_ERROR.REPORT_LOCKED)) return '제출한 보고서에는 서명할 수 없어요';
  return '서명을 추가하지 못했어요';
}

/** 서명 삭제가 실패했을 때: 작업을 시작했거나 제출해서 막힌 경우, 서명이 이미 없는 경우, 그 밖. */
export function deleteSignatureErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.INVALID_STATE)) {
    return '작업을 시작해서 서명을 지울 수 없어요';
  }
  if (isWorkError(error, WORK_ERROR.REPORT_LOCKED)) return '제출한 보고서의 서명은 지울 수 없어요';
  if (isWorkError(error, WORK_ERROR.SIGNATURE_NOT_FOUND)) return '이미 지워진 서명이에요';
  return '서명을 지우지 못했어요';
}

/** AI 초안 요청이 실패했을 때: 작업내용 없음·이미 생성 중·제출 잠금, 그 밖(-624). */
export function draftErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.REPORT_FORM_NO_TASKS)) return '작업내용을 먼저 골라 주세요';
  if (isWorkError(error, WORK_ERROR.REPORT_IN_PROGRESS)) return '초안을 이미 만들고 있어요';
  if (isWorkError(error, WORK_ERROR.REPORT_LOCKED))
    return '제출한 보고서에는 초안을 만들 수 없어요';
  return 'AI 초안을 요청하지 못했어요';
}
