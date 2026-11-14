import { useMonitoringPhase } from '@/features/monitor/useMonitoringPhase';
import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import {
  activePpeRequirement,
  ppeStatusText,
  ruleProfileText,
} from '@/features/monitor/workPpeText';
import { useRecentBundles } from '@/features/zones/useRecentBundles';

const BADGE =
  'pointer-events-auto flex max-w-[680px] flex-wrap items-center gap-x-2 gap-y-1 rounded-[12px] bg-card/94 px-4 py-2 text-sm shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset';

/**
 * 관제 머리줄의 보호구 줄(S15P31A101-654, -692). 진행 중 작업이 있을 때만 보인다(작업 전에는 「작업 시작 전 · 경보 꺼짐」이 맡는다).
 * 이번 작업 묶음이 현장에 적용되기 전에는 바로 「보호구 감시 준비 중」, 적용되면 「안전모·조끼 감시 중」이고, 작업이 끈 장비는
 * 「감시 대상 아님」이다. 뒤에 현재 규칙 기준(「교실 시험 기준 · 0.35m」 등)을 붙인다. 작업 목록을 모르면 그리지 않는다.
 */
export function WorkPpeStatus() {
  const works = useMonitorWorks({ passive: true });
  const bundles = useRecentBundles().data;
  const phase = useMonitoringPhase();
  if (works.state !== 'ready' || !works.active) return null;
  const profile = ruleProfileText(bundles);
  const text = ppeStatusText(activePpeRequirement(works.active, bundles), phase.stage === 'on');
  return (
    <section aria-label="보호구 감시" className={BADGE}>
      <b role="status">{text}</b>
      {profile && <span className="text-muted-foreground">· {profile}</span>}
    </section>
  );
}
