import type { EventItem } from '@/features/events/types';
import type { ConfigBundle } from '@/features/zones/types';

// R9(작업 중 안전모 미착용)를 구성 묶음의 안전모 요구(ppe_policy)와 맞춰 읽는 계산. 화면 없이 시험한다(S15P31A101-520).
// 요구는 작업 기준이다: 진행 중 작업이 있으면 REQUIRED, 없으면 NOT_REQUIRED(명세 7.2 R9, 8.10).

/** 위치·구역을 보지 않는 사람 단위 규칙. 이벤트에 zone_id 가 없다(명세 7.2 R9). */
export const PERSON_ONLY_RULES: ReadonlySet<string> = new Set(['R9']);

/**
 * 현장 Tier 0 가 지금 쓰는 묶음: 최근 묶음(새것부터) 가운데 가장 새로 적용된(APPLIED) 것. 뒤 묶음이 적용 대기·거부·불일치여도
 * Tier 0 는 그 앞에 적용한 묶음을 계속 쓴다(명세 3.2). 목록에 적용된 묶음이 없으면 모른다(null).
 */
export const appliedBundle = (bundles: readonly ConfigBundle[]): ConfigBundle | null =>
  bundles.find((b) => b.status === 'APPLIED') ?? null;

/**
 * "PPE 요구 미수신": 적용 중인 묶음에 ppe_policy 가 실리지 않았다(null). Tier 0 는 요구 없음으로 보고 R9 를 내지 않는다
 * (명세 7.2 R9). 적용 묶음을 모르거나 서버가 칸을 주지 않으면(undefined) 표시하지 않는다(모르는 것을 지어내지 않는다).
 */
export function ppePolicyMissing(bundles: readonly ConfigBundle[] | undefined): boolean {
  if (!bundles) return false;
  return appliedBundle(bundles)?.ppe_policy === null;
}

/**
 * 해소된 R9 의 해소 원인. 해소 전이의 config_version(서버가 전이마다 갱신한 risk_event.config_version)이 가리키는 묶음의
 * 안전모 요구로 가른다(명세 7.2 R9, docs/design/risk-r9-helmet.md).
 * - WORK_ENDED: 요구 NOT_REQUIRED 묶음(작업 종료) 적용으로 요구가 풀려 닫혔다.
 * - POLICY_MISSING: ppe_policy 가 없는 묶음 적용으로 요구 없음이 돼 닫혔다.
 * - WORN: 요구가 REQUIRED 인 채로 닫혔다. R9 는 착용 관측이나 요구 해제로만 해소된다.
 * 운영자 종료, 아직 열림, LIVE 아님, 묶음을 못 찾음(최근 목록 밖), 요약을 모름은 null 이다(그냥 "해소").
 */
export type R9Release = 'WORK_ENDED' | 'POLICY_MISSING' | 'WORN';

export function r9ReleaseOf(
  event: Pick<EventItem, 'rule_id' | 'source' | 'lifecycle' | 'resolution' | 'config_version'>,
  bundles: readonly ConfigBundle[] | undefined,
): R9Release | null {
  if (event.rule_id !== 'R9' || event.source !== 'LIVE' || event.lifecycle !== 'RESOLVED') {
    return null;
  }
  if (event.resolution || !event.config_version || !bundles) return null;
  const bundle = bundles.find((b) => b.config_version === event.config_version);
  if (!bundle || bundle.ppe_policy === undefined) return null;
  if (bundle.ppe_policy === null) return 'POLICY_MISSING';
  return bundle.ppe_policy.helmet === 'REQUIRED' ? 'WORN' : 'WORK_ENDED';
}

/** 해소 원인 문구. 상세 처리·근거 줄과 타임라인이 "해소" 대신 쓴다. */
export const R9_RELEASE_LABEL: Record<R9Release, string> = {
  WORK_ENDED: '작업 종료로 해소',
  POLICY_MISSING: 'PPE 요구 미수신으로 해소',
  WORN: '착용 확인으로 해소',
};

/** 해소된 LIVE R9 가 있어 해소 원인을 가리려면 묶음 목록이 필요한지. 없으면 묶음을 받지 않는다. */
export const needsBundlesForRelease = (events: readonly EventItem[]) =>
  events.some((e) => e.rule_id === 'R9' && e.source === 'LIVE' && e.lifecycle === 'RESOLVED');
