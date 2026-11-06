import { ppePolicyMissing } from '@/features/events/ppePolicy';
import { useRecentBundles } from '@/features/zones/useRecentBundles';

/** 안내 문구. 명세 7.2 R9 의 표시 이름이다. */
export const PPE_POLICY_MISSING_TEXT = 'PPE 요구 미수신';

/**
 * 실시간 관제 알림 자리의 "PPE 요구 미수신" 안내(명세 7.2 R9, S15P31A101-520). 현장이 적용 중인 구성 묶음에 안전모 요구
 * (ppe_policy)가 없으면 Tier 0 는 요구 없음으로 보고 R9 를 내지 않는다. 작업 여부와 관계없이 그 사실만 알린다.
 * 적용 묶음을 모르면 그리지 않는다.
 */
export function PpePolicyNotice() {
  const bundles = useRecentBundles().data;
  if (!ppePolicyMissing(bundles)) return null;
  return (
    <p
      role="status"
      className="pointer-events-auto rounded-[12px] bg-card/94 px-4 py-2 text-sm shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <b>{PPE_POLICY_MISSING_TEXT}</b>
      <span className="text-muted-foreground"> · 안전모 미착용(R9)을 판정하지 않습니다</span>
    </p>
  );
}
