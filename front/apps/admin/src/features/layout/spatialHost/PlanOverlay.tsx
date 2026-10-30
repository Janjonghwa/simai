import { LivePlan } from '@/features/plan/LivePlan';
import { cn } from '@simai/ui/lib/utils';

/** 3D 대신(또는 3D 가 뜨기 전에) 보이는 2D 평면 창. 이벤트 패널이 없는 화면은 왼쪽에 편집 패널이 있어 오른쪽으로 비킨다. */
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
        withEventPanel ? 'right-[370px] left-5' : 'right-5 left-[384px]',
      )}
    >
      <LivePlan selectedKey={selectedKey} onSelect={onSelect} className="size-full" />
    </div>
  );
}
