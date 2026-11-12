import { useMonitorWorks } from '@/features/monitor/useMonitorWorks';
import {
  activePpeRequirement,
  IDLE_TEXT,
  ppeRequirementText,
  ruleProfileText,
  START_HINT_TEXT,
} from '@/features/monitor/workPpeText';
import { useRecentBundles } from '@/features/zones/useRecentBundles';
import { selectTrackedCount, useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';

const BADGE =
  'pointer-events-auto flex max-w-[680px] flex-wrap items-center gap-x-2 gap-y-1 rounded-[12px] bg-card/94 px-4 py-2 text-sm shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset';

/**
 * 관제 머리줄의 작업 상태 배지(S15P31A101-654). 보호구 판정(R9·R11)은 작업 시작 뒤에만 켜지므로 관제만 보고 있으면 꺼진 줄 모른다
 * (14차 로비). 서버 상태(진행 중 작업, 현장이 쓰는 묶음의 ppe_policy·rule_profile)로 보인다.
 * - 작업 없음: 「작업 시작 전 · 보호구 판정 꺼짐」, 사람이 보이면 작업 시작 안내와 「작업 관리로」
 * - 작업 중: 「작업 진행 중 · 안전모·조끼 필수」(작업이 끈 장비는 빼고)
 * 뒤에 현재 규칙 기준(「교실 시험 기준 · 0.35m」 등)을 붙인다. 작업 목록을 모르면 그리지 않는다.
 */
export function WorkPpeStatus() {
  const works = useMonitorWorks({ passive: true });
  const bundles = useRecentBundles().data;
  const tracked = useRealtimeStore(selectTrackedCount);
  const navigate = useNavigate();
  if (works.state !== 'ready') return null;
  const active = works.active;
  const profile = ruleProfileText(bundles);
  const text = active
    ? `작업 진행 중 · ${ppeRequirementText(activePpeRequirement(active, bundles))}`
    : IDLE_TEXT;
  return (
    <section aria-label="작업 상태" className={BADGE}>
      <span
        aria-hidden
        className={cn('size-2 shrink-0 rounded-full', active ? 'bg-ok' : 'bg-muted-foreground')}
      />
      <b role="status">{text}</b>
      {profile && <span className="text-muted-foreground">· {profile}</span>}
      {!active && tracked > 0 && (
        <>
          <span className="w-full text-sev-caution">{START_HINT_TEXT}</span>
          <Button
            size="sm"
            variant="secondary"
            className="shrink-0"
            onClick={() => void navigate({ to: '/work' })}
          >
            작업 관리로
          </Button>
        </>
      )}
    </section>
  );
}
