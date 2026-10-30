import type { SpatialViewMode } from '@/features/layout/spatialHost/useViewerMode';
import { Button } from '@simai/ui/components/button';

/**
 * 3D 는 처음 시점으로 돌리고, 3D 가 멈춰 2D 를 보고 있으면 3D 를 다시 띄운다(관제 데모와 같다).
 * 2D 는 왼쪽 아래 미니맵을 눌러 큰 창으로 본다.
 */
export function ViewControls({
  view,
  onResetView,
  onRetry3d,
}: {
  view: SpatialViewMode;
  onResetView: () => void;
  onRetry3d: () => void;
}) {
  return (
    <div
      role="group"
      aria-label="보기"
      className="absolute top-[72px] left-3.5 z-[5] flex gap-1 rounded-[12px] bg-card/94 p-1 shadow-float backdrop-blur-[8px]"
    >
      {view === '3d' ? (
        <Button size="sm" variant="ghost" onClick={onResetView}>
          시점 초기화
        </Button>
      ) : (
        <Button size="sm" variant="ghost" onClick={onRetry3d}>
          3D 다시 시도
        </Button>
      )}
    </div>
  );
}
