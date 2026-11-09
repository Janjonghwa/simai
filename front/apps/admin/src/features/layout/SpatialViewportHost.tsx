import { useCamSplitStore } from '@/features/cctv/camSplit.store';
import { CamSplitPanel } from '@/features/cctv/CamSplitPanel';
import { CctvFloat, saveCctvFloatCamera, type CctvCameraRequest } from '@/features/cctv/CctvFloat';
import {
  MONITOR_LAYERS,
  PLAN_FALLBACK_MS,
  ZONE_EDIT_LAYERS,
} from '@/features/layout/spatialHost/constants';
import { HostEventPanel } from '@/features/layout/spatialHost/HostEventPanel';
import { PlanOverlay } from '@/features/layout/spatialHost/PlanOverlay';
import { ToolPalette } from '@/features/layout/spatialHost/ToolPalette';
import { useBoxEditProps } from '@/features/layout/spatialHost/useBoxEditProps';
import {
  importViewer,
  type SpatialViewMode,
  type ViewerModule,
} from '@/features/layout/spatialHost/useViewerMode';
import { useViewportHost } from '@/features/layout/spatialHost/useViewportHost';
import { ZoneEditFade } from '@/features/layout/spatialHost/ZoneEditFade';
import { useThemeStore } from '@/features/layout/theme.store';
import { useCurrentCalibration } from '@/features/layout/useCurrentCalibration';
import { useZoneEditActive } from '@/features/monitor/useZoneEditActive';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import { PlanMinimap } from '@/features/plan/PlanMinimap';
import type { CommandResult, ViewerTarget } from '@/features/viewer3d';
import { useWatchBadges, useWatchBindDetector } from '@/features/watch/useWatch';
import { useWatchBindEffectStore } from '@/features/watch/watchBindEffect.store';
import type { ViewerZones, Zone } from '@/features/zones/types';
import { avatarMeshFetch } from '@/lib/avatarMeshFetch';
import { useRealtimeStore } from '@/store/realtime.store';
import { cn } from '@simai/ui/lib/utils';
import { createContext, Suspense, useCallback, useContext, useState, type ReactNode } from 'react';

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
  const {
    viewer,
    scene,
    sceneStatus,
    onSceneStatusChange,
    view,
    viewerReady,
    showPlan,
    Viewer3D,
    loadAttempt,
    onLoadError,
    show3d,
    selectedKey,
    setSelectedKey,
    selectIncident,
    onViewerSelect,
    onViewerStatus,
    viewport,
  } = useViewportHost({ source, loadViewer, planFallbackMs });
  const boxEditProps = useBoxEditProps();
  const editing = useZoneEditActive();
  const [cctvOpen, setCctvOpen] = useState(false);
  const [requestedCamera, setRequestedCamera] = useState<CctvCameraRequest | null>(null);
  const split = useCamSplitStore((s) => s.split);
  const { cameraPoses: calibratedCameraPoses } = useCurrentCalibration();

  // 편집 모드에서는 이벤트 패널·카메라 분할 창·2D 미니맵을 두지 않고 구역 편집에 화면을 준다(맵 재생성은 도구 모음이 숨긴다).
  const showEventPanel = eventPanel && !editing;
  // 작업면은 관리자 편집 모드(이벤트 패널이 없는 호스트도 같다)에서 고르고 고친다. 관제에서는 보이기만 한다.
  const layers = editing || !eventPanel ? ZONE_EDIT_LAYERS : MONITOR_LAYERS;
  // 사람 이름표 뒤 워치 채널 상태(-342). 배정·연결이 바뀔 때만 새 객체다
  const personBadges = useWatchBadges();
  // 손 흔들어 워치가 새로 이어진 사람 강조(-509). 편집 모드에서는 숨긴다
  useWatchBindDetector();
  const bindHighlights = useWatchBindEffectStore((s) => s.highlights);
  const personHighlights = editing ? undefined : bindHighlights;
  const cameraPoses = editing ? undefined : calibratedCameraPoses;

  const handleCameraClick = useCallback(
    (cameraId: string) => {
      if (!eventPanel || editing) return;
      if (useCamSplitStore.getState().split) useCamSplitStore.getState().close();
      saveCctvFloatCamera(cameraId);
      setRequestedCamera((prev) => ({ cameraId, seq: (prev?.seq ?? 0) + 1 }));
      setCctvOpen(true);
    },
    [eventPanel, editing],
  );

  return (
    <SpatialViewportContext value={viewport}>
      <main aria-label="실시간 관제" className="relative flex-1 bg-background">
        {view === '3d' && sceneStatus?.regenerationRequired && (
          <div
            role="status"
            className="pointer-events-none absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded bg-card px-3 py-2 text-sm text-muted-foreground shadow-float"
          >
            재생성 필요
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
                personBadges={personBadges}
                personHighlights={personHighlights}
                cameraPoses={cameraPoses}
                onCameraClick={handleCameraClick}
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
          onToggleCctv={
            eventPanel && !editing
              ? () => {
                  if (split) useCamSplitStore.getState().close();
                  setCctvOpen((open) => (split ? true : !open));
                }
              : undefined
          }
          cctvOpen={cctvOpen && !split && !editing}
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
        {showEventPanel && cctvOpen && !split && (
          <CctvFloat
            selectedKey={selectedKey}
            requestedCamera={requestedCamera}
            onClose={() => setCctvOpen(false)}
          />
        )}
      </main>
    </SpatialViewportContext>
  );
}
