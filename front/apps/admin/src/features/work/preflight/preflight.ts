import { SIGNATURE_MAX_COUNT, type DayProgress } from '@/features/work/types';

// 작업 전 확인의 순수 규칙: 확인창의 체크 상태, '작업 시작'을 켜는 조건, 서명 추가를 켜는 조건.

/** 세팅이 끝난 뒤의 서버 진행 값들(작업 전 확인 이후 어느 때든). */
const SETUP_FINISHED: readonly DayProgress[] = ['SETUP_DONE', 'IN_PROGRESS', 'ENDED', 'SUBMITTED'];

/**
 * 서버가 세팅을 끝냈는가. 보고서 초안은 이때부터 만들어지므로 그 전에는 초안을 기다리며 보고서를 자주 받을 필요가 없다.
 * 이 화면은 세팅 구역 저장 직후 서버 진행이 아직 SETUP 인 채로도 열리므로 화면 단계가 아니라 서버 진행으로 가른다.
 */
export const isSetupFinished = (progress: DayProgress | null): boolean =>
  progress !== null && SETUP_FINISHED.includes(progress);

/** 확인창의 체크 둘: 'TBM 보고서를 확인했습니다'·'가상 레이아웃을 확인했습니다'. */
export interface PreflightChecks {
  reportChecked: boolean;
  layoutChecked: boolean;
}

export type PreflightCheckKey = keyof PreflightChecks;

/** 아무것도 체크하지 않은 처음 상태. */
export const NO_CHECKS: PreflightChecks = { reportChecked: false, layoutChecked: false };

/** 체크 하나를 바꾼 새 상태(원래 객체는 그대로). */
export function withCheck(
  checks: PreflightChecks,
  key: PreflightCheckKey,
  checked: boolean,
): PreflightChecks {
  return { ...checks, [key]: checked };
}

/** 작업을 시작할 수 있는지: 두 체크가 모두 켜져 있고 참석자 서명이 1명 이상. */
export function canStart({
  reportChecked,
  layoutChecked,
  signerCount,
}: PreflightChecks & { signerCount: number }): boolean {
  return reportChecked && layoutChecked && signerCount > 0;
}

/** 서명 인원이 상한(50명)에 닿았는지. */
export const isSignerLimitReached = (signerCount: number): boolean =>
  signerCount >= SIGNATURE_MAX_COUNT;

/**
 * 서명 추가를 켤 수 있는지: 이름(공백 제외)과 획이 있고, 서명할 보고서가 있고, 다른 요청이 없고, 상한에 닿지 않았을 때.
 */
export function canAddSigner({
  name,
  hasInk,
  ready,
  busy,
  signerCount,
}: {
  name: string;
  hasInk: boolean;
  /** 서명을 받을 보고서가 있는지. */
  ready: boolean;
  /** 서명 추가·삭제 요청이 가는 중인지. */
  busy: boolean;
  signerCount: number;
}): boolean {
  return name.trim() !== '' && hasInk && ready && !busy && !isSignerLimitReached(signerCount);
}
