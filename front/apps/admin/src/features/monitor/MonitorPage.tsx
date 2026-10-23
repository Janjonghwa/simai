import { EventPanel } from '@/features/events/EventPanel';
import type { Incident } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { ViewerLoadBoundary } from '@/features/monitor/ViewerLoadBoundary';
import { LivePlan } from '@/features/plan/LivePlan';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { useZones } from '@/features/zones/useZones';
import { useRealtimeStore } from '@/store/realtime.store';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
} from 'react';

// 3D 장면(또는 2D 평면)이 화면을 채우고, 상단 바·메뉴·이벤트 패널이 그 위에 뜬다. 경보 배너는 모든 화면에 뜨도록 레이아웃(AppLayout)이 그린다.
// 데이터는 앱의 STOMP 연결이 세션마다 만든 Source 로 받는다(lib/stomp.ts). 로그아웃하면 Source 가 바뀌어 장면 데이터가 비워진다.
// 경보 대상 강조는 뷰어가 같은 Source 의 risk_event 로 한다(S15P31A101-185). 여기서는 목록과 3D 선택만 잇는다.

type ViewerModule = { default: ComponentType<Viewer3DProps> };

// three.js 묶음은 3D 를 볼 때만 받는다. 받지 못하거나 3D 가 멈추면 2D 평면으로 바꾼다(명세 13.6 비상 화면).
const importViewer = (): Promise<ViewerModule> =>
  import('@/features/viewer3d').then((viewer) => ({ default: viewer.Viewer3D }));

type View = '3d' | '2d';

/**
 * 화면에 들어와 3D 가 이 시간(ms) 안에 첫 화면을 그리지 못하면 2D 평면을 대신 보인다(명세 13.6). 그보다 빨리 준비되면
 * 탭에 들어올 때마다 2D 가 잠깐 비쳤다 사라지지 않게 2D 를 띄우지 않는다. 2D 를 보다가 3D 로 바꿀 때는 기다리지 않는다.
 */
const PLAN_FALLBACK_MS = 2_000;

/** 3D 에 선택을 다시 적용할 때 장면에 대상이 올라오기를 기다리는 간격(ms)과 횟수(약 3초). */
const RESELECT_MS = 200;
const RESELECT_TRIES = 15;

/** 이벤트 패널 선택 키(person:…·zone:…)를 3D 대상으로 바꾼다. 사람·구역이 없는 묶음 키는 대상이 없다. */
function targetOfKey(key: string | null): ViewerTarget | null {
  if (!key) return null;
  const at = key.indexOf(':');
  const type = key.slice(0, at);
  const id = key.slice(at + 1);
  return (type === 'person' || type === 'zone') && id ? { type, id } : null;
}

export function MonitorPage({
  loadViewer = importViewer,
  planFallbackMs = PLAN_FALLBACK_MS,
}: {
  /** 3D 뷰어 코드를 불러온다. 시험에서 바꿔 넣는다. */
  loadViewer?: () => Promise<ViewerModule>;
  /** 3D 가 준비되지 않았을 때 2D 를 띄우기까지 기다리는 시간(ms). 시험에서 줄인다. */
  planFallbackMs?: number;
} = {}) {
  const source = useRealtimeStore((s) => s.source);
  const viewer = useRef<Viewer3DHandle>(null);
  const { entries, loaded, failed } = useLiveEvents();
  const { raw: zonesRaw } = useZones();
  const [view, setView] = useState<View>('3d');
  // 불러오기에 실패한 lazy 는 같은 오류를 계속 던진다. 그 뒤 3D 로 다시 바꾸면 새로 불러온다.
  const [loader, setLoader] = useState(() => ({ attempt: 0, Viewer3D: lazy(loadViewer) }));
  const loadFailed = useRef(false);
  // 3D 가 첫 화면을 그리기 전(코드를 받는 중·초기화 중·멈춤)에는 planBeforeReady 일 때만 2D 평면을 보인다.
  const [viewerReady, setViewerReady] = useState(false);
  // 화면에 들어와 planFallbackMs 가 지났거나, 2D 를 보다가 3D 로 바꿨으면 참이다.
  const [planBeforeReady, setPlanBeforeReady] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const openEventId = useEventsStore((s) => s.openEventId);
  const openEvent = useEventsStore((s) => s.openEvent);

  // 3D 뷰어도 같은 구역을 그리게 넘긴다. Source 는 지금 구독 중인 뷰어에만 알리므로 뷰어가 준비됐을 때도 넘긴다.
  const pushZones = useCallback(() => {
    if (source && zonesRaw) source.pushZones({ ok: true, raw: zonesRaw });
  }, [source, zonesRaw]);
  useEffect(() => {
    if (view === '3d') pushZones();
  }, [view, pushZones]);

  const onViewerStatus = useCallback(
    (status: ViewerStatus) => {
      if (status.phase === 'error') {
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

  // 3D 가 준비되면 2D 에서 고른 대상을 3D 에도 고른다(뷰어는 새로 띄우면 선택이 없다).
  // 준비 직후에는 사람·구역이 아직 장면에 올라오지 않았을 수 있어(NOT_FOUND) 될 때까지 잠깐 다시 시도한다.
  useEffect(() => {
    const target = targetOfKey(selectedKey);
    if (view !== '3d' || !viewerReady || !target) return;
    let tries = 0;
    const attempt = () => viewer.current?.select(target)?.ok === true || ++tries >= RESELECT_TRIES;
    if (attempt()) return;
    const id = setInterval(() => {
      if (attempt()) clearInterval(id);
    }, RESELECT_MS);
    return () => clearInterval(id);
  }, [view, viewerReady, selectedKey]);

  const onLoadError = useCallback(() => {
    loadFailed.current = true;
    setView('2d');
  }, []);

  const show3d = () => {
    // 이미 3D 면 그대로 둔다(뷰어가 다시 뜨지 않아 준비됐다는 알림도 다시 오지 않는다).
    if (view === '3d') return;
    if (loadFailed.current) {
      loadFailed.current = false;
      setLoader((prev) => ({ attempt: prev.attempt + 1, Viewer3D: lazy(loadViewer) }));
    }
    setViewerReady(false);
    // 보던 2D 를 3D 가 준비될 때까지 그대로 둔다(그 사이 빈 화면을 두지 않는다).
    setPlanBeforeReady(true);
    setView('3d');
  };

  // 카드를 고르면 3D 에서 그 사람(또는 구역)을 고르고 비춘다. 장면에 없으면 뷰어가 실패만 돌려준다. 2D 는 selectedKey 로 표시한다.
  const selectIncident = (incident: Incident) => {
    setSelectedKey(incident.key);
    if (!incident.target) return;
    viewer.current?.select(incident.target);
    viewer.current?.focus(incident.target);
  };

  // 3D 에서 사람·구역을 고르면 그 묶음을 목록에서 표시한다.
  const onViewerSelect = (target: ViewerTarget | null) =>
    setSelectedKey(
      target && (target.type === 'person' || target.type === 'zone')
        ? `${target.type}:${target.id}`
        : null,
    );

  const { Viewer3D } = loader;
  const planShown = view === '2d' || (!viewerReady && planBeforeReady);

  return (
    <main aria-label="실시간 관제" className="relative flex-1 bg-background">
      <h1 className="sr-only">실시간 관제</h1>
      {view === '3d' && (
        <ViewerLoadBoundary key={loader.attempt} onError={onLoadError}>
          <Suspense fallback={null}>
            <Viewer3D
              ref={viewer}
              source={source}
              onSelectionChange={onViewerSelect}
              onStatusChange={onViewerStatus}
              // 준비 전에는 2D 평면이 덮지 않는 가장자리에서도 3D 를 조작하지 않게 한다.
              className={cn('absolute inset-0', !viewerReady && 'pointer-events-none')}
            />
          </Suspense>
        </ViewerLoadBoundary>
      )}
      {planShown && (
        <div className="absolute top-[72px] right-[370px] bottom-[96px] left-5">
          <LivePlan selectedKey={selectedKey} onSelect={setSelectedKey} className="size-full" />
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
          onClick={() => setView('2d')}
        >
          2D
        </Button>
      </div>
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
  );
}
