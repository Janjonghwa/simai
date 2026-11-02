import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { geometryErrors, sameGeometry } from '@/features/surfaces/surfaces';
import { useWorkSurfaces } from '@/features/surfaces/useWorkSurfaces';
import type { Severity } from '@/features/zones/types';
import { useConfigApply } from '@/features/zones/useConfigApply';
import { useZoneCommit } from '@/features/zones/useZoneCommit';
import { useZoneTarget } from '@/features/zones/useZoneTarget';
import { useZoneUndoKey } from '@/features/zones/useZoneUndoKey';
import {
  useBoxChange,
  useBoxEditBridge,
  useWorkSurfaceFeed,
} from '@/features/zones/useZoneViewerBridge';
import {
  boxEditOf,
  boxOfZone,
  currentSurface,
  defaultNewBox,
  formWithPatch,
  ghostsOf,
  newSurfaceDraft,
  newZoneForm,
  surfaceChanged,
  targetChanged,
  withPatch,
  withSurface,
  zoneChanged,
  type TargetDraft,
  type ZonePatch,
} from '@/features/zones/zoneEditModel';
import { formErrors, formOf, newZoneErrors } from '@/features/zones/zoneForm';
import { boxToPolygon } from '@/features/zones/zones';
import { linkSurfaces, pointInPolygon } from '@/features/zones/zoneSurfaceLink';
import { useRoomLimit } from '@/store/roomBounds.store';
import {
  recordZoneDraft,
  removeZoneDraft,
  undoZoneDraft,
  updateZoneDraft,
  useZoneEditStore,
} from '@/store/zoneEdit.store';
import { useMemo } from 'react';

/** 이름 글자 입력을 한 단계로 묶는 되돌리기 묶음 이름. */
const NAME_EDIT_GROUP = 'name';

/**
 * 발행 전: 마지막 묶음의 items 에 이 버전이 없는 구역·이은 작업면(보관해 묶음에서 빠질 것도 센다).
 * pendingIds 는 드롭다운에 `발행 전`을 붙일 구역, pendingCount 는 다시 보내기를 띄울지 가르는 수다.
 */
function usePendingPublish(
  apply: ReturnType<typeof useConfigApply>,
  links: ReadonlyMap<string, { objectId: string }>,
  ready: boolean,
) {
  const { unpublished, unpublishedSurfaces, removed } = apply;
  const pendingIds = useMemo(() => {
    const ids = new Set(unpublished ?? []);
    for (const [zoneId, linkedSurface] of links)
      if (unpublishedSurfaces?.has(linkedSurface.objectId)) ids.add(zoneId);
    return ids;
  }, [unpublished, unpublishedSurfaces, links]);
  const pendingCount =
    (unpublished?.size ?? 0) + (unpublishedSurfaces?.size ?? 0) + (ready ? removed : 0);
  return { pendingIds, pendingCount };
}

/**
 * 구역 편집 화면의 상태와 동작. 구역(또는 새 구역) 하나를 고르면 이름·심각도·구역 상자·작업면(하나)을 고치고, 저장하면
 * 구역 → 작업면 → 구성 묶음 발행 순서로 보낸다(useZoneCommit). 고친 것은 구역마다 초안으로 남아 다른 구역으로 옮겨도
 * 잃지 않는다. 3D 손잡이는 편집 상자를 boxEdit 저장소로 내려 SpatialViewportHost 가 뷰어에 넘긴다.
 */
export function useZoneEditing({
  initialZoneId = null,
  onSearchConsumed,
}: {
  initialZoneId?: string | null;
  onSearchConsumed?: () => void;
} = {}) {
  const viewport = useSpatialViewport()!;
  const { zones, zonesRaw, zonesFailed, view, viewerReady } = viewport;
  const canEdit = useCurrentRole() === 'ADMIN';
  const surfaceList = useWorkSurfaces();
  const { surfaces } = surfaceList;
  const apply = useConfigApply(zones, surfaceList.loaded ? surfaces : undefined);
  const drafts = useZoneEditStore((s) => s.drafts);
  const target = useZoneTarget(viewport, { initialZoneId, onSearchConsumed });
  const { zone, key, surfMode } = target;

  // ---- 고르고 있는 대상의 값 ----------------------------------------------------------------
  const links = useMemo(() => linkSurfaces(zones, surfaces), [zones, surfaces]);
  const linked = zone ? (links.get(zone.zoneId) ?? null) : null;
  const draft: TargetDraft | undefined = drafts[key];
  const surface = useMemo(() => currentSurface(draft, linked), [draft, linked]);
  const roomLimit = useRoomLimit();
  const zoneBox = useMemo(
    () => draft?.box ?? (zone ? boxOfZone(zone) : defaultNewBox(zones, roomLimit)),
    [draft?.box, zone, zones, roomLimit],
  );
  const form = zone ? formWithPatch(zone, draft?.patch ?? {}) : newZoneForm(draft?.patch ?? {});
  const errors = zone
    ? formErrors(form, { saved: formOf(zone), hasHeight: zone.zRange !== null })
    : newZoneErrors(form);
  const surfaceErrors = surface ? geometryErrors(surface.geometry) : null;
  const surfaceValid = surfaceErrors === null;
  // 작업면 중심이 구역 바닥 안에 있어야 그 구역의 작업면이다(서버에는 잇는 칸이 없어 위치로 잇는다).
  const polygon = zone && !draft?.box ? zone.polygon : boxToPolygon(zoneBox);
  const surfaceOutside = surface !== null && !pointInPolygon(surface.geometry.centerXy, polygon);

  // ---- 저장 조건 ----------------------------------------------------------------------------
  const zoneDirty = zone ? zoneChanged(zone, draft) : false;
  const surfaceDirty = zone ? surfaceChanged(draft?.surface, linked) : false;
  const dirty = zoneDirty || surfaceDirty;
  const ready = Boolean(zonesRaw) && surfaceList.loaded;
  const valid = Object.keys(errors).length === 0 && surfaceValid && !surfaceOutside;
  const { pendingIds, pendingCount } = usePendingPublish(apply, links, ready);
  // 이 구역이 저장 전(고침)인지 가르는 ID 모음. 드롭다운에 `저장 전`을 붙인다.
  const changedIds = useMemo(
    () =>
      new Set(
        zones
          .filter((z) => targetChanged(z, drafts[z.zoneId], links.get(z.zoneId) ?? null))
          .map((z) => z.zoneId),
      ),
    [zones, drafts, links],
  );

  // ---- 3D 와 주고받기 -----------------------------------------------------------------------
  const boxEdit = useMemo(
    () =>
      boxEditOf({
        canEdit,
        surfMode,
        key,
        zone,
        zoneBox,
        surface,
        surfaceValid,
      }),
    [canEdit, surfMode, key, zone, zoneBox, surface, surfaceValid],
  );
  const ghosts = useMemo(
    () => ghostsOf(drafts, key, surfMode, zones),
    [drafts, key, surfMode, zones],
  );
  const onBoxChange = useBoxChange({ key, zone, surface, surfMode, zoneBox });
  useBoxEditBridge({ boxEdit, ghosts, onChange: onBoxChange });
  useWorkSurfaceFeed({ active: view === '3d' && viewerReady, raw: surfaceList.raw });

  // ---- 저장·추가·삭제 -----------------------------------------------------------------------
  const commit = useZoneCommit({
    canEdit,
    zone,
    zoneBox,
    form,
    draft,
    linked,
    zoneDirty,
    surfaceDirty,
    saveReady: canEdit && zone !== null && dirty && valid,
    addReady: canEdit && zone === null && ready && valid,
    pendingCount,
    publish: () => apply.publish.mutateAsync(),
    target,
  });

  // ---- 고치기 -------------------------------------------------------------------------------
  // 고칠 때마다 되돌릴 단계를 남긴다. group 이 같은 연속 고침(이름 글자 입력)은 한 단계로 묶는다.
  const edit = (next: (prev: TargetDraft | undefined) => TargetDraft, group?: string) => {
    recordZoneDraft(key, group);
    updateZoneDraft(key, next);
  };
  const setPatch = (patch: ZonePatch, group?: string) =>
    edit((prev) => withPatch(prev, zone, patch), group);
  const setName = (name: string) => setPatch({ name }, NAME_EDIT_GROUP);
  const setSeverity = (severity: Severity) => setPatch({ severity });
  const addSurface = () => {
    edit((prev) => withSurface(prev, newSurfaceDraft(zoneBox)));
    target.setSurfaceMode(true);
  };
  // 저장된 작업면이면 구역 저장 때 보관하고, 이번에 만든 것이면 그냥 버린다.
  const removeSurface = () => {
    edit((prev) => withSurface(prev, linked ? null : undefined));
    target.setSurfaceMode(false);
  };
  const setSurfaceTop = (top: number) => {
    if (!surface) return;
    const geometry = { ...surface.geometry, topHeightM: top };
    // 같은 높이를 다시 받으면(입력 뒤 포커스를 잃을 때) 고친 것이 없으니 되돌릴 단계를 남기지 않는다.
    if (sameGeometry(geometry, surface.geometry)) return;
    edit((prev) => withSurface(prev, { ...surface, geometry }));
  };
  const pick = (value: string) => {
    commit.disarm();
    target.pick(value);
  };
  const restore = () => {
    if (!zone) return;
    removeZoneDraft(zone.zoneId);
    target.setSurfaceMode(false);
    commit.clearNotice();
  };
  // Ctrl+Z: 되돌린 결과 작업면이 없어졌으면 작업면 손잡이 모드도 끈다(손잡이가 숨은 채 남지 않게).
  const undo = () => {
    if (!undoZoneDraft(key)) return false;
    const restored = useZoneEditStore.getState().drafts[key];
    if (surfMode && !currentSurface(restored, linked)) target.setSurfaceMode(false);
    return true;
  };
  useZoneUndoKey(canEdit && !commit.busy, undo);

  return {
    canEdit,
    zones,
    zone,
    zoneKey: key,
    zonesFailed,
    form,
    errors,
    surface,
    surfaceErrors,
    surfaceOutside,
    surfMode,
    dirty,
    changedIds,
    pendingIds,
    armed: commit.armed,
    busy: commit.busy,
    canSave: commit.canSave,
    canAdd: commit.canAdd,
    showRetry: commit.showRetry,
    notice: commit.notice,
    pick,
    setName,
    setSeverity,
    addSurface,
    removeSurface,
    setSurfaceTop,
    chooseSurfaceMode: target.setSurfaceMode,
    save: commit.save,
    add: commit.add,
    restore,
    remove: commit.remove,
    republish: commit.republish,
  };
}

/** 구역 편집 화면 훅의 반환값. 패널 조각들이 받는다. */
export type ZoneEditing = ReturnType<typeof useZoneEditing>;
