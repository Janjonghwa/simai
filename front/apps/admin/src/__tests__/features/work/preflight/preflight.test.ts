import { SignatureUploadError, WORK_ERROR } from '@/features/work/api';
import {
  canAddSigner,
  canStart,
  isSetupFinished,
  isSignerLimitReached,
  NO_CHECKS,
  withCheck,
} from '@/features/work/preflight/preflight';
import {
  addSignatureErrorMessage,
  deleteSignatureErrorMessage,
  SIGNER_LIMIT_MESSAGE,
  startErrorMessage,
} from '@/features/work/preflight/preflightErrors';
import { DAY_PROGRESS, SIGNATURE_MAX_COUNT } from '@/features/work/types';
import { ApiError } from '@/lib/http';
import { describe, expect, it } from 'vitest';

describe('isSetupFinished', () => {
  it('서버 진행이 SETUP_DONE 이거나 그 뒤(작업 중·종료·제출)일 때만 참이다', () => {
    const finished = DAY_PROGRESS.filter((progress) => isSetupFinished(progress));
    expect(finished).toEqual(['SETUP_DONE', 'IN_PROGRESS', 'ENDED', 'SUBMITTED']);
  });

  it('진행 값이 없으면 거짓이다', () => {
    expect(isSetupFinished(null)).toBe(false);
  });
});

describe('canStart', () => {
  it('두 체크가 모두 켜져 있고 서명이 1명 이상일 때만 시작할 수 있다', () => {
    expect(canStart({ reportChecked: true, layoutChecked: true, signerCount: 1 })).toBe(true);
    expect(canStart({ reportChecked: true, layoutChecked: true, signerCount: 12 })).toBe(true);
  });

  it.each([
    { reportChecked: false, layoutChecked: true, signerCount: 1 },
    { reportChecked: true, layoutChecked: false, signerCount: 1 },
    { reportChecked: true, layoutChecked: true, signerCount: 0 },
    { reportChecked: false, layoutChecked: false, signerCount: 0 },
  ])('하나라도 모자라면 시작할 수 없다 %j', (input) => {
    expect(canStart(input)).toBe(false);
  });
});

describe('withCheck', () => {
  it('체크 하나만 바꾼 새 상태를 돌려주고 원래 상태는 그대로 둔다', () => {
    const next = withCheck(NO_CHECKS, 'reportChecked', true);
    expect(next).toEqual({ reportChecked: true, layoutChecked: false });
    expect(NO_CHECKS).toEqual({ reportChecked: false, layoutChecked: false });
    expect(withCheck(next, 'layoutChecked', true)).toEqual({
      reportChecked: true,
      layoutChecked: true,
    });
    expect(withCheck(next, 'reportChecked', false).reportChecked).toBe(false);
  });
});

describe('canAddSigner', () => {
  const ok = { name: '홍길동', hasInk: true, ready: true, busy: false, signerCount: 0 };

  it('이름과 획이 있고 보고서가 있고 요청 중이 아니면 추가할 수 있다', () => {
    expect(canAddSigner(ok)).toBe(true);
  });

  it('이름이 비었거나 공백뿐이면 추가할 수 없다', () => {
    expect(canAddSigner({ ...ok, name: '' })).toBe(false);
    expect(canAddSigner({ ...ok, name: '   ' })).toBe(false);
  });

  it('획이 없으면 추가할 수 없다', () => {
    expect(canAddSigner({ ...ok, hasInk: false })).toBe(false);
  });

  it('보고서가 아직 없거나 요청 중이면 추가할 수 없다', () => {
    expect(canAddSigner({ ...ok, ready: false })).toBe(false);
    expect(canAddSigner({ ...ok, busy: true })).toBe(false);
  });

  it('인원 상한(50명)에 닿으면 추가할 수 없다', () => {
    expect(canAddSigner({ ...ok, signerCount: SIGNATURE_MAX_COUNT - 1 })).toBe(true);
    expect(canAddSigner({ ...ok, signerCount: SIGNATURE_MAX_COUNT })).toBe(false);
    expect(isSignerLimitReached(SIGNATURE_MAX_COUNT)).toBe(true);
    expect(isSignerLimitReached(SIGNATURE_MAX_COUNT - 1)).toBe(false);
  });
});

const httpError = (code: string, status = 409) => new ApiError(status, { code });

describe('startErrorMessage', () => {
  it('오늘 작업이 아니면·서명이 없으면·진행 중인 다른 작업이 있으면·시작할 수 없는 상태면 각각 알려 주고, 그 밖은 일반 글이다', () => {
    expect(startErrorMessage(httpError(WORK_ERROR.NOT_TODAY))).toBe('오늘 작업만 시작할 수 있어요');
    expect(startErrorMessage(httpError(WORK_ERROR.NO_SIGNATURE))).toBe('참석자 서명이 필요합니다');
    expect(startErrorMessage(httpError(WORK_ERROR.ALREADY_IN_PROGRESS))).toBe(
      '진행 중인 다른 작업이 있어요',
    );
    expect(startErrorMessage(httpError(WORK_ERROR.INVALID_STATE))).toBe(
      '지금은 작업을 시작할 수 없어요. 세팅이 끝났는지 확인해 주세요.',
    );
    expect(startErrorMessage(httpError(WORK_ERROR.REPORT_LOCKED))).toBe('시작하지 못했어요');
    expect(startErrorMessage(new ApiError(0))).toBe('시작하지 못했어요');
    expect(startErrorMessage(new Error('x'))).toBe('시작하지 못했어요');
  });
});

describe('addSignatureErrorMessage', () => {
  it('인원 상한·이미지 크기·올리기 실패·판 바뀜·잠금을 가른다', () => {
    expect(addSignatureErrorMessage(httpError(WORK_ERROR.SIGNATURE_LIMIT_REACHED))).toBe(
      SIGNER_LIMIT_MESSAGE,
    );
    expect(addSignatureErrorMessage(new SignatureUploadError('TOO_LARGE'))).toBe(
      '서명이 너무 커요. 지우고 다시 써 주세요',
    );
    expect(addSignatureErrorMessage(new SignatureUploadError('UPLOAD_FAILED'))).toBe(
      '서명을 올리지 못했어요',
    );
    expect(addSignatureErrorMessage(httpError(WORK_ERROR.REPORT_VERSION_STALE))).toBe(
      '보고서가 새로 바뀌었어요. 다시 서명해 주세요',
    );
    expect(addSignatureErrorMessage(httpError(WORK_ERROR.REPORT_LOCKED))).toBe(
      '제출한 보고서에는 서명할 수 없어요',
    );
    expect(addSignatureErrorMessage(new ApiError(0))).toBe('서명을 추가하지 못했어요');
  });

  it('상한 안내에 50명이 들어 있다', () => {
    expect(SIGNER_LIMIT_MESSAGE).toContain(String(SIGNATURE_MAX_COUNT));
  });
});

describe('deleteSignatureErrorMessage', () => {
  it('작업을 시작했거나 제출해서 막힌 이유를 알리고, 그 밖은 일반 글이다', () => {
    expect(deleteSignatureErrorMessage(httpError(WORK_ERROR.INVALID_STATE))).toBe(
      '작업을 시작해서 서명을 지울 수 없어요',
    );
    expect(deleteSignatureErrorMessage(httpError(WORK_ERROR.REPORT_LOCKED))).toBe(
      '제출한 보고서의 서명은 지울 수 없어요',
    );
    expect(deleteSignatureErrorMessage(httpError('SIGNATURE_NOT_FOUND', 404))).toBe(
      '서명을 지우지 못했어요',
    );
  });
});
