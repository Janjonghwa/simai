import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { targetOfKey, useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import {
  bodyOf,
  fieldsChanged,
  fieldValue,
  geometryErrors,
  newSurfaceGeometry,
  readWorkSurfaces,
  sameGeometry,
  sameName,
  withField,
  type SurfaceField,
} from '@/features/surfaces/surfaces';
import type { SurfaceGeometry, WorkSurface } from '@/features/surfaces/types';
import {
  SurfaceSaveBlockedError,
  surfaceSaveErrorText,
  useSurfaceSave,
} from '@/features/surfaces/useSurfaceSave';
import { useWorkSurfaces } from '@/features/surfaces/useWorkSurfaces';
import type { ViewerEditRejectReason } from '@/features/viewer3d';
import { useObjectEditStore, type SurfaceFinished } from '@/store/objectEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

/** 새 작업면 초안의 3D 편집 ID. 저장하면 서버가 ws-… ID 를 정한다. 한 번에 하나만 만든다. */
export const NEW_SURFACE_ID = 'new-surface';
/** 화면을 떠날 때 3D 에서 작업면을 지우는 빈 묶음. */
const NO_SURFACES = { work_surfaces: [] };

export interface SurfaceNotice {
  tone: 'neutral' | 'warning';
  text: string;
}

const DRAGGING_TEXT = '손잡이를 놓은 뒤 다시 누르세요.';

/** 3D 편집 명령을 뷰어가 거부한 이유. */
export function refusalText(reason: ViewerEditRejectReason): string {
  switch (reason) {
    case 'NOT_FOUND':
      return '3D 장면에 아직 이 작업면이 없습니다. 잠시 뒤 다시 시도하세요.';
    case 'NOT_READY':
      return '3D 가 준비되지 않았습니다.';
    case 'NOT_EDITABLE':
      return '기록 재생 중에는 고칠 수 없습니다.';
    case 'INVALID_TARGET':
      return '이미 3D 장면에 있는 ID 라 만들 수 없습니다.';
    case 'INVALID_GEOMETRY':
      return '이 형상으로는 바꿀 수 없습니다.';
    case 'STALE_SOURCE':
      return '작업면 목록이 바뀌어 3D 편집을 멈췄습니다. 고친 값은 남겨 두었습니다.';
  }
}

/** 3D 편집이 저절로 끝난 이유(뷰어 REJECTED). */
function endedText(reason: string | null): string {
  switch (reason) {
    case 'STALE_SOURCE':
      return '작업면 목록이 바뀌어 3D 편집을 멈췄습니다. 고친 값은 남겨 두었습니다.';
    case 'NOT_READY':
      return '3D 를 그리지 못해 편집을 멈췄습니다. 고친 값은 남겨 두었습니다.';
    case 'NOT_EDITABLE':
      return '기록 재생 중에는 고칠 수 없어 편집을 멈췄습니다.';
    case 'INVALID_GEOMETRY':
      return '형상이 올바르지 않아 3D 편집을 끝내지 못했습니다.';
    default:
      return '3D 편집을 멈췄습니다.';
  }
}

const firstError = (geometry: SurfaceGeometry) => {
  const errors = geometryErrors(geometry);
  return errors ? Object.values(errors)[0]! : null;
};

function omit<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * 작업면 등록 화면(-165)의 상태와 처리.
 * - 고른 작업면은 틀의 선택 키(object:…)에서 얻는다. 새 작업면 초안은 이 화면이 들고, 목록에 없는 키면 그것을 보인다.
 * - 형상 초안은 편집 저장소(열린 3D 편집 미리보기·남긴 형상)에, 이름 초안·저장 기준 버전은 이 화면에 둔다. 작업면마다 따로라
 *   다른 작업면으로 옮겨도 잃지 않는다. 화면을 떠나면 버린다.
 * - 작업면은 이 화면에서만 3D 에 넣는다(관제·구역 화면 3D 에는 넣지 않는다). 떠날 때 지운다.
 */
export function useSurfaceEditing() {
  const viewport = useSpatialViewport()!;
  const { viewer, view, viewerReady, selectedKey, selectTarget, clearSelection } = viewport;
  const canEdit = useCurrentRole() === 'ADMIN';
  const queryClient = useQueryClient();
  const list = useWorkSurfaces();
  const { surfaces, raw } = list;
  const { save, create } = useSurfaceSave();
  const source = useRealtimeStore((s) => s.source);
  const preview = useObjectEditStore((s) => s.preview);
  const kept = useObjectEditStore((s) => s.kept);
  const ended = useObjectEditStore((s) => s.ended);

  const [creating, setCreating] = useState<{ initial: SurfaceGeometry } | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [bases, setBases] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNoticeState] = useState<SurfaceNotice | null>(null);
  // 3D 편집이 저절로 멈춘 알림(편집 저장소 ended)은 이 번호까지 보였거나 지웠다. 화면 알림을 바꾸거나 지울 때 적는다.
  const [seenEndSeq, setSeenEndSeq] = useState(0);
  // 저장·만들기를 같은 틱에 두 번 보내지 않는다(busy 는 다음 그리기에 바뀐다).
  const sending = useRef(false);
  // 저장 응답이 화면을 떠난 뒤 와도 선택·알림을 바꾸지 않는다.
  const alive = useRef(true);
  // 떠날 때 쓰는 지금 선택과 선택 놓기. 떠날 때 정리가 틀 함수가 바뀔 때마다 돌지 않게 효과 의존에 두지 않는다.
  const leaving = useRef({ selectedKey, clearSelection });
  useEffect(() => {
    leaving.current = { selectedKey, clearSelection };
  }, [selectedKey, clearSelection]);

  // 3D 에 넣는다. 2D 에서 3D 로 돌아오면 뷰어가 새로 떠 준비될 때 다시 넣는다.
  useEffect(() => {
    if (view !== '3d' || !viewerReady || !source || !raw) return;
    source.pushWorkSurfaces({ ok: true, raw });
  }, [view, viewerReady, source, raw]);

  // 들어오면 다른 관리자가 저장한 새 버전도 보이게 다시 받는다(이미 받는 중이면 그 응답을 쓴다).
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: STATIC_OBJECTS_KEY }, { cancelRefetch: false });
  }, [queryClient]);

  // 떠나면 편집을 취소하고 초안을 버리며 3D 에서 작업면을 지운다. 고른 작업면도 놓는다(관제에서 빈 대상을 다시 고르지 않게).
  useEffect(() => {
    alive.current = true;
    useObjectEditStore.getState().clear();
    // 뷰어는 화면보다 늦게 뜰 수 있어 떠날 때의 핸들로 취소한다.
    const cancelOpenEdit = () => viewer.current?.cancelObjectEdit?.();
    return () => {
      alive.current = false;
      cancelOpenEdit();
      useObjectEditStore.getState().clear();
      useRealtimeStore.getState().source?.pushWorkSurfaces({ ok: true, raw: NO_SURFACES });
      const { selectedKey: key, clearSelection: release } = leaving.current;
      if (targetOfKey(key)?.type === 'object') release();
    };
  }, [viewer]);

  const picked = targetOfKey(selectedKey);
  const selected =
    picked?.type === 'object' ? (surfaces.find((s) => s.objectId === picked.id) ?? null) : null;
  const showingNew = selected === null && creating !== null;
  const targetId = selected?.objectId ?? (showingNew ? NEW_SURFACE_ID : null);
  const sessionOpen = targetId !== null && preview?.target.id === targetId;

  const draftOf = (id: string): SurfaceGeometry | null => {
    if (preview?.target.id === id) return preview.draft;
    if (kept[id]) return kept[id];
    if (id === NEW_SURFACE_ID) return creating?.initial ?? null;
    return surfaces.find((s) => s.objectId === id)?.geometry ?? null;
  };
  const isDirty = (surface: WorkSurface) => {
    const draft = draftOf(surface.objectId) ?? surface.geometry;
    const draftName = names[surface.objectId];
    return (
      !sameGeometry(draft, surface.geometry) ||
      (draftName !== undefined && !sameName(draftName, surface.name))
    );
  };
  const geometry = targetId ? draftOf(targetId) : null;
  const name = targetId ? (names[targetId] ?? selected?.name ?? '') : '';
  const dirty = selected ? isDirty(selected) : showingNew;
  const dirtyIds = new Set(surfaces.filter(isDirty).map((s) => s.objectId));

  const endedNotice: SurfaceNotice | null =
    ended && ended.status === 'REJECTED' && ended.seq > seenEndSeq
      ? { tone: 'warning', text: endedText(ended.reason) }
      : null;

  const setNotice = (next: SurfaceNotice | null) => {
    setNoticeState(next);
    setSeenEndSeq(useObjectEditStore.getState().ended?.seq ?? 0);
  };

  /** 초안이 있는지(편집 저장소는 처리 중에도 최신을 읽는다). */
  const hasDraft = (id: string) => {
    const state = useObjectEditStore.getState();
    return id in state.kept || state.preview?.target.id === id || names[id] !== undefined;
  };
  /** 초안을 처음 만들 때 고치기 시작한 버전을 적는다(저장 직전 확인의 기준). */
  const markBase = (surface: WorkSurface) => {
    if (!hasDraft(surface.objectId))
      setBases((prev) => ({ ...prev, [surface.objectId]: surface.version }));
  };
  const forget = (id: string) => {
    useObjectEditStore.getState().discard(id);
    setNames((prev) => omit(prev, id));
    setBases((prev) => omit(prev, id));
  };

  const openNew = (draft: SurfaceGeometry) => {
    if (useObjectEditStore.getState().preview?.target.id === NEW_SURFACE_ID) return;
    const handle = viewer.current;
    if (view === '3d' && viewerReady && handle?.beginObjectCreate) {
      const res = handle.beginObjectCreate({ id: NEW_SURFACE_ID, geometry: draft });
      if (res.ok) return;
      setNotice({ tone: 'warning', text: refusalText(res.reason) });
    }
    // 3D 에 띄우지 못해도(2D·준비 전) 고른 작업면을 놓아 새 작업면 칸을 보인다.
    clearSelection();
  };

  const startNew = () => {
    if (!canEdit || busy) return;
    setNotice(null);
    if (creating) {
      openNew(draftOf(NEW_SURFACE_ID) ?? creating.initial);
      return;
    }
    const initial = newSurfaceGeometry(surfaces);
    setCreating({ initial });
    openNew(initial);
  };

  const pick = (id: string) => {
    if (id !== NEW_SURFACE_ID) {
      selectTarget({ type: 'object', id }, { focus: true });
      return;
    }
    if (creating) openNew(draftOf(NEW_SURFACE_ID) ?? creating.initial);
  };

  /** 3D 로 고치기. 남긴 초안이 있으면 뷰어 편집(늘 등록 형상에서 시작)에 바뀐 칸을 다시 넣는다. */
  const startEdit = () => {
    if (!canEdit || busy || !targetId) return;
    setNotice(null);
    if (!selected) {
      if (creating) openNew(draftOf(NEW_SURFACE_ID) ?? creating.initial);
      return;
    }
    const handle = viewer.current;
    if (!handle) return;
    const snapshot = useObjectEditStore.getState().kept[selected.objectId];
    markBase(selected);
    const res = handle.beginEdit({ type: 'object', id: selected.objectId });
    if (!res.ok) {
      setNotice({ tone: 'warning', text: refusalText(res.reason) });
      return;
    }
    if (!snapshot) return;
    for (const field of fieldsChanged(selected.geometry, snapshot)) {
      const step = handle.updateObjectEditField?.({ field, value: fieldValue(snapshot, field) });
      if (!step?.ok) {
        // 남긴 초안은 Esc·취소에도 그대로 남는다.
        handle.cancelObjectEdit?.();
        setNotice({ tone: 'warning', text: '남긴 값을 3D 에 다시 넣지 못했습니다.' });
        return;
      }
    }
  };

  const finishEdit = () => {
    const result = viewer.current?.finishObjectEdit?.() ?? null;
    if (!result && useObjectEditStore.getState().preview)
      setNotice({ tone: 'neutral', text: DRAGGING_TEXT });
  };
  const cancelEdit = () => {
    viewer.current?.cancelObjectEdit?.();
  };

  /** 숫자 칸 하나를 반영한다(길이 m, 회전 rad). 문제가 있으면 칸에 보일 문구를 돌려준다. */
  const commitField = (field: SurfaceField, value: number): string | null => {
    // 저장 중에는 칸이 잠겨 부르지 않는다.
    if (!canEdit || !targetId || !geometry) return null;
    if (selected) markBase(selected);
    if (sessionOpen) {
      const res = viewer.current?.updateObjectEditField?.({ field, value });
      if (!res) return refusalText('NOT_READY');
      if (res.ok) return null;
      return res.reason === 'NOT_READY' ? DRAGGING_TEXT : refusalText(res.reason);
    }
    const next = withField(geometry, field, value);
    const problem = firstError(next);
    if (problem) return problem;
    useObjectEditStore.getState().keep(targetId, next);
    return null;
  };

  const changeName = (value: string) => {
    if (!canEdit || !targetId) return;
    if (selected) markBase(selected);
    setNames((prev) => ({ ...prev, [targetId]: value }));
  };

  /** 되돌리기(등록 작업면) · 새 작업면 버리기. */
  const revert = () => {
    if (busy || !targetId) return;
    setNotice(null);
    if (sessionOpen) viewer.current?.cancelObjectEdit?.();
    forget(targetId);
    if (targetId === NEW_SURFACE_ID) setCreating(null);
  };

  /**
   * 저장 전에 열린 3D 편집을 끝낸다. 열린 편집이 없으면 undefined, 끝내지 못하면 null(알림은 여기·편집 저장소가 보인다),
   * 끝냈으면 그 형상이다.
   */
  const finishForSave = (id: string): SurfaceGeometry | undefined | null => {
    if (useObjectEditStore.getState().preview?.target.id !== id) return undefined;
    const result = viewer.current?.finishObjectEdit?.() ?? null;
    if (!result) {
      setNotice({ tone: 'neutral', text: DRAGGING_TEXT });
      return null;
    }
    if (result.status !== 'FINISHED') return null;
    return (result as SurfaceFinished).after;
  };

  const run = async (work: () => Promise<void>) => {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    try {
      await work();
    } finally {
      sending.current = false;
      if (alive.current) setBusy(false);
    }
  };

  const saveSelected = (surface: WorkSurface) =>
    run(async () => {
      const finished = finishForSave(surface.objectId);
      if (finished === null) return;
      const draft =
        finished ?? useObjectEditStore.getState().kept[surface.objectId] ?? surface.geometry;
      const draftName = names[surface.objectId] ?? surface.name ?? '';
      if (sameGeometry(draft, surface.geometry) && sameName(draftName, surface.name)) {
        forget(surface.objectId);
        return;
      }
      const problem = firstError(draft);
      if (problem) {
        setNotice({ tone: 'warning', text: problem });
        return;
      }
      try {
        const item = await save.mutateAsync({
          surface: { ...surface, version: bases[surface.objectId] ?? surface.version },
          body: bodyOf(surface, draftName, draft),
        });
        if (!alive.current) return;
        forget(surface.objectId);
        setNotice({ tone: 'neutral', text: `저장했습니다 · v${item.object_version}` });
      } catch (error) {
        if (!alive.current) return;
        if (error instanceof SurfaceSaveBlockedError && error.reason === 'CHANGED') {
          // 다시 받은 버전을 새 기준으로 삼고, 초안은 이 사용자가 고친 칸만 그 버전 형상에 얹는다(손대지 않은 칸은
          // 다른 관리자의 값을 쓴다. 구역 속성 입력과 같다). 확인한 뒤 다시 저장하면 보낸다.
          const latest = readWorkSurfaces(queryClient.getQueryData(STATIC_OBJECTS_KEY)).find(
            (s) => s.objectId === surface.objectId,
          );
          if (latest) {
            const rebased = fieldsChanged(surface.geometry, draft).reduce(
              (geometry, field) => withField(geometry, field, fieldValue(draft, field)),
              latest.geometry,
            );
            useObjectEditStore.getState().keep(surface.objectId, rebased);
            setBases((prev) => ({ ...prev, [surface.objectId]: latest.version }));
          }
        }
        setNotice({ tone: 'warning', text: surfaceSaveErrorText(error) });
      }
    });

  const saveNew = () =>
    run(async () => {
      const finished = finishForSave(NEW_SURFACE_ID);
      if (finished === null) return;
      const draft = finished ?? draftOf(NEW_SURFACE_ID);
      if (!draft) return;
      const problem = firstError(draft);
      if (problem) {
        setNotice({ tone: 'warning', text: problem });
        return;
      }
      try {
        const item = await create.mutateAsync(bodyOf(null, names[NEW_SURFACE_ID] ?? '', draft));
        if (!alive.current) return;
        forget(NEW_SURFACE_ID);
        setCreating(null);
        // 끝내기가 새 상자를 지우며 놓은 선택(null) 뒤에 고른다. 장면에 올라오면 틀이 다시 고르고 초점을 맞춘다.
        selectTarget({ type: 'object', id: item.object_id }, { focus: true });
        setNotice({ tone: 'neutral', text: `만들었습니다 · ${item.object_id}` });
      } catch (error) {
        if (!alive.current) return;
        setNotice({ tone: 'warning', text: surfaceSaveErrorText(error) });
      }
    });

  const saveTarget = () => {
    if (!canEdit || busy) return;
    setNotice(null);
    if (selected) void saveSelected(selected);
    else if (showingNew) void saveNew();
  };

  return {
    canEdit,
    list,
    selected,
    showingNew,
    hasNew: creating !== null,
    targetId,
    geometry,
    name,
    dirty,
    dirtyIds,
    sessionOpen,
    busy,
    can3d: view === '3d' && viewerReady,
    notice: endedNotice ?? notice,
    setNotice,
    startNew,
    pick,
    startEdit,
    finishEdit,
    cancelEdit,
    commitField,
    changeName,
    revert,
    saveTarget,
  };
}
