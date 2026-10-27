import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { ConfigApplyBar } from '@/features/surfaces/ConfigApplyBar';
import { SurfaceFields } from '@/features/surfaces/SurfaceFields';
import { SurfaceList } from '@/features/surfaces/SurfaceList';
import { useSurfaceEditing } from '@/features/surfaces/useSurfaceEditing';
import { useConfigApply } from '@/features/zones/useConfigApply';
import { Button } from '@simai/ui/components/button';
import { Check, Move3d, Plus, RotateCcw, Save, X } from 'lucide-react';

/**
 * 작업면 등록(명세 7.5·13.4·FR-112): 단상·책상 같은 작업면을 3D 손잡이·숫자로 OBB 와 상판 높이를 정해 새 버전으로 저장하고,
 * 구성 묶음으로 현장에 적용한다(FR-17). 구역 편집 화면과 같은 떠 있는 패널이다. 관리자만 고치고 다른 역할은 값만 본다.
 */
export function SurfacesPage() {
  const viewport = useSpatialViewport()!;
  const editing = useSurfaceEditing();
  const { list, selected, showingNew, geometry, canEdit, busy, sessionOpen, notice } = editing;
  const apply = useConfigApply(viewport.zones, list.loaded ? list.surfaces : undefined);
  // 발행은 구역·작업면을 함께 보낸다. 다른 관리자가 저장만 해 둔 구역도 함께 나가므로 같이 센다.
  const unpublishedCount = (apply.unpublished?.size ?? 0) + (apply.unpublishedSurfaces?.size ?? 0);

  return (
    <section
      aria-label="작업면 등록 도구"
      className="pointer-events-auto absolute top-[124px] left-3.5 z-[5] flex max-h-[calc(100svh-228px)] w-[356px] flex-col gap-2.5 overflow-y-auto rounded-[16px] bg-card/94 p-3 break-keep shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <h1 className="sr-only">작업면 등록</h1>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="shrink-0 rounded-md bg-surface-3 px-2 py-0.5 text-[11px] font-semibold whitespace-nowrap text-point-soft">
          작업면 등록
        </span>
        {canEdit && (
          <Button
            size="sm"
            variant="secondary"
            disabled={!list.loaded || busy}
            onClick={editing.startNew}
            className="whitespace-nowrap"
          >
            <Plus className="size-3.5" aria-hidden />새 작업면
          </Button>
        )}
      </div>

      <ConfigApplyBar
        apply={apply}
        unpublishedCount={unpublishedCount}
        canEdit={canEdit}
        onError={(text) => editing.setNotice({ tone: 'warning', text })}
      />

      <SurfaceList
        surfaces={list.surfaces}
        loaded={list.loaded}
        failed={list.failed}
        selectedId={editing.targetId}
        hasNew={editing.hasNew}
        dirtyIds={editing.dirtyIds}
        unpublished={apply.unpublishedSurfaces}
        onPick={editing.pick}
      />

      {geometry && (selected || showingNew) && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 rounded-[10px] bg-surface-2 px-2.5 py-1.5">
            <span className="shrink-0 rounded-md bg-surface-3 px-2 py-0.5 font-mono text-xs font-semibold whitespace-nowrap text-foreground">
              {selected ? selected.objectId : '새 작업면'}
            </span>
            {selected?.name && (
              <span className="min-w-0 truncate text-xs font-medium text-foreground">
                {selected.name}
              </span>
            )}
            {selected && (
              <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
                v{selected.version}
                {apply.unpublishedSurfaces?.has(selected.objectId) && ' · 발행 전'}
              </span>
            )}
          </div>

          <SurfaceFields
            key={editing.targetId}
            name={editing.name}
            geometry={geometry}
            readOnly={!canEdit}
            disabled={busy}
            onName={editing.changeName}
            onField={editing.commitField}
          />

          {canEdit && (
            <div className="flex flex-col gap-1.5">
              {sessionOpen ? (
                <div className={selected ? 'grid grid-cols-2 gap-1.5' : 'flex'}>
                  {/* 새 작업면은 끝내면 3D 상자가 사라지므로 저장·취소만 둔다. */}
                  {selected && (
                    <Button
                      size="sm"
                      disabled={busy}
                      onClick={editing.finishEdit}
                      className="w-full"
                    >
                      <Check className="size-3.5" aria-hidden />
                      3D 편집 끝내기
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy}
                    onClick={editing.cancelEdit}
                    className="w-full"
                  >
                    <X className="size-3.5" aria-hidden />
                    3D 편집 취소
                  </Button>
                </div>
              ) : (
                <Button
                  size="sm"
                  disabled={!editing.can3d || busy}
                  onClick={editing.startEdit}
                  className="w-full"
                >
                  <Move3d className="size-3.5" aria-hidden />
                  3D 로 고치기
                </Button>
              )}
              <div className="grid grid-cols-2 gap-1.5">
                <Button
                  size="sm"
                  disabled={!editing.dirty || busy}
                  onClick={editing.saveTarget}
                  className="w-full"
                >
                  <Save className="size-3.5" aria-hidden />
                  {busy ? '저장 중…' : '저장'}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy || (selected !== null && !editing.dirty && !sessionOpen)}
                  onClick={editing.revert}
                  className="w-full"
                >
                  <RotateCcw className="size-3.5" aria-hidden />
                  {selected ? '되돌리기' : '새 작업면 버리기'}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {notice && (
        <p
          role="status"
          className={
            'rounded-[10px] px-2.5 py-1.5 text-xs ' +
            (notice.tone === 'warning'
              ? 'bg-surface-3 font-medium text-foreground ring-1 ring-border ring-inset'
              : 'bg-surface-2 text-muted-foreground')
          }
        >
          {notice.text}
        </p>
      )}
    </section>
  );
}
