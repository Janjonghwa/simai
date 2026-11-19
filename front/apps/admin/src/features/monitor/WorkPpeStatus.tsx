import { useMonitoringPhase } from '@/features/monitor/useMonitoringPhase';
import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import {
  activePpeRequirement,
  PPE_OFF_TEXT,
  ppeOffDetail,
  ppeStatusText,
  ruleProfileText,
} from '@/features/monitor/workPpeText';
import { usePpeReceipt, useSustained } from '@/features/ppe/usePpeReceipt';
import { useRecentBundles } from '@/features/zones/useRecentBundles';
import { cn } from '@simai/ui/lib/utils';
import { useState } from 'react';

/** 관제가 켜진 뒤 PPE 상태가 이만큼(ms) 계속 없어야 「보호구 감시 꺼짐」으로 바꾼다(PPE 첫 수신·순간 끊김은 알리지 않는다, -634). */
export const PPE_OFF_SUSTAIN_MS = 15_000;

const BADGE =
  'pointer-events-auto flex max-w-[520px] flex-col rounded-[12px] px-3 py-1.5 text-[13px] shadow-float ring-1 ring-inset backdrop-blur-[8px]';

/** 정상 상태는 세션 바 설명에 두고, 준비 지연이나 수신 중단이 지속될 때만 표시한다. */
export function WorkPpeStatus() {
  const works = useMonitorWorks({ passive: true });
  const bundles = useRecentBundles().data;
  const phase = useMonitoringPhase();
  const [open, setOpen] = useState(false);
  const active = works.state === 'ready' ? works.active : null;
  const requirement = active ? activePpeRequirement(active, bundles) : null;
  const watched =
    requirement !== null && (requirement.helmet === 'REQUIRED' || requirement.vest === 'REQUIRED');
  const watching = watched && phase.stage === 'on' && phase.problem === null;
  const receipt = usePpeReceipt(watching);
  const off = useSustained(watching && receipt === 'stale', PPE_OFF_SUSTAIN_MS);
  const preparing = useSustained(watched && phase.stage === 'starting', PPE_OFF_SUSTAIN_MS);
  if (!active || !requirement || (!off && !preparing)) return null;

  const profile = ruleProfileText(bundles);
  const text = off ? PPE_OFF_TEXT : ppeStatusText(requirement, phase.stage === 'on');
  const detail = [...(off ? ppeOffDetail(requirement) : []), ...(profile ? [profile] : [])];
  return (
    <section
      aria-label="보호구 감시"
      title={detail.length > 0 ? detail.join('\n') : undefined}
      className={cn(
        BADGE,
        off ? 'bg-muted/94 text-muted-foreground ring-border' : 'bg-card/94 ring-border',
      )}
    >
      <div className="flex items-center gap-2">
        <b role="status" className="font-semibold">
          {text}
        </b>
        {detail.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="text-[12px] text-muted-foreground underline-offset-2 hover:underline"
          >
            {open ? '접기' : '자세히'}
          </button>
        )}
      </div>
      {open && detail.length > 0 && (
        <ul aria-label="보호구 감시 자세히" className="mt-1 flex flex-col gap-0.5">
          {detail.map((line) => (
            <li key={line} className="text-muted-foreground">
              {line}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
