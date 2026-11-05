import { SurfaceHeightField } from '@/features/zones/SurfaceHeightField';
import type { ZoneEditing } from '@/features/zones/useZoneEditing';
import {
  BUTTON_CLASS,
  CARD_CLASS,
  FIELD_LABEL_CLASS,
  GHOST_BUTTON_CLASS,
} from '@/features/zones/zonePanelStyle';
import { Button } from '@simai/ui/components/button';

/** 고소 작업면(구역에 하나): 상판 높이, 추가·삭제. 구역은 작업면에서 계산하므로 3D 손잡이는 작업면을 고친다. */
export function SurfaceCard({ edit }: { edit: ZoneEditing }) {
  const { surface, busy } = edit;
  return (
    <section aria-label="고소 작업면" className={CARD_CLASS}>
      {surface ? (
        <>
          <div className="flex items-center justify-between gap-2">
            <b className={FIELD_LABEL_CLASS}>고소 작업면</b>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={edit.removeSurface}
              className={GHOST_BUTTON_CLASS}
            >
              삭제
            </Button>
          </div>
          <SurfaceHeightField
            key={`${edit.zoneKey}:${surface.id ?? 'new'}`}
            value={surface.geometry.topHeightM}
            bad={edit.surfaceErrors !== null}
            disabled={busy}
            onCommit={edit.setSurfaceTop}
          />
        </>
      ) : (
        <Button
          variant="secondary"
          disabled={busy}
          onClick={edit.addSurface}
          className={`${BUTTON_CLASS} w-full`}
        >
          + 고소 작업면 추가
        </Button>
      )}
    </section>
  );
}
