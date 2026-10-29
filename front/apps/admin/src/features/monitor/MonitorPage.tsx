import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { Button } from '@simai/ui/components/button';

// 3D 장면·2D 비상 평면·이벤트 패널은 AppLayout 의 SpatialViewportHost 가 단일 인스턴스로 유지한다.

function MonitorZoneAction({ onEditZone }: { onEditZone?: (zoneId: string) => void }) {
  const viewport = useSpatialViewport();
  const role = useCurrentRole();
  const zoneId = viewport?.selectedZoneId ?? null;
  // 명세 14장: 구역 수정은 관리자(ADMIN)만 할 수 있다.
  if (!zoneId || role !== 'ADMIN' || !onEditZone) return null;
  const zone = viewport?.zones.find((z) => z.zoneId === zoneId);

  return (
    <div
      role="region"
      aria-label="선택 구역 동작"
      className="pointer-events-auto absolute top-[124px] left-3.5 z-[5] flex items-center gap-2 rounded-[12px] bg-card/94 px-3 py-1.5 shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <span className="font-mono text-xs font-semibold text-foreground">{zoneId}</span>
      {zone && (
        <span className="max-w-[160px] truncate text-xs text-muted-foreground">{zone.name}</span>
      )}
      <Button size="sm" aria-label={`구역 수정 ${zoneId}`} onClick={() => onEditZone(zoneId)}>
        구역 수정
      </Button>
    </div>
  );
}

export function MonitorPage({
  onEditZone,
}: {
  /** 선택 구역 수정을 눌렀을 때 동작(/zones?zone=…&edit=1 이동). */
  onEditZone?: (zoneId: string) => void;
} = {}) {
  return (
    <>
      <h1 className="sr-only">실시간 관제</h1>
      <MonitorZoneAction onEditZone={onEditZone} />
    </>
  );
}
