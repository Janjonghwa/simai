import { isWorkError, SignatureUploadError, WORK_ERROR } from '@/features/work/api';
import { SIGNATURE_MAX_COUNT } from '@/features/work/types';

// 작업 전 확인의 실패 안내 글. 서버가 준 문구는 보이지 않고 오류 코드로 가른다.

/** 작업 시작이 거절됐을 때. */
export function startErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.NOT_TODAY)) return '오늘 작업만 시작할 수 있어요';
  if (isWorkError(error, WORK_ERROR.SIGNATURE_REQUIRED)) return '참석자 서명이 필요합니다';
  if (isWorkError(error, WORK_ERROR.ALREADY_IN_PROGRESS)) return '진행 중인 다른 작업이 있어요';
  if (isWorkError(error, WORK_ERROR.INVALID_STATE)) {
    return '지금은 작업을 시작할 수 없어요. 세팅이 끝났는지 확인해 주세요.';
  }
  return '시작하지 못했어요';
}

/** 서명 인원이 상한에 닿았을 때(서버가 거절했거나 목록이 이미 가득 찼을 때). */
export const SIGNER_LIMIT_MESSAGE = `서명은 ${SIGNATURE_MAX_COUNT}명까지 받을 수 있어요`;

/** 서명 추가가 실패했을 때: 인원 상한·이미지 크기·올리기 실패·보고서 판 바뀜·제출 잠금, 그 밖. */
export function addSignatureErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.SIGNATURE_LIMIT_REACHED)) return SIGNER_LIMIT_MESSAGE;
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

/** 서명 삭제가 실패했을 때: 작업을 시작했거나 제출해서 막힌 경우와 그 밖. */
export function deleteSignatureErrorMessage(error: unknown): string {
  if (isWorkError(error, WORK_ERROR.INVALID_STATE)) {
    return '작업을 시작해서 서명을 지울 수 없어요';
  }
  if (isWorkError(error, WORK_ERROR.REPORT_LOCKED)) return '제출한 보고서의 서명은 지울 수 없어요';
  return '서명을 지우지 못했어요';
}
