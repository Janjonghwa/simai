import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import { LivePlan } from '@/features/plan/LivePlan';
import type {
  CommandResult,
  Viewer3DHandle,
  Viewer3DProps,
  ViewerEditBox,
  ViewerEditCommandResult,
  ViewerEditField,
  ViewerEditPreview,
  ViewerEditResult,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import type { ViewerZones, Zone } from '@/features/zones/types';
import { useZones } from '@/features/zones/useZones';
import { continueOrder, rejectReasonText, sameEditBox } from '@/features/zones/zoneEditor';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import { useZoneDraftsStore, type KeptZoneDraft } from '@/store/zoneDrafts.store';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import {
  createContext,
  lazy,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
  type RefObject,
} from 'react';

export type ViewerModule = { default: ComponentType<Viewer3DProps> };

// three.js 묶음은 3D 를 볼 때만 받는다. 받지 못하거나 3D 가 멈추면 2D 평면으로 바꾼다(명세 13.6 비상 화면).
export const importViewer = (): Promise<ViewerModule> =>
  import('@/features/viewer3d').then((viewer) => ({ default: viewer.Viewer3D }));

export type SpatialViewMode = '3d' | '2d';

/**
 * 화면에 들어와 3D 가 이 시간(ms) 안에 첫 화면을 그리지 못하면 2D 평면을 대신 보인다(명세 13.6). 그보다 빨리 준비되면
 * 탭에 들어올 때마다 2D 가 잠깐 비쳤다 사라지지 않게 2D 를 띄우지 않는다. 2D 를 보다가 3D 로 바꿀 때는 기다리지 않는다.
 */
const PLAN_FALLBACK_MS = 2_000;

/** 3D 에 선택을 다시 적용할 때 장면에 대상이 올라오기를 기다리는 간격(ms)과 횟수(약 3초). */
const RESELECT_MS = 200;
const RESELECT_TRIES = 15;

/** 이벤트 패널·2D 선택 키(person:…·zone:…)를 3D 대상으로 바꾼다. 사람·구역이 없는 묶음 키는 대상이 없다. */
export function targetOfKey(key: string | null): ViewerTarget | null {
  if (!key) return null;
  const at = key.indexOf(':');
  const type = key.slice(0, at);
  const id = key.slice(at + 1);
  return (type === 'person' || type === 'zone') && id ? { type, id } : null;
}

export type { KeptZoneDraft };

/** 미리보기 결과를 보관 목록에 넣는다. 원본과 같으면 그 구역의 보관 항목을 지운다. */
function withKeptDraft(
  prev: Record<string, KeptZoneDraft>,
  preview: ViewerEditPreview,
  after: ViewerEditBox,
): Record<string, KeptZoneDraft> {
  if (sameEditBox(preview.before, after)) {
    if (!(preview.target.id in prev)) return prev;
    const next = { ...prev };
    delete next[preview.target.id];
    return next;
  }
  return {
    ...prev,
    [preview.target.id]: {
      sourceId: preview.provenance.sourceId,
      zoneId: preview.target.id,
      before: structuredClone(preview.before),
      after: structuredClone(after),
      changed: true,
      provenance: { ...preview.provenance },
    },
  };
}

export interface EditNotice {
  tone: 'neutral' | 'warning';
  text: string;
}

export interface SpatialViewportValue {
  viewer: RefObject<Viewer3DHandle | null>;
  view: SpatialViewMode;
  viewerReady: boolean;
  show3d: () => void;
  show2d: () => void;
  selectedKey: string | null;
  setSelectedKey: (key: string | null) => void;
  selectedZoneId: string | null;
  zones: Zone[];
  zonesRaw: ViewerZones | undefined;
  zonesFailed: boolean;
  selectTarget: (
    target: ViewerTarget,
    options?: { focus?: boolean; autoEdit?: boolean },
  ) => CommandResult | undefined;
  clearSelection: () => void;
}

export interface ZoneEditContextValue {
  canEditZones: boolean;
  editPreview: ViewerEditPreview | null;
  editSessionId: number;
  localDraft: KeptZoneDraft | null;
  localDrafts: Readonly<Record<string, KeptZoneDraft>>;
  editNotice: EditNotice | null;
  setEditNotice: (notice: EditNotice | null) => void;
  requestZoneEdit: (
    zoneId: string,
    options?: { focus?: boolean; continueFrom?: ViewerEditBox },
  ) => ViewerEditCommandResult;
  beginZoneEdit: (zoneId: string) => ViewerEditCommandResult;
  continueZoneEdit: (zoneId: string, box: ViewerEditBox) => ViewerEditCommandResult;
  finishZoneEdit: () => ViewerEditResult | null;
  cancelZoneEdit: () => ViewerEditResult | null;
  discardLocalDraft: (zoneId?: string) => void;
  discardAllLocalDrafts: () => void;
  updateZoneField: (field: ViewerEditField, value: number) => ViewerEditCommandResult;
}

const SpatialViewportContext = createContext<SpatialViewportValue | null>(null);
const ZoneEditContext = createContext<ZoneEditContextValue | null>(null);

export const useSpatialViewport = () => useContext(SpatialViewportContext);
export const useZoneEditor = () => useContext(ZoneEditContext);

/**
 * 관제(/)와 구역 편집(/zones)이 공유하는 단일 3D World · 2D 비상 평면 · 우측 이벤트 패널 호스트.
 * AppLayout 의 fullBleed 영역에 하나만 마운트되어 라우트 전환 시에도 Canvas·Renderer·Camera·선택을 유지한다.
 */
export function SpatialViewportHost({
  loadViewer = importViewer,
  planFallbackMs = PLAN_FALLBACK_MS,
  zoneEditor = false,
  children,
}: {
  loadViewer?: () => Promise<ViewerModule>;
  /** 3D 가 준비되지 않았을 때 2D 를 띄우기까지 기다리는 시간(ms). 시험에서 줄인다. */
  planFallbackMs?: number;
  zoneEditor?: boolean;
  children?: ReactNode;
}) {
  const source = useRealtimeStore((s) => s.source);
  const sceneVersion = useRealtimeStore((s) => s.world?.payload.scene_version ?? null);
  const role = useCurrentRole();
  const canEditZones = role === 'ADMIN';
  const viewer = useRef<Viewer3DHandle>(null);
  const { entries, loaded, failed } = useLiveEvents();
  const { zones, raw: zonesRaw, failed: zonesFailed } = useZones();
  const [view, setView] = useState<SpatialViewMode>('3d');
  const [loader, setLoader] = useState(() => ({ attempt: 0, Viewer3D: lazy(loadViewer) }));
  const loadFailed = useRef(false);
  // 3D 가 첫 화면을 그리기 전(코드를 받는 중·초기화 중·멈춤)에는 planBeforeReady 일 때만 2D 평면을 보인다.
  const [viewerReady, setViewerReady] = useState(false);
  // 화면에 들어와 planFallbackMs 가 지났거나, 2D 를 보다가 3D 로 바꿨으면 참이다.
  const [planBeforeReady, setPlanBeforeReady] = useState(false);
  const [selectionState, setSelectionState] = useState<{
    sourceId: string | null;
    key: string | null;
  }>(() => ({ sourceId: source?.id ?? null, key: null }));
  const [editPreview, setEditPreview] = useState<ViewerEditPreview | null>(null);
  const editPreviewRef = useRef<ViewerEditPreview | null>(null);
  const [editSessionId, setEditSessionId] = useState(0);
  // 보관 초안은 이 화면보다 오래 사는 앱 세션 Store 와 맞춘다. 다시 뜨면 Store 값으로 시작하고, 바뀌면 Store 에 옮긴다.
  // Store 를 밖에서 비우면(로그아웃·다른 계정·권한 변경, bindSession) 화면도 따라 비운다.
  const [rawDrafts, setRawDrafts] = useState<Record<string, KeptZoneDraft>>(() => ({
    ...useZoneDraftsStore.getState().drafts,
  }));
  const writingDraftsRef = useRef(false);
  useEffect(() => {
    writingDraftsRef.current = true;
    useZoneDraftsStore.getState().setDrafts(rawDrafts);
    writingDraftsRef.current = false;
  }, [rawDrafts]);
  useEffect(
    () =>
      useZoneDraftsStore.subscribe((state) => {
        if (!writingDraftsRef.current) setRawDrafts({ ...state.drafts });
      }),
    [],
  );
  const rawDraftsRef = useRef(rawDrafts);
  useEffect(() => {
    rawDraftsRef.current = rawDrafts;
  }, [rawDrafts]);
  const [editNotice, setEditNotice] = useState<EditNotice | null>(null);
  const openEventId = useEventsStore((s) => s.openEventId);
  const openEvent = useEventsStore((s) => s.openEvent);
  const syncedKeyRef = useRef<string | null>(null);
  const pendingActionRef = useRef<{
    key: string;
    focus?: boolean;
    autoEdit?: boolean;
    continueFrom?: ViewerEditBox;
  } | null>(null);
  const recoveringContinueRef = useRef(false);
  const isZonesRoute = zoneEditor;
  const wasZonesRoute = useRef(isZonesRoute);
  const currentSourceId = source?.id ?? null;

  const selectedKey = selectionState.sourceId === currentSourceId ? selectionState.key : null;
  const setSelectedKey = useCallback((key: string | null) => {
    if (pendingActionRef.current && pendingActionRef.current.key !== key) {
      pendingActionRef.current = null;
    }
    setSelectionState({ sourceId: useRealtimeStore.getState().source?.id ?? null, key });
  }, []);

  // 보관 중인 다중 구역 로컬 미리보기의 유효성을 현재 Source·Scene 버전·원본 구역 형상과 비교해 파생한다.
  const { localDrafts, staleNotice, invalidDraftIds } = useMemo((): {
    localDrafts: Record<string, KeptZoneDraft>;
    staleNotice: EditNotice | null;
    invalidDraftIds: string[];
  } => {
    const entries = Object.values(rawDrafts);
    if (entries.length === 0 || !canEditZones) {
      return { localDrafts: {}, staleNotice: null, invalidDraftIds: entries.map((d) => d.zoneId) };
    }
    const valid: Record<string, KeptZoneDraft> = {};
    let notice: EditNotice | null = null;
    for (const draft of entries) {
      if (draft.sourceId !== currentSourceId) continue;
      if (
        draft.provenance.sceneVersion !== null &&
        sceneVersion !== null &&
        draft.provenance.sceneVersion !== sceneVersion
      ) {
        notice = {
          tone: 'warning',
          text: `Scene 버전이 바뀌어(${draft.provenance.sceneVersion} → ${sceneVersion}) 이전 미저장 미리보기를 폐기했습니다.`,
        };
        continue;
      }
      if (!zonesRaw) {
        valid[draft.zoneId] = draft;
        continue;
      }
      const current = zones.find((z) => z.zoneId === draft.zoneId);
      if (!current) {
        notice = {
          tone: 'warning',
          text: `원본 구역(${draft.zoneId})이 목록에서 사라져 미저장 미리보기를 폐기했습니다.`,
        };
        continue;
      }
      const currentBox: ViewerEditBox = {
        min: [current.min[0], current.min[1], current.min[2]],
        max: [current.max[0], current.max[1], current.max[2]],
      };
      if (!sameEditBox(currentBox, draft.before)) {
        // 그 초안 형상으로 저장된 구역이 먼저 들어왔다(저장 응답과 초안 정리의 순서는 보장되지 않는다). 알리지 않고 정리한다.
        if (sameEditBox(currentBox, draft.after)) continue;
        notice = {
          tone: 'warning',
          text: `원본 구역(${draft.zoneId}) 형상이 갱신되어 이전 미저장 미리보기를 폐기했습니다.`,
        };
        continue;
      }
      valid[draft.zoneId] = draft;
    }
    return {
      localDrafts: valid,
      staleNotice: notice,
      invalidDraftIds: entries.filter((d) => !valid[d.zoneId]).map((d) => d.zoneId),
    };
  }, [rawDrafts, currentSourceId, canEditZones, sceneVersion, zones, zonesRaw]);

  // 무효화한 항목은 원시 보관 상태에서도 제거하여 조건이 되돌아와도 재등장하지 않게 한다.
  // 현재 컴포넌트의 상태를 유효한 부분집합으로 수렴시킨다. 무효 초안을 한 프레임 더 보관하지 않는다.
  if (invalidDraftIds.length > 0) {
    setRawDrafts(localDrafts);
    if (staleNotice) setEditNotice(staleNotice);
  }

  useEffect(
    () =>
      useRealtimeStore.subscribe((state, prev) => {
        const oldScene = prev.world?.payload.scene_version ?? null;
        const nextScene = state.world?.payload.scene_version ?? null;
        const sourceChanged = state.source?.id !== prev.source?.id;
        const sceneChanged = oldScene !== null && nextScene !== null && oldScene !== nextScene;
        if (!sourceChanged && !sceneChanged) return;
        const hadDrafts =
          Object.keys(rawDraftsRef.current).length > 0 || editPreviewRef.current !== null;
        pendingActionRef.current = null;
        editPreviewRef.current = null;
        rawDraftsRef.current = {};
        setEditPreview(null);
        setRawDrafts({});
        setEditNotice(
          hadDrafts
            ? {
                tone: 'warning',
                text: sourceChanged
                  ? '입력 Source가 바뀌어 이전 미저장 미리보기를 폐기했습니다.'
                  : 'Scene 버전이 바뀌어 이전 미저장 미리보기를 폐기했습니다.',
              }
            : null,
        );
      }),
    [],
  );

  // 권한이 관리자가 아니게 되면 예약된 편집과 진행 중인 편집 세션을 즉시 닫는다.
  useEffect(() => {
    if (canEditZones) return;
    if (pendingActionRef.current?.autoEdit) {
      pendingActionRef.current = null;
    }
    if (editPreview) {
      viewer.current?.cancelEdit();
    }
  }, [canEditZones, editPreview]);

  // /zones 에서 다른 화면(/)으로 이동할 때 편집 중이던 변경이 있으면 미리보기로 보존하고 손잡이만 내린다.
  useEffect(() => {
    if (!isZonesRoute && pendingActionRef.current?.autoEdit) {
      pendingActionRef.current = null;
    }
    if (wasZonesRoute.current && !isZonesRoute && editPreview) {
      if (!sameEditBox(editPreview.before, editPreview.draft)) {
        viewer.current?.finishEdit();
      } else {
        viewer.current?.cancelEdit();
      }
    }
    wasZonesRoute.current = isZonesRoute;
  }, [isZonesRoute, editPreview]);

  const pushZones = useCallback(() => {
    if (source && zonesRaw) source.pushZones({ ok: true, raw: zonesRaw });
  }, [source, zonesRaw]);

  useEffect(() => {
    if (view === '3d') pushZones();
  }, [view, pushZones]);

  const onViewerStatus = useCallback(
    (status: ViewerStatus) => {
      if (status.phase === 'error') {
        syncedKeyRef.current = null;
        pendingActionRef.current = null;
        setView('2d');
        return;
      }
      if (status.phase !== 'ready') return;
      setViewerReady(true);
      pushZones();
    },
    [pushZones],
  );

  useEffect(() => {
    if (view !== '3d' || viewerReady || planBeforeReady) return;
    const id = setTimeout(() => setPlanBeforeReady(true), planFallbackMs);
    return () => clearTimeout(id);
  }, [view, viewerReady, planBeforeReady, planFallbackMs]);

  const onLoadError = useCallback(() => {
    loadFailed.current = true;
    syncedKeyRef.current = null;
    pendingActionRef.current = null;
    setView('2d');
  }, []);

  const show3d = useCallback(() => {
    if (view === '3d') return;
    if (loadFailed.current) {
      loadFailed.current = false;
      setLoader((prev) => ({ attempt: prev.attempt + 1, Viewer3D: lazy(loadViewer) }));
    }
    syncedKeyRef.current = null;
    setViewerReady(false);
    // 보던 2D 를 3D 가 준비될 때까지 그대로 둔다(그 사이 빈 화면을 두지 않는다).
    setPlanBeforeReady(true);
    setView('3d');
  }, [view, loadViewer]);

  const show2d = useCallback(() => {
    syncedKeyRef.current = null;
    pendingActionRef.current = null;
    if (editPreviewRef.current) viewer.current?.finishEdit();
    setView('2d');
  }, []);

  const clearSelection = useCallback(() => {
    syncedKeyRef.current = null;
    pendingActionRef.current = null;
    setSelectedKey(null);
    viewer.current?.clearSelection();
  }, [setSelectedKey]);

  const selectIncident = useCallback(
    (incident: Incident) => {
      setSelectedKey(incident.key);
      if (!incident.target) return;
      const res = viewer.current?.select(incident.target);
      if (res?.ok) {
        syncedKeyRef.current = incident.key;
        viewer.current?.focus(incident.target);
      } else {
        syncedKeyRef.current = null;
        pendingActionRef.current = { key: incident.key, focus: true };
      }
    },
    [setSelectedKey],
  );

  const onViewerSelect = useCallback(
    (target: ViewerTarget | null) => {
      const key =
        target && (target.type === 'person' || target.type === 'zone')
          ? `${target.type}:${target.id}`
          : null;
      syncedKeyRef.current = key;
      if (pendingActionRef.current && pendingActionRef.current.key !== key) {
        pendingActionRef.current = null;
      }
      setSelectedKey(key);
    },
    [setSelectedKey],
  );

  const onEditPreview = useCallback((preview: ViewerEditPreview) => {
    if (!editPreviewRef.current) setEditSessionId((id) => id + 1);
    editPreviewRef.current = preview;
    setEditPreview(preview);
  }, []);

  const keepDraft = useCallback(
    (preview: ViewerEditPreview, after: ViewerEditBox) => {
      if (!canEditZones || preview.provenance.sourceId !== useRealtimeStore.getState().source?.id)
        return;
      setRawDrafts((prev) => withKeptDraft(prev, preview, after));
    },
    [canEditZones],
  );

  // 편집 중에 다른 메뉴(/cctv·/tbm·/system)로 나가 이 화면이 내려가면 진행 중이던 미리보기도 보관한다.
  // 로그아웃·권한 상실·Source 변경으로 내려갈 때는 보관하지 않는다.
  useEffect(
    () => () => {
      const preview = editPreviewRef.current;
      if (
        !preview ||
        useSessionStore.getState().session?.user.role !== 'ADMIN' ||
        preview.provenance.sourceId !== useRealtimeStore.getState().source?.id
      )
        return;
      useZoneDraftsStore
        .getState()
        .setDrafts((prev) => withKeptDraft({ ...prev }, preview, preview.draft));
    },
    [],
  );

  const onEditResult = useCallback(
    (result: ViewerEditResult) => {
      const preview = editPreviewRef.current;
      // 시작 거부는 진행 중인 다른 세션의 Preview를 지우지 않는다.
      if (result.status === 'REJECTED' && preview && result.target?.id !== preview.target.id) {
        setEditNotice({ tone: 'warning', text: rejectReasonText(result.reason, result.detail) });
        return;
      }
      // 3D 가 멈추거나(렌더 오류로 NOT_READY) 내려가면(UNMOUNTED) 하던 편집은 2D 로 바꿀 때처럼 미리보기로 남긴다.
      const interrupted =
        preview !== null &&
        result.target?.id === preview.target.id &&
        ((result.status === 'REJECTED' && result.reason === 'NOT_READY') ||
          (result.status === 'CANCELLED' && result.reason === 'UNMOUNTED'));
      editPreviewRef.current = null;
      setEditPreview(null);
      if (recoveringContinueRef.current) return;
      if (interrupted) {
        keepDraft(preview, preview.draft);
        if (!sameEditBox(preview.before, preview.draft)) {
          setEditNotice({ tone: 'neutral', text: '3D 가 멈춰 편집 중이던 변경을 보관했습니다.' });
        }
        return;
      }
      if (result.status === 'FINISHED') {
        keepDraft({ ...result, draft: result.after }, result.after);
        setEditNotice({
          tone: 'neutral',
          text: result.changed
            ? '미리보기를 유지했습니다.'
            : '원본과 같은 형상으로 돌아와 미저장 변경을 정리했습니다.',
        });
        return;
      }
      if (result.status === 'CANCELLED') {
        if (
          result.reason === 'SELECTION_CHANGED' &&
          preview &&
          preview.target.id === result.target.id
        ) {
          keepDraft(preview, preview.draft);
          setEditNotice({
            tone: 'neutral',
            text: '선택이 바뀌어 마지막 유효 미리보기를 보관했습니다.',
          });
        } else if (result.reason === 'USER') {
          setEditNotice({
            tone: 'neutral',
            text: rawDrafts[result.target.id]
              ? '이번 수정을 취소하고 이전에 보관한 미리보기로 돌아왔습니다.'
              : '이번 수정을 취소하고 원본 구역으로 돌아왔습니다.',
          });
        } else if (result.reason === 'MODE_CHANGED') {
          setEditNotice({ tone: 'neutral', text: '업무 모드가 바뀌어 편집을 취소했습니다.' });
        }
        return;
      }
      if (result.reason === 'STALE_SOURCE') setRawDrafts({});
      setEditNotice({ tone: 'warning', text: rejectReasonText(result.reason, result.detail) });
    },
    [keepDraft, rawDrafts],
  );

  const beginZoneEdit = useCallback(
    (zoneId: string): ViewerEditCommandResult => {
      if (!canEditZones) {
        pendingActionRef.current = null;
        return { ok: false, reason: 'NOT_EDITABLE' };
      }
      const target: ViewerTarget = { type: 'zone', id: zoneId };
      setSelectedKey(`zone:${zoneId}`);
      // 상자 편집기로 고치면 다각형·높이를 상자로 덮어쓰게 되는 구역은 형상 편집을 막는다(속성은 고칠 수 있다).
      if (zones.find((z) => z.zoneId === zoneId)?.shapeEditable === false) {
        setEditNotice({
          tone: 'warning',
          text: rejectReasonText(
            'NOT_EDITABLE',
            '직사각형이 아니거나 높이가 없는 구역은 형상을 고칠 수 없습니다.',
          ),
        });
        return { ok: false, reason: 'NOT_EDITABLE' };
      }
      const handle = viewer.current;
      if (!handle) {
        setEditNotice({ tone: 'warning', text: rejectReasonText('NOT_READY') });
        return { ok: false, reason: 'NOT_READY' };
      }
      const res = handle.beginEdit(target);
      if (res.ok) {
        setEditNotice(null);
      } else {
        setEditNotice({ tone: 'warning', text: rejectReasonText(res.reason) });
      }
      return res;
    },
    [canEditZones, zones, setSelectedKey],
  );

  const continueZoneEdit = useCallback(
    (zoneId: string, box: ViewerEditBox): ViewerEditCommandResult => {
      if (!canEditZones) {
        pendingActionRef.current = null;
        return { ok: false, reason: 'NOT_EDITABLE' };
      }
      const zone = zones.find((z) => z.zoneId === zoneId);
      if (!zone) {
        setEditNotice({ tone: 'warning', text: rejectReasonText('NOT_FOUND') });
        return { ok: false, reason: 'NOT_FOUND' };
      }
      const res = beginZoneEdit(zoneId);
      if (!res.ok || !viewer.current) return res;
      const base: ViewerEditBox = {
        min: [zone.min[0], zone.min[1], zone.min[2]],
        max: [zone.max[0], zone.max[1], zone.max[2]],
      };
      for (const step of continueOrder(base, box)) {
        const stepRes = viewer.current.updateEditField(step);
        if (!stepRes.ok) {
          recoveringContinueRef.current = true;
          viewer.current.cancelEdit();
          recoveringContinueRef.current = false;
          setEditNotice({
            tone: 'warning',
            text: '초안 좌표를 복원하지 못해 원본 구역 상태를 유지했습니다.',
          });
          return stepRes;
        }
      }
      return res;
    },
    [canEditZones, zones, beginZoneEdit],
  );

  const runPendingFor = useCallback(
    (target: ViewerTarget, key: string) => {
      const pending = pendingActionRef.current;
      if (!pending || pending.key !== key) return;
      pendingActionRef.current = null;
      if (pending.focus) viewer.current?.focus(target);
      if (pending.autoEdit && target.type === 'zone') {
        if (!canEditZones) {
          return;
        }
        const resumeBox = pending.continueFrom ?? localDrafts[target.id]?.after;
        if (resumeBox) {
          continueZoneEdit(target.id, resumeBox);
        } else {
          beginZoneEdit(target.id);
        }
      }
    },
    [canEditZones, localDrafts, beginZoneEdit, continueZoneEdit],
  );

  const requestZoneEdit = useCallback(
    (
      zoneId: string,
      options?: { focus?: boolean; continueFrom?: ViewerEditBox },
    ): ViewerEditCommandResult => {
      if (!canEditZones) {
        pendingActionRef.current = null;
        return { ok: false, reason: 'NOT_EDITABLE' };
      }
      if (editPreviewRef.current?.target.id === zoneId) return { ok: true };
      const target: ViewerTarget = { type: 'zone', id: zoneId };
      const key = `zone:${zoneId}`;
      setSelectedKey(key);
      setEditNotice(null);
      const resumeBox = options?.continueFrom ?? localDrafts[zoneId]?.after;

      const zoneLoaded = Boolean(zonesRaw && zones.some((z) => z.zoneId === zoneId));
      if (view !== '3d' || !viewerReady || !zoneLoaded) {
        pendingActionRef.current = {
          key,
          focus: options?.focus,
          autoEdit: true,
          continueFrom: resumeBox,
        };
        syncedKeyRef.current = null;
        if (view !== '3d') show3d();
        return { ok: true };
      }

      const sel = viewer.current?.select(target);
      if (!sel?.ok) {
        pendingActionRef.current = {
          key,
          focus: options?.focus,
          autoEdit: true,
          continueFrom: resumeBox,
        };
        syncedKeyRef.current = null;
        return { ok: true };
      }

      syncedKeyRef.current = key;
      pendingActionRef.current = null;
      if (options?.focus) viewer.current?.focus(target);
      return resumeBox ? continueZoneEdit(zoneId, resumeBox) : beginZoneEdit(zoneId);
    },
    [
      canEditZones,
      setSelectedKey,
      localDrafts,
      zonesRaw,
      zones,
      view,
      viewerReady,
      show3d,
      continueZoneEdit,
      beginZoneEdit,
    ],
  );

  // 구역 편집 모드(/zones)에서 3D 구역을 더블클릭하면 즉시 해당 구역 편집으로 진입한다.
  const onViewerDoubleClick = useCallback(
    (target: ViewerTarget) => {
      if (!isZonesRoute || target.type !== 'zone') return;
      requestZoneEdit(target.id);
    },
    [isZonesRoute, requestZoneEdit],
  );

  const selectTarget = useCallback(
    (
      target: ViewerTarget,
      options?: { focus?: boolean; autoEdit?: boolean },
    ): CommandResult | undefined => {
      const key =
        target.type === 'person' || target.type === 'zone' ? `${target.type}:${target.id}` : null;
      if (key) {
        setSelectedKey(key);
        pendingActionRef.current =
          options?.focus || options?.autoEdit
            ? { key, focus: options.focus, autoEdit: options.autoEdit }
            : null;
      }
      if (view !== '3d' || !viewerReady) {
        syncedKeyRef.current = null;
        return undefined;
      }
      if (target.type === 'zone' && !zones.some((z) => z.zoneId === target.id)) {
        syncedKeyRef.current = null;
        return undefined;
      }
      const res = viewer.current?.select(target);
      if (res?.ok && key) {
        syncedKeyRef.current = key;
        runPendingFor(target, key);
      } else {
        syncedKeyRef.current = null;
      }
      return res;
    },
    [view, viewerReady, zones, runPendingFor, setSelectedKey],
  );

  // 3D 가 준비되거나 늦은 구역 응답이 도착하면 아직 3D 에 반영되지 않은 선택 대상을 단일 루프로 맞춘다.
  useEffect(() => {
    const target = targetOfKey(selectedKey);
    if (view !== '3d' || !viewerReady || !target || !selectedKey) return;
    if (syncedKeyRef.current === selectedKey) return;
    if (target.type === 'zone' && !zonesRaw) {
      return;
    }
    let tries = 0;
    const attempt = () => {
      tries += 1;
      if (viewer.current?.select(target)?.ok === true) {
        syncedKeyRef.current = selectedKey;
        runPendingFor(target, selectedKey);
        return true;
      }
      if (tries >= RESELECT_TRIES) {
        const expired = pendingActionRef.current;
        if (expired?.key === selectedKey) {
          pendingActionRef.current = null;
          if (expired.autoEdit) {
            setEditNotice({ tone: 'warning', text: rejectReasonText('NOT_FOUND') });
          }
        }
        return true;
      }
      return false;
    };
    if (attempt()) return;
    const id = setInterval(() => {
      if (attempt()) clearInterval(id);
    }, RESELECT_MS);
    return () => clearInterval(id);
  }, [view, viewerReady, selectedKey, zones, zonesRaw, runPendingFor]);

  const finishZoneEdit = useCallback((): ViewerEditResult | null => {
    if (!canEditZones) {
      return null;
    }
    return viewer.current?.finishEdit() ?? null;
  }, [canEditZones]);

  const cancelZoneEdit = useCallback((): ViewerEditResult | null => {
    return viewer.current?.cancelEdit() ?? null;
  }, []);

  const selectedZoneId = useMemo(() => {
    const target = targetOfKey(selectedKey);
    return target?.type === 'zone' ? target.id : null;
  }, [selectedKey]);

  const localDraft = useMemo(() => {
    if (selectedZoneId && localDrafts[selectedZoneId]) {
      return localDrafts[selectedZoneId]!;
    }
    const list = Object.values(localDrafts);
    return list.length > 0 ? list[list.length - 1]! : null;
  }, [selectedZoneId, localDrafts]);

  const discardLocalDraft = useCallback(
    (zoneId?: string) => {
      const targetId = zoneId ?? selectedZoneId;
      if (!targetId) {
        setRawDrafts({});
      } else {
        setRawDrafts((prev) => {
          if (!(targetId in prev)) return prev;
          const next = { ...prev };
          delete next[targetId];
          return next;
        });
      }
      setEditNotice({
        tone: 'neutral',
        text: '미저장 미리보기를 버리고 원본 구역으로 되돌렸습니다.',
      });
    },
    [selectedZoneId],
  );

  const discardAllLocalDrafts = useCallback(() => {
    viewer.current?.cancelEdit();
    setRawDrafts({});
    setEditNotice({
      tone: 'neutral',
      text: '보관 중이던 모든 미저장 미리보기를 버리고 원본 구역으로 되돌렸습니다.',
    });
  }, []);

  const updateZoneField = useCallback(
    (field: ViewerEditField, value: number): ViewerEditCommandResult => {
      if (!canEditZones) {
        return { ok: false, reason: 'NOT_EDITABLE' };
      }
      const handle = viewer.current;
      if (!handle) {
        setEditNotice({ tone: 'warning', text: rejectReasonText('NOT_READY') });
        return { ok: false, reason: 'NOT_READY' };
      }
      const res = handle.updateEditField({ field, value });
      if (res.ok) {
        setEditNotice(null);
      } else if (res.reason !== 'INVALID_GEOMETRY') {
        setEditNotice({ tone: 'warning', text: rejectReasonText(res.reason) });
      }
      return res;
    },
    [canEditZones],
  );

  // 실시간 관제(/)에서는 실제 운영 판정 구역만 보이도록 Ghost 를 넘기지 않고, 구역 편집(/zones)에서만 표시한다(R04).
  const draftOverlay = useMemo(() => {
    if (!isZonesRoute || !canEditZones) return null;
    const items = Object.values(localDrafts).map((d) => ({
      target: { type: 'zone' as const, id: d.zoneId },
      box: d.after,
    }));
    if (items.length === 0) return null;
    if (items.length === 1) return items[0]!;
    return items;
  }, [isZonesRoute, canEditZones, localDrafts]);

  const activeNotice = editNotice;

  const viewportValue = useMemo<SpatialViewportValue>(
    () => ({
      viewer,
      view,
      viewerReady,
      show3d,
      show2d,
      selectedKey,
      setSelectedKey,
      selectedZoneId,
      zones,
      zonesRaw,
      zonesFailed,
      selectTarget,
      clearSelection,
    }),
    [
      view,
      viewerReady,
      show3d,
      show2d,
      selectedKey,
      setSelectedKey,
      selectedZoneId,
      zones,
      zonesRaw,
      zonesFailed,
      selectTarget,
      clearSelection,
    ],
  );

  const zoneEditValue = useMemo<ZoneEditContextValue>(
    () => ({
      canEditZones,
      editPreview,
      editSessionId,
      localDraft,
      localDrafts,
      editNotice: activeNotice,
      setEditNotice,
      requestZoneEdit,
      beginZoneEdit,
      continueZoneEdit,
      finishZoneEdit,
      cancelZoneEdit,
      discardLocalDraft,
      discardAllLocalDrafts,
      updateZoneField,
    }),
    [
      canEditZones,
      editPreview,
      editSessionId,
      localDraft,
      localDrafts,
      activeNotice,
      requestZoneEdit,
      beginZoneEdit,
      continueZoneEdit,
      finishZoneEdit,
      cancelZoneEdit,
      discardLocalDraft,
      discardAllLocalDrafts,
      updateZoneField,
    ],
  );

  const { Viewer3D } = loader;
  const ariaLabel = isZonesRoute ? '구역 편집' : '실시간 관제';

  return (
    <SpatialViewportContext value={viewportValue}>
      <ZoneEditContext value={zoneEditValue}>
        <main aria-label={ariaLabel} className="relative flex-1 bg-background">
          {view === '3d' && (
            <ViewerLoadBoundary key={loader.attempt} onError={onLoadError}>
              <Suspense fallback={null}>
                <Viewer3D
                  ref={viewer}
                  source={source}
                  onSelectionChange={onViewerSelect}
                  onTargetDoubleClick={onViewerDoubleClick}
                  onStatusChange={onViewerStatus}
                  onEditPreview={onEditPreview}
                  onEditResult={onEditResult}
                  editDraftOverlay={draftOverlay}
                  className={cn('absolute inset-0', !viewerReady && 'pointer-events-none')}
                />
              </Suspense>
            </ViewerLoadBoundary>
          )}
          {(view === '2d' || (!viewerReady && planBeforeReady)) && (
            <div className="absolute top-[72px] right-[370px] bottom-[96px] left-5">
              <LivePlan selectedKey={selectedKey} onSelect={setSelectedKey} className="size-full" />
            </div>
          )}
          {isZonesRoute && (
            <div
              aria-hidden
              data-testid="zone-edit-fade"
              className="pointer-events-none absolute top-[56px] right-0 bottom-0 left-0 z-[2]"
            >
              <div className="absolute inset-x-0 top-0 h-[14%] bg-linear-to-b from-primary/20 via-primary/7 to-transparent dark:from-primary/26 dark:via-primary/9" />
              <div className="absolute inset-x-0 bottom-0 h-[14%] bg-linear-to-t from-primary/20 via-primary/7 to-transparent dark:from-primary/26 dark:via-primary/9" />
              <div className="absolute inset-y-0 left-0 w-[12%] bg-linear-to-r from-primary/20 via-primary/7 to-transparent dark:from-primary/26 dark:via-primary/9" />
              <div className="absolute inset-y-0 right-0 w-[12%] bg-linear-to-l from-primary/20 via-primary/7 to-transparent dark:from-primary/26 dark:via-primary/9" />
            </div>
          )}
          <div
            role="group"
            aria-label="보기"
            className="absolute top-[72px] left-3.5 z-[5] flex gap-1 rounded-[12px] bg-card/94 p-1 shadow-float backdrop-blur-[8px]"
          >
            <Button
              size="sm"
              variant={view === '3d' ? 'default' : 'ghost'}
              aria-pressed={view === '3d'}
              onClick={show3d}
            >
              3D
            </Button>
            <Button
              size="sm"
              variant={view === '2d' ? 'default' : 'ghost'}
              aria-pressed={view === '2d'}
              onClick={show2d}
            >
              2D
            </Button>
          </div>
          {children}
          <EventPanel
            entries={entries}
            loaded={loaded}
            failed={failed}
            selectedKey={selectedKey}
            onSelect={selectIncident}
            openEventId={openEventId}
            onOpen={openEvent}
          />
        </main>
      </ZoneEditContext>
    </SpatialViewportContext>
  );
}
