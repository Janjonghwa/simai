import { SurfaceHeightField } from '@/features/zones/SurfaceHeightField';
import type { ZoneEditing } from '@/features/zones/useZoneEditing';
import { SURFACE_OUTSIDE_TEXT } from '@/features/zones/zoneEditModel';
import {
  BUTTON_CLASS,
  CARD_CLASS,
  FIELD_LABEL_CLASS,
  FIELD_NOTE_CLASS,
  GHOST_BUTTON_CLASS,
} from '@/features/zones/zonePanelStyle';
import { Button } from '@simai/ui/components/button';

/** 고소 작업면(구역에 하나): 상판 높이, 3D 에서 고칠 대상(구역·작업면), 추가·삭제. */
export function SurfaceCard({ edit }: { edit: ZoneEditing }) {
  const { surface, canEdit, busy } = edit;
  return (
    <section aria-label="고소 작업면" className={CARD_CLASS}>
      {surface ? (
        <>
          <div className="flex items-center justify-between gap-2">
            <b className={FIELD_LABEL_CLASS}>고소 작업면</b>
            {canEdit && (
              <Button
                variant="ghost"
                disabled={busy}
                onClick={edit.removeSurface}
                className={GHOST_BUTTON_CLASS}
              >
                삭제
              </Button>
            )}
          </div>
          <SurfaceHeightField
            key={`${edit.zoneKey}:${surface.id ?? 'new'}`}
            value={surface.geometry.topHeightM}
            bad={edit.surfaceErrors !== null}
            disabled={!canEdit || busy}
            onCommit={edit.setSurfaceTop}
          />
          {edit.surfaceOutside && (
            <span role="alert" className={FIELD_NOTE_CLASS}>
              {SURFACE_OUTSIDE_TEXT}
            </span>
          )}
          {canEdit && <EditTargetToggle edit={edit} />}
        </>
      ) : (
        canEdit && (
          <Button
            variant="secondary"
            disabled={busy}
            onClick={edit.addSurface}
            className={`${BUTTON_CLASS} w-full`}
          >
            + 고소 작업면 추가
          </Button>
        )
      )}
    </section>
  );
}

/** 3D 손잡이가 고칠 대상을 구역과 작업면 중에서 고른다. 고르는 쪽이 눌린 버튼이다. */
function EditTargetToggle({ edit }: { edit: ZoneEditing }) {
  const { surfMode } = edit;
  return (
    <div role="group" aria-label="3D 에서 조절할 대상" className="grid grid-cols-2 gap-1.5">
      <Button
        variant="secondary"
        aria-pressed={!surfMode}
        disabled={!edit.zone && edit.surface !== null}
        onClick={() => edit.chooseSurfaceMode(false)}
        className={BUTTON_CLASS}
      >
        구역
      </Button>
      <Button
        variant="secondary"
        aria-pressed={surfMode}
        disabled={edit.surfaceErrors !== null}
        onClick={() => edit.chooseSurfaceMode(true)}
        className={BUTTON_CLASS}
      >
        작업면
      </Button>
    </div>
  );
}
