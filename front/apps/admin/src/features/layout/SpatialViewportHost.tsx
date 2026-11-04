import { CamSplitPanel } from '@/features/cctv/CamSplitPanel';
import { PLAN_FALLBACK_MS } from '@/features/layout/spatialHost/constants';
import { HostEventPanel } from '@/features/layout/spatialHost/HostEventPanel';
import { PlanOverlay } from '@/features/layout/spatialHost/PlanOverlay';
import { ToolPalette } from '@/features/layout/spatialHost/ToolPalette';
import { useBoxEditProps } from '@/features/layout/spatialHost/useBoxEditProps';
import { useSpatialSnapshots } from '@/features/layout/spatialHost/useSpatialSnapshots';
import {
  importViewer,
  useViewerMode,
  type SpatialViewMode,
  type ViewerModule,
} from '@/features/layout/spatialHost/useViewerMode';
import { useViewerSelection } from '@/features/layout/spatialHost/useViewerSelection';
import { ZoneEditFade } from '@/features/layout/spatialHost/ZoneEditFade';
import { useThemeStore } from '@/features/layout/theme.store';
import { useCurrentScene } from '@/features/layout/useCurrentScene';
import { useZoneEditActive } from '@/features/monitor/useZoneEditActive';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import { PlanMinimap } from '@/features/plan/PlanMinimap';
import type {
  CommandResult,
  Viewer3DHandle,
  ViewerLayers,
  ViewerSceneStatus,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import type { ViewerZones, Zone } from '@/features/zones/types';
import { useZones } from '@/features/zones/useZones';
import { avatarMeshFetch } from '@/lib/avatarMeshFetch';
import { usableScanBounds } from '@/lib/roomLimit';
import { useRealtimeStore } from '@/store/realtime.store';
import { useRoomBoundsStore } from '@/store/roomBounds.store';
import { cn } from '@simai/ui/lib/utils';
import {
  createContext,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from 'react';

export interface SpatialViewportValue {
  view: SpatialViewMode;
  viewerReady: boolean;
  selectedKey: string | null;
  selectedZoneId: string | null;
  zones: Zone[];
  zonesRaw: ViewerZones | undefined;
  zonesFailed: boolean;
  selectTarget: (target: ViewerTarget, options?: { focus?: boolean }) => CommandResult | undefined;
  clearSelection: () => void;
}

export const SpatialViewportContext = createContext<SpatialViewportValue | null>(null);

export const useSpatialViewport = () => useContext(SpatialViewportContext);

function readSceneVersion(scene: unknown): string | null {
  if (typeof scene !== 'object' || scene === null) return null;
  const val: unknown = Reflect.get(scene, 'scene_version');
  return typeof val === 'string' ? val : null;
}

/**
 * 뷰어가 알린 장면 상태에서 보이는 스캔의 경계를 꺼내 호스트 저장소에 올린다(새 구역 자리·머리글이 읽는다).
 * 스캔이 숨겨졌거나 쓸 수 없거나 3D 를 보고 있지 않을 때, 그리고 호스트가 내려갈 때는 비워 목업 교실로 돌아간다.
 */
function usePublishRoomBounds(view: SpatialViewMode, sceneStatus: ViewerSceneStatus | null) {
  const shown =
    view === '3d' && sceneStatus && sceneStatus.display !== 'HIDDEN'
      ? usableScanBounds(sceneStatus.boundsWorld)
      : null;
  useEffect(() => {
    useRoomBoundsStore.getState().set(shown);
  }, [shown]);
  useEffect(() => () => useRoomBoundsStore.getState().set(null), []);
}

/**
 * 실시간 관제(/)의 3D World · 2D 비상 평면 · 도구 모음 · 우측 이벤트 패널 호스트. 관리자 편집 모드(구역 편집)도
 * 이 화면 위에서 한다. AppLayout 이 fullBleed 인 화면(관제 하나뿐)에서만 마운트하므로 다른 화면으로 가면 내려가고,
 * 돌아오면 3D 를 새로 띄운다.
 */
export function SpatialViewportHost({
  loadViewer = importViewer,
  planFallbackMs = PLAN_FALLBACK_MS,
  eventPanel = true,
  children,
}: {
  loadViewer?: () => Promise<ViewerModule>;
  /** 3D 가 준비되지 않았을 때 2D 를 띄우기까지 기다리는 시간(ms). 시험에서 줄인다. */
  planFallbackMs?: number;
  /** 오른쪽 이벤트 패널과 왼쪽 아래 2D 미니맵을 두는 화면인지(라우트 staticData.eventPanel, 실시간 관제). */
  eventPanel?: boolean;
  children?: ReactNode;
}) {
  const source = useRealtimeStore((s) => s.source);
  const theme = useThemeStore((s) => s.theme);
  const viewer = useRef<Viewer3DHandle>(null);
  const { zones, raw: zonesRaw, failed: zonesFailed } = useZones();
  const { scene, sceneStatus, onSceneStatusChange } = useCurrentScene();
  const boxEditProps = useBoxEditProps();
  const editing = useZoneEditActive();
  const {
    view,
    viewerReady,
    showPlan,
    Viewer3D,
    loadAttempt,
    onViewerReady,
    onViewerError,
    onLoadError,
    show3d,
  } = useViewerMode(loadViewer, planFallbackMs);
  usePublishRoomBounds(view, sceneStatus);
  const pushSnapshots = useSpatialSnapshots(view, zonesRaw);
  const {
    selectedKey,
    selectedZoneId,
    setSelectedKey,
    selectTarget,
    clearSelection,
    selectIncident,
    onViewerSelect,
  } = useViewerSelection({ viewer, view, viewerReady, zones, zonesRaw });

  const onViewerStatus = useCallback(
    (status: ViewerStatus) => {
      if (status.phase === 'error') {
        onViewerError();
      } else if (status.phase === 'ready') {
        onViewerReady();
        pushSnapshots();
      }
    },
    [onViewerError, onViewerReady, pushSnapshots],
  );

  const viewportValue = useMemo<SpatialViewportValue>(
    () => ({
      view,
      viewerReady,
      selectedKey,
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
      selectedKey,
      selectedZoneId,
      zones,
      zonesRaw,
      zonesFailed,
      selectTarget,
      clearSelection,
    ],
  );

  // 편집 모드에서는 이벤트 패널·카메라 분할 창·2D 미니맵을 두지 않고 구역 편집에 화면을 준다(맵 재생성은 도구 모음이 숨긴다).
  const showEventPanel = eventPanel && !editing;
  // 작업면은 관리자 편집 모드(이벤트 패널이 없는 호스트도 같다)에서 고르고 고친다(구역 없는 작업면을 지우려면 골라야 한다).
  // 관제에서는 보이기만 해 사람·구역 선택을 가로막지 않는다. 장애물은 고치는 화면이 없어 어디서도 고르지 않는다.
  const layers = useMemo<ViewerLayers>(
    () => ({
      workSurfaces: editing || !eventPanel ? 'interactive' : 'visible',
      obstacles: 'visible',
    }),
    [eventPanel, editing],
  );

  return (
    <SpatialViewportContext value={viewportValue}>
      <main aria-label="실시간 관제" className="relative flex-1 bg-background">
        {view === '3d' &&
          (sceneStatus?.provisionalBackground || sceneStatus?.regenerationRequired) && (
            <div
              role="status"
              className="pointer-events-none absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded bg-card px-3 py-2 text-sm text-muted-foreground shadow-float"
            >
              {sceneStatus.provisionalBackground && <span>임시 보정 기준 배경</span>}
              {sceneStatus.provisionalBackground && sceneStatus.regenerationRequired && ' · '}
              {sceneStatus.regenerationRequired && <span>재생성 필요</span>}
            </div>
          )}
        {view === '3d' && (
          <ViewerLoadBoundary key={loadAttempt} onError={onLoadError}>
            <Suspense fallback={null}>
              <Viewer3D
                ref={viewer}
                theme={theme}
                source={source}
                avatarMeshFetch={avatarMeshFetch}
                scene={scene}
                layers={layers}
                onSceneStatusChange={onSceneStatusChange}
                onSelectionChange={onViewerSelect}
                onStatusChange={onViewerStatus}
                {...boxEditProps}
                className={cn('absolute inset-0', !viewerReady && 'pointer-events-none')}
              />
            </Suspense>
          </ViewerLoadBoundary>
        )}
        {showPlan && (
          <PlanOverlay
            withEventPanel={showEventPanel}
            selectedKey={selectedKey}
            onSelect={setSelectedKey}
          />
        )}
        {editing && <ZoneEditFade />}
        <ToolPalette
          view={view}
          onResetView={() => viewer.current?.resetView?.()}
          onRetry3d={show3d}
          currentSceneVersion={readSceneVersion(scene)}
          currentScene={scene?.ok ? scene.raw : undefined}
        />
        {children}
        {showEventPanel && view === '3d' && (
          <PlanMinimap selectedKey={selectedKey} onSelect={setSelectedKey} />
        )}
        <HostEventPanel
          visible={showEventPanel}
          selectedKey={selectedKey}
          onSelect={selectIncident}
        />
        {showEventPanel && <CamSplitPanel />}
      </main>
    </SpatialViewportContext>
  );
}
