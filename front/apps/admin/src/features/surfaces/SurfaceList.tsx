import type { WorkSurface } from '@/features/surfaces/types';
import { NEW_SURFACE_ID } from '@/features/surfaces/useSurfaceEditing';

const ROW =
  'flex items-center justify-between gap-2 rounded-[8px] px-2.5 py-1.5 text-left text-xs transition-colors ';
const ROW_ACTIVE = 'bg-surface-3 font-semibold text-foreground ring-1 ring-primary';
const ROW_IDLE = 'text-muted-foreground hover:bg-surface-3 hover:text-foreground';
const BADGE = 'rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-muted-foreground';
const DRAFT_BADGE =
  'rounded bg-point-soft/20 px-1.5 py-0.5 text-[10px] font-medium text-point-soft';

/** 등록 작업면 목록(구역 목록과 같은 모양). 새 작업면 초안이 있으면 맨 위에 둔다. */
export function SurfaceList({
  surfaces,
  loaded,
  failed,
  selectedId,
  hasNew,
  dirtyIds,
  unpublished,
  onPick,
}: {
  surfaces: readonly WorkSurface[];
  loaded: boolean;
  failed: boolean;
  /** 고른 작업면 ID. 새 작업면이면 NEW_SURFACE_ID. */
  selectedId: string | null;
  hasNew: boolean;
  dirtyIds: ReadonlySet<string>;
  unpublished: ReadonlySet<string> | null;
  onPick: (id: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label="작업면 목록"
      className="flex flex-col gap-1 rounded-[12px] bg-surface-2 p-2 ring-1 ring-border ring-inset"
    >
      {failed && (
        <p role="alert" className="text-xs text-muted-foreground">
          작업면 목록을 불러오지 못했습니다. 자동으로 다시 시도합니다.
        </p>
      )}
      <div className="flex max-h-[200px] flex-col gap-1 overflow-y-auto">
        {hasNew && (
          <button
            type="button"
            aria-pressed={selectedId === NEW_SURFACE_ID}
            aria-label="새 작업면, 저장 전"
            onClick={() => onPick(NEW_SURFACE_ID)}
            className={ROW + (selectedId === NEW_SURFACE_ID ? ROW_ACTIVE : ROW_IDLE)}
          >
            <span className="truncate font-semibold text-foreground">새 작업면</span>
            <span className={DRAFT_BADGE}>저장 전</span>
          </button>
        )}
        {surfaces.map((surface) => {
          const active = surface.objectId === selectedId;
          const top = `상판 ${surface.geometry.topHeightM.toFixed(2)}m`;
          // 읽는 이름은 이름·배지를 쉼표로 끊는다(배지 글자를 이어 붙이면 한 낱말처럼 읽힌다).
          const label = [
            surface.name ? `${surface.objectId} · ${surface.name}` : surface.objectId,
            top,
            dirtyIds.has(surface.objectId) && '고치는 중',
            unpublished?.has(surface.objectId) && '발행 전',
          ]
            .filter(Boolean)
            .join(', ');
          return (
            <button
              key={surface.objectId}
              type="button"
              aria-pressed={active}
              aria-label={label}
              onClick={() => onPick(surface.objectId)}
              className={ROW + (active ? ROW_ACTIVE : ROW_IDLE)}
            >
              <span className="truncate">
                <span className="font-mono font-semibold text-foreground">{surface.objectId}</span>
                {surface.name && ` · ${surface.name}`}
              </span>
              <span className="flex shrink-0 items-center gap-1">
                <span className={`${BADGE} font-mono`}>{top}</span>
                {dirtyIds.has(surface.objectId) && <span className={DRAFT_BADGE}>고치는 중</span>}
                {unpublished?.has(surface.objectId) && <span className={BADGE}>발행 전</span>}
              </span>
            </button>
          );
        })}
        {!loaded && !failed && (
          <p className="py-2 text-center text-xs text-muted-foreground">
            작업면을 불러오는 중입니다.
          </p>
        )}
        {loaded && surfaces.length === 0 && !hasNew && (
          <p className="py-2 text-center text-xs text-muted-foreground">
            등록된 작업면이 없습니다.
          </p>
        )}
      </div>
    </div>
  );
}
