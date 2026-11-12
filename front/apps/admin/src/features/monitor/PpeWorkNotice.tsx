import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import { ppeRequirementMismatch } from '@/features/ppe/ppeHealth';
import { usePpeReceipt, useSustained } from '@/features/ppe/usePpeReceipt';
import { useRecentBundles } from '@/features/zones/useRecentBundles';

/** 안내 제목(S15P31A101-634). */
export const PPE_RECEIPT_STALE_TEXT = 'PPE 상태 수신 없음';
export const PPE_NOT_APPLIED_TEXT = 'PPE 요구 미적용';
export const PPE_NOT_RELEASED_TEXT = 'PPE 요구 해제 대기';

/** 작업 시작·종료 직후 묶음 적용·첫 수신을 기다리는 시간(ms). 이만큼 이어져야 안내한다 */
export const PPE_NOTICE_SUSTAIN_MS = 15_000;

const NOTICE =
  'pointer-events-auto rounded-[12px] bg-card/94 px-4 py-2 text-sm shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset';

/**
 * PPE 경보(R9·R11)가 조용히 나지 않거나 그치지 않는 상태를 알린다(S15P31A101-634). 경보가 아니라 안내다.
 * - 작업 중인데 EC2 가 PPE 상태를 10초 넘게 받지 못했다(PPE 프로세스가 안 떴거나 멈춤)
 * - 작업 중인데 현장이 쓰는 묶음이 안전모·조끼를 요구하지 않는다(시작 묶음 적용 대기·거부)
 * - 작업이 없는데 현장이 아직 요구한다(종료 묶음 적용 대기·거부)
 * 작업 목록·묶음을 모르면 그리지 않는다. ppe_policy 가 아예 없는 묶음은 PpePolicyNotice 가 맡는다.
 */
export function PpeWorkNotice() {
  const works = useMonitorWorks({ passive: true });
  const bundles = useRecentBundles().data;
  const known = works.state === 'ready';
  const activeId = known && works.active ? String(works.active.work_id) : null;
  const receipt = usePpeReceipt(activeId !== null);
  const mismatch = known ? ppeRequirementMismatch(bundles, activeId) : null;
  const showReceipt = useSustained(activeId !== null && receipt === 'stale', PPE_NOTICE_SUSTAIN_MS);
  const showNotApplied = useSustained(mismatch === 'not_applied', PPE_NOTICE_SUSTAIN_MS);
  const showNotReleased = useSustained(mismatch === 'not_released', PPE_NOTICE_SUSTAIN_MS);
  if (!showReceipt && !showNotApplied && !showNotReleased) return null;
  return (
    <>
      {showReceipt && (
        <p role="status" className={NOTICE}>
          <b>{PPE_RECEIPT_STALE_TEXT}</b>
          <span className="text-muted-foreground">
            {' · 안전모·조끼 미착용(R9·R11)이 나지 않습니다. PPE 프로세스가 떠 있는지 확인하세요'}
          </span>
        </p>
      )}
      {showNotApplied && (
        <p role="status" className={NOTICE}>
          <b>{PPE_NOT_APPLIED_TEXT}</b>
          <span className="text-muted-foreground">
            {
              ' · 작업 중인데 현장이 안전모·조끼를 요구하지 않아 R9·R11 이 나지 않습니다(구성 묶음 적용 대기·거부)'
            }
          </span>
        </p>
      )}
      {showNotReleased && (
        <p role="status" className={NOTICE}>
          <b>{PPE_NOT_RELEASED_TEXT}</b>
          <span className="text-muted-foreground">
            {' · 작업이 끝났는데 현장이 아직 안전모·조끼를 요구합니다(구성 묶음 적용 대기·거부)'}
          </span>
        </p>
      )}
    </>
  );
}
