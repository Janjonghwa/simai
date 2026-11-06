import { SurfaceHeightField } from '@/features/zones/SurfaceHeightField';
import type { ZoneEditing } from '@/features/zones/useZoneEditing';
import { CARD_CLASS, FIELD_LABEL_CLASS } from '@/features/zones/zonePanelStyle';

/**
 * 고소 작업면(구역에 하나): 상판 높이. 구역은 작업면에서 계산하므로 작업면은 지우거나 따로 더하지 않는다(새 구역은 작업면과
 * 함께 시작한다). 3D 손잡이는 작업면을 고친다. 작업면 없이 만든 기존 구역은 카드를 보이지 않는다.
 */
export function SurfaceCard({ edit }: { edit: ZoneEditing }) {
  const { surface, busy } = edit;
  if (!surface) return null;
  return (
    <section aria-label="고소 작업면" className={CARD_CLASS}>
      <b className={FIELD_LABEL_CLASS}>고소 작업면</b>
      <SurfaceHeightField
        key={`${edit.zoneKey}:${surface.id ?? 'new'}`}
        value={surface.geometry.topHeightM}
        bad={edit.surfaceErrors !== null}
        disabled={busy}
        onCommit={edit.setSurfaceTop}
      />
    </section>
  );
}
