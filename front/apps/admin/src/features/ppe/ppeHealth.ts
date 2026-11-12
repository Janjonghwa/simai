import { appliedBundle } from '@/features/events/ppePolicy';
import type { ConfigBundle } from '@/features/zones/types';
import type { PpeViewPayload } from '@simai/contracts';

// PPE 가 조용히 꺼진 상태를 관제 화면이 알리기 위한 계산(S15P31A101-634). 경보·판정에는 쓰지 않는다(안내).

/**
 * EC2 가 PPE 상태(ppe_state)를 이만큼 받지 못하면 'PPE 상태 수신 없음'이다. PPE 는 0.5초마다 보내고 상태 근거 만료가 10초다.
 * 두 시각 모두 EC2 시계라(snapshot_at_ms − last_received_at_ms) 브라우저 시계 차이와 무관하다.
 */
export const PPE_RECEIPT_STALE_MS = 10_000;

/**
 * 진행 중인 작업에서 PPE 상태가 들어오고 있는가.
 * - 'ok': 어떤 Tier 0 든 최근 10초 안에 받았다
 * - 'stale': EC2 가 수신 시각을 알려 주는데(ppe_view 1.1.0) 받은 적이 없거나 오래됐다, 또는 스냅샷이 하나도 없다(PPE 가 한 번도 안 보냄)
 * - 'unknown': 응답을 아직 못 받았거나 이전 EC2 라 수신 시각을 모른다(안내하지 않는다)
 */
export type PpeReceipt = 'ok' | 'stale' | 'unknown';

export function ppeReceipt(payloads: readonly PpeViewPayload[] | undefined): PpeReceipt {
  if (payloads === undefined) return 'unknown';
  if (payloads.length === 0) return 'stale';
  let knows = false;
  for (const p of payloads) {
    if (p.last_received_at_ms === undefined) continue; // 이전 EC2: 모른다
    knows = true;
    if (
      p.last_received_at_ms !== null &&
      p.snapshot_at_ms - p.last_received_at_ms <= PPE_RECEIPT_STALE_MS
    )
      return 'ok';
  }
  return knows ? 'stale' : 'unknown';
}

/**
 * 현장 Tier 0 가 지금 쓰는(가장 새로 적용된) 묶음의 PPE 요구가 작업 상태와 어긋나는가. 작업 시작·종료 묶음이 적용 대기·거부면 생긴다.
 * - 'not_applied': 작업이 진행 중인데 안전모 요구가 REQUIRED 가 아니거나 다른 작업의 요구다 → R9·R11 이 나지 않는다
 * - 'not_released': 진행 중인 작업이 없는데 아직 REQUIRED 다 → R9·R11 이 계속 나고 워치가 울린다
 * - null: 맞거나, 적용 묶음·요약을 모른다(ppe_policy 가 아예 없는 묶음은 기존 'PPE 요구 미수신' 안내가 맡는다)
 */
export type PpeRequirementMismatch = 'not_applied' | 'not_released' | null;

export function ppeRequirementMismatch(
  bundles: readonly ConfigBundle[] | undefined,
  activeWorkId: string | null,
): PpeRequirementMismatch {
  const policy = bundles ? appliedBundle(bundles)?.ppe_policy : undefined;
  if (!policy) return null;
  const required = policy.helmet === 'REQUIRED';
  if (activeWorkId !== null) {
    return required && String(policy.work_id) === activeWorkId ? null : 'not_applied';
  }
  return required ? 'not_released' : null;
}
