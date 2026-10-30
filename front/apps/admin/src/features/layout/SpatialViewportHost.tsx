import { PLAN_FALLBACK_MS } from '@/features/layout/spatialHost/constants';
import { HostEventPanel } from '@/features/layout/spatialHost/HostEventPanel';
import { PlanOverlay } from '@/features/layout/spatialHost/PlanOverlay';
import { useBoxEditProps } from '@/features/layout/spatialHost/useBoxEditProps';
import { useSpatialSnapshots } from '@/features/layout/spatialHost/useSpatialSnapshots';
import {
  importViewer,
  useViewerMode,
  type SpatialViewMode,
  type ViewerModule,
} from '@/features/layout/spatialHost/useViewerMode';
import { useViewerSelection } from '@/features/layout/spatialHost/useViewerSelection';
import { ViewControls } from '@/features/layout/spatialHost/ViewControls';
import { ZoneEditFade } from '@/features/layout/spatialHost/ZoneEditFade';
import { useThemeStore } from '@/features/layout/theme.store';
import { useCurrentScene } from '@/features/layout/useCurrentScene';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import { PlanMinimap } from '@/features/plan/PlanMinimap';
import type {
  CommandResult,
  Viewer3DHandle,
  ViewerLayers,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import type { ViewerZones, Zone } from '@/features/zones/types';
import { useZones } from '@/features/zones/useZones';
import { avatarMeshFetch } from '@/lib/avatarMeshFetch';
import { useRealtimeStore } from '@/store/realtime.store';
import { cn } from '@simai/ui/lib/utils';
import {
  createContext,
  Suspense,
  useCallback,
  useContext,
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

const SpatialViewportContext = createContext<SpatialViewportValue | null>(null);

export const useSpatialViewport = () => useContext(SpatialViewportContext);

/**
 * 관제(/)와 구역 편집(/zones)이 공유하는 단일 3D World · 2D 비상 평면 · 우측 이벤트 패널 호스트.
 * AppLayout 의 fullBleed 영역에 하나만 마운트되어 라우트 전환 시에도 Canvas·Renderer·Camera·선택을 유지한다.
 */
export function SpatialViewportHost({
  loadViewer = importViewer,
  planFallbackMs = PLAN_FALLBACK_MS,
  zoneEditor = false,
  eventPanel = true,
  children,
}: {
  loadViewer?: () => Promise<ViewerModule>;
  /** 3D 가 준비되지 않았을 때 2D 를 띄우기까지 기다리는 시간(ms). 시험에서 줄인다. */
  planFallbackMs?: number;
  zoneEditor?: boolean;
  /** 오른쪽 이벤트 패널과 왼쪽 아래 2D 미니맵을 두는 화면인지(라우트 staticData.eventPanel, 실시간 관제). */
  eventPanel?: boolean;
  children?: ReactNode;
}) {
  const source = useRealtimeStore((s) => s.source);
  const theme = useThemeStore((s) => s.theme);
  const viewer = useRef<Viewer3DHandle>(null);
  const { zones, raw: zonesRaw, failed: zonesFailed } = useZones();
  const { scene, onSceneStatusChange } = useCurrentScene();
  const boxEditProps = useBoxEditProps();
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

  const showEventPanel = eventPanel && !zoneEditor;
  // 작업면은 등록 화면(이벤트 패널도 구역 편집도 아닌 화면)에서만 고르고 고친다. 관제·구역 편집에서는 보이기만 해
  // 사람·구역 선택을 가로막지 않는다. 장애물은 고치는 화면이 없어 어디서도 고르지 않는다.
  const layers = useMemo<ViewerLayers>(
    () => ({
      workSurfaces: eventPanel || zoneEditor ? 'visible' : 'interactive',
      obstacles: 'visible',
    }),
    [eventPanel, zoneEditor],
  );

  return (
    <SpatialViewportContext value={viewportValue}>
      <main
        aria-label={zoneEditor ? '구역 편집' : '실시간 관제'}
        className="relative flex-1 bg-background"
      >
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
        {zoneEditor && <ZoneEditFade />}
        <ViewControls
          view={view}
          onResetView={() => viewer.current?.resetView?.()}
          onRetry3d={show3d}
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
      </main>
    </SpatialViewportContext>
  );
}
