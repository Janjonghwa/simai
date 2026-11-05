import { LivePlan } from '@/features/plan/LivePlan';
import { cn } from '@simai/ui/lib/utils';

/**
 * 3D 대신(또는 3D 가 뜨기 전에) 보이는 2D 평면 창. 이벤트 패널이 있는 화면은 왼쪽에 구역 목록 칼럼(왼쪽 14px + 폭 260px)이
 * 있고, 없는 화면(편집 모드)은 구역 편집 패널(왼쪽 14px + 폭 356px)이 있어 그 오른쪽에서 시작한다. 오른쪽은 이벤트 패널이
 * 있으면 그 자리를 비운다.
 */
export function PlanOverlay({
  withEventPanel,
  selectedKey,
  onSelect,
}: {
  withEventPanel: boolean;
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  return (
    <div
      className={cn(
        'absolute top-[72px] bottom-[96px]',
        withEventPanel ? 'right-(--event-panel-inset) left-[290px]' : 'right-5 left-[384px]',
      )}
    >
      <LivePlan selectedKey={selectedKey} onSelect={onSelect} className="size-full" />
    </div>
  );
}
