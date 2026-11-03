import { SurfaceCard } from '@/features/zones/SurfaceCard';
import { useZoneEditing } from '@/features/zones/useZoneEditing';
import { ZoneActions } from '@/features/zones/ZoneActions';
import { ZoneAttributesCard } from '@/features/zones/ZoneAttributesCard';
import {
  BUTTON_CLASS,
  NOTE_CLASS,
  PANEL_CLASS,
  RETRY_CLASS,
  WARNING_CLASS,
} from '@/features/zones/zonePanelStyle';
import { ZoneSelect } from '@/features/zones/ZoneSelect';
import { Button } from '@simai/ui/components/button';

/** 직사각형이 아니거나 높이가 없어 3D 손잡이로 고칠 수 없는 구역을 골랐을 때 알림. */
const SHAPE_LOCKED_TEXT = '직사각형이 아니거나 높이가 없는 구역은 형상을 고칠 수 없습니다.';

/**
 * 구역 편집 도구(명세 FR-15·FR-16): 3D 위 왼쪽 패널. 드롭다운 하나(새 구역 추가 · 기존 구역), 이름·심각도, 고소 작업면(구역에
 * 하나), 버튼. 3D 손잡이는 고른 구역(또는 새 구역·작업면)의 상자를 고친다. 저장하면 구역·작업면·현장 판정 발행을 요청 하나로 보낸다.
 * 관리자가 아니면 값만 본다.
 */
export function ZonesPage({
  initialZoneId = null,
  onSearchConsumed,
}: {
  initialZoneId?: string | null;
  onSearchConsumed?: () => void;
} = {}) {
  const edit = useZoneEditing({ initialZoneId, onSearchConsumed });
  const showForm = edit.zone !== null || edit.canEdit;
  const warning = edit.notice ?? (edit.zone?.shapeEditable === false ? SHAPE_LOCKED_TEXT : null);

  return (
    <section aria-label="구역 편집 도구" className={PANEL_CLASS}>
      <h1 className="sr-only">구역 편집</h1>
      <ZoneSelect edit={edit} />

      {edit.zonesFailed && (
        <p role="alert" className={NOTE_CLASS}>
          구역 목록을 불러오지 못했습니다.
        </p>
      )}

      {edit.showRetry && (
        <div role="alert" className={RETRY_CLASS}>
          <span className="min-w-0">저장했지만 현장 판정으로 보내지 못했습니다.</span>
          <Button
            variant="secondary"
            onClick={() => void edit.republish()}
            className={BUTTON_CLASS}
          >
            다시 보내기
          </Button>
        </div>
      )}

      {warning && (
        <p role="status" className={WARNING_CLASS}>
          {warning}
        </p>
      )}

      {showForm && <ZoneAttributesCard edit={edit} />}
      {showForm && <SurfaceCard edit={edit} />}
      {edit.canEdit && <ZoneActions edit={edit} />}
    </section>
  );
}
