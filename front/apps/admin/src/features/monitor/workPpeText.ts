import { appliedBundle } from '@/features/events/ppePolicy';
import type { TbmWorkUnit } from '@/features/tbm/types';
import type { ConfigBundle } from '@/features/zones/types';

// 관제 머리줄의 작업·보호구·규칙 기준 표시 계산(S15P31A101-654). 화면 없이 시험한다. 서버가 준 값만 쓴다.

type Requirement = 'REQUIRED' | 'NOT_REQUIRED';

export interface PpeRequirement {
  helmet: Requirement;
  vest: Requirement;
}

/**
 * 진행 중 작업의 안전모·조끼 요구. 현장 Tier 0 가 쓰는(적용된) 묶음의 ppe_policy 가 이 작업의 것이면 그 값, 아니면(시작 묶음 적용 대기)
 * 서버가 이 작업에 저장한 값(work_unit.helmet·vest)이다. 이전 서버라 작업에 칸이 없으면 서버 기본값(필수)이다.
 * 적용이 늦거나 거부된 것은 관제 단계(WorkSessionBar)가 알린다.
 */
export function activePpeRequirement(
  work: Pick<TbmWorkUnit, 'work_id' | 'helmet' | 'vest'>,
  bundles: readonly ConfigBundle[] | undefined,
): PpeRequirement {
  const policy = bundles ? appliedBundle(bundles)?.ppe_policy : undefined;
  if (policy && policy.work_id !== null && String(policy.work_id) === work.work_id) {
    return { helmet: policy.helmet, vest: policy.vest ?? 'NOT_REQUIRED' };
  }
  return { helmet: work.helmet ?? 'REQUIRED', vest: work.vest ?? 'REQUIRED' };
}

/** 이번 작업 묶음이 현장에 적용되기 전(S15P31A101-692). 작업 시작 직후 바로 보인다. */
export const PPE_PREPARING_TEXT = '보호구 감시 준비 중';

const GEAR_NAME = { helmet: '안전모', vest: '조끼' } as const;

/**
 * 관제 머리줄의 보호구 글(S15P31A101-692). 작업이 끈 장비는 「감시 대상 아님」이다.
 * - 현장 적용 전(on 아님): 「보호구 감시 준비 중」(끈 장비가 있으면 「· 조끼 감시 대상 아님」을 붙인다)
 * - 적용 뒤: 「안전모·조끼 감시 중」·「안전모 감시 중 · 조끼 감시 대상 아님」·「보호구 감시 대상 아님」
 */
export function ppeStatusText({ helmet, vest }: PpeRequirement, on: boolean): string {
  const watched = (['helmet', 'vest'] as const).filter((gear) =>
    gear === 'helmet' ? helmet === 'REQUIRED' : vest === 'REQUIRED',
  );
  const off = (['helmet', 'vest'] as const).filter((gear) => !watched.includes(gear));
  if (watched.length === 0) return '보호구 감시 대상 아님';
  const offText =
    off.length > 0 ? ` · ${off.map((g) => GEAR_NAME[g]).join('·')} 감시 대상 아님` : '';
  if (!on) return `${PPE_PREPARING_TEXT}${offText}`;
  return `${watched.map((g) => GEAR_NAME[g]).join('·')} 감시 중${offText}`;
}

/** R2·R3 고소 기준 높이(명세 7.2: 운영 v1 2.0 m, 교실 시험 v1001 0.35 m). */
const RULE_PROFILE_TEXT: Record<string, string> = {
  OPERATION: '운영 기준 · 2.0m',
  CLASSROOM_TEST: '교실 시험 기준 · 0.35m',
};

/**
 * 현장이 쓰는 규칙 기준 표시. 적용된 묶음의 rule_profile 을 보이고, 더 새 묶음이 다른 기준으로 적용 대기면 「적용 대기」를 붙인다.
 * 적용 묶음을 모르거나 모르는 값이면 null(그리지 않는다).
 */
export function ruleProfileText(bundles: readonly ConfigBundle[] | undefined): string | null {
  if (!bundles) return null;
  const applied = appliedBundle(bundles);
  const text = applied?.rule_profile ? RULE_PROFILE_TEXT[applied.rule_profile] : undefined;
  if (!text) return null;
  const latest = bundles[0];
  const pending =
    latest !== undefined &&
    latest !== applied &&
    latest.status === 'PENDING' &&
    latest.rule_profile != null &&
    latest.rule_profile !== applied?.rule_profile;
  return pending ? `${text} (바꾼 기준 적용 대기)` : text;
}
