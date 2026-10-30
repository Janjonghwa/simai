import { CctvFigures, CctvMockWall } from '@/features/cctv/CctvDemo';
import { cctvPeople, type CctvProjector } from '@/features/cctv/cctvMockView';
import { formatTime } from '@/features/events/events';
import { replayErrorLine } from '@/features/history/replay';
import { allGap, isEmptyReplay, type ReplayIndex } from '@/features/history/replayIndex';
import type { Span } from '@/features/history/types';
import {
  useReplayFrame,
  useReplayMarks,
  useReplayNoData,
  type WorkReplay,
} from '@/features/history/useWorkReplay';
import { type ZonedReplaySource } from '@/features/history/zonedSource';
import { useThemeStore } from '@/features/layout/theme.store';
import { FloorPlan } from '@/features/plan/FloorPlan';
import { PERSON_KEY_PREFIX, personKey } from '@/features/plan/plan';
import type { ReplaySpatialSource, Viewer3DHandle, ViewerTarget } from '@/features/viewer3d';
import { useZones } from '@/features/zones/useZones';
import type { WorldTrack } from '@simai/contracts';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Maximize2, Minimize2 } from 'lucide-react';
import { lazy, Suspense, useCallback, useMemo, useState, type ReactNode, type Ref } from 'react';

/** 작은 창(누르면 크게)에 3D 를 둘지. 지금은 숨겨 두고(코드는 둔다) 같은 자리에 2D 평면을 둔다. */
const SHOW_3D: boolean = false;

// 3D 뷰어는 이 화면만 띄운다(fullBleed 화면이 아니어서 공용 틀의 Canvas 는 내려가 있다). three.js 는 들어올 때 받는다.
const Viewer3D = lazy(() =>
  import('@/features/viewer3d').then((viewer) => ({ default: viewer.Viewer3D })),
);

const NO_TRACKS: WorldTrack[] = [];

// 작은 창은 오른쪽 아래에 있고 누르면 이 칸을 덮는다. CCTV 화면(맨 아래)보다 위(z-10)에 쌓는다.
const WINDOW =
  'absolute z-10 overflow-hidden rounded-[12px] bg-card shadow-lg ring-1 ring-border ring-inset';
const WINDOW_SMALL = 'right-3 bottom-3 h-[200px] w-[320px]';
const WINDOW_WIDE = 'inset-0';

function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-6 text-muted-foreground">
      {children}
    </div>
  );
}

/**
 * CCTV 4화면(목 화면). 재생 시각에 보이는 World 기록의 사람을 그리고, 기록이 바뀔 때만 다시 그린다.
 * 데이터 없음이면 사람을 그리지 않고 흐리게 둔다(마지막 기록을 이어 그리지 않는다, FR-28). 기록이 없어도 기록 없는 구간
 * 밖이면 그 시각에 아무도 없던 것이라 빈 화면을 그대로 보인다.
 */
function ReplayCctv({
  replay,
  index,
  holdMs,
}: {
  replay: ReplaySpatialSource | null;
  index: ReplayIndex | null;
  holdMs: number;
}) {
  const frame = useReplayFrame(replay, index, holdMs);
  const noData = useReplayNoData(replay, index, holdMs);
  const people = useMemo(() => (frame ? cctvPeople(frame.tracks) : []), [frame]);
  const figures = useCallback(
    (project: CctvProjector) => <CctvFigures project={project} people={people} />,
    [people],
  );
  return (
    <CctvMockWall
      figures={figures}
      frozen={noData}
      badge={noData ? '데이터 없음' : frame ? formatTime(frame.t) : '작업자 없음'}
    />
  );
}

/**
 * 2D 평면 다시 보기. 재생 시각의 사람과 그 시각에 활성인 이벤트 표시를 그린다. 데이터 없음이면 흐리게 두고 사람을 지운다.
 * 기록·표시가 바뀔 때만 다시 그린다(재생 시계가 돌 때마다 그리지 않는다).
 */
function ReplayPlan({
  replay,
  index,
  holdMs,
  selectedId,
  onPick,
}: {
  replay: ReplaySpatialSource | null;
  index: ReplayIndex | null;
  holdMs: number;
  selectedId: string | null;
  onPick: (worldId: string) => void;
}) {
  const { zones } = useZones();
  const frame = useReplayFrame(replay, index, holdMs);
  const noData = useReplayNoData(replay, index, holdMs);
  const marks = useReplayMarks(replay, index);
  const select = useCallback(
    (key: string) => {
      if (key.startsWith(PERSON_KEY_PREFIX)) onPick(key.slice(PERSON_KEY_PREFIX.length));
    },
    [onPick],
  );
  return (
    <FloorPlan
      tracks={frame?.tracks ?? NO_TRACKS}
      zones={zones}
      marks={marks}
      selectedKey={selectedId ? personKey(selectedId) : null}
      onSelect={select}
      stale={noData}
      className="size-full"
    />
  );
}

function Replay3D({
  source,
  viewerRef,
  onSelectionChange,
}: {
  source: ZonedReplaySource;
  viewerRef: Ref<Viewer3DHandle>;
  onSelectionChange: (target: ViewerTarget | null) => void;
}) {
  const theme = useThemeStore((s) => s.theme);
  return (
    <Suspense fallback={<Notice>3D 를 불러오는 중입니다…</Notice>}>
      <Viewer3D
        ref={viewerRef}
        source={source}
        initialMode="REPLAY"
        theme={theme}
        onSelectionChange={onSelectionChange}
      />
    </Suspense>
  );
}

/** 작은 창(누르면 크게). 크기만 바꿔 안의 Canvas·평면을 다시 만들지 않는다. */
function ReplayWindow({ label, children }: { label: string; children: ReactNode }) {
  const [wide, setWide] = useState(false);
  return (
    <div role="region" aria-label={label} className={cn(WINDOW, wide ? WINDOW_WIDE : WINDOW_SMALL)}>
      {children}
      {wide ? (
        <Button
          size="icon"
          variant="secondary"
          aria-label="작게 보기"
          aria-expanded
          onClick={() => setWide(false)}
          className="absolute top-2.5 right-2.5"
        >
          <Minimize2 />
        </Button>
      ) : (
        <button
          type="button"
          aria-label="크게 보기"
          aria-expanded={false}
          onClick={() => setWide(true)}
          className="absolute inset-0 flex cursor-zoom-in items-start justify-end p-2 outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Maximize2 aria-hidden className="size-4 text-muted-foreground" />
        </button>
      )}
    </div>
  );
}

interface ReplayStageProps {
  session: WorkReplay;
  /** 작업 구간. */
  range: Span;
  selectedId: string | null;
  viewerRef: Ref<Viewer3DHandle>;
  onPickWorker: (worldId: string) => void;
  onSelectionChange: (target: ViewerTarget | null) => void;
}

/** 화면 가운데 안내 한 줄. 보일 기록이 있으면 null. 기록이 하나도 없을 때 전부 기록 없는 구간이면 데이터 없음이고, 아니면 잡힌 사람이 없던 것이다. */
function stageNotice(session: WorkReplay, range: Span): string | null {
  if (session.failed) return replayErrorLine(session.error);
  if (session.loading || !session.source) return '기록을 불러오는 중입니다…';
  if (!session.index || !isEmptyReplay(session.index)) return null;
  return allGap(session.index.gaps, range) ? '데이터 없음' : '잡힌 작업자가 없습니다.';
}

/** 가운데 칸: CCTV 목 화면 위에 작은 창(3D 또는 2D 평면)을 얹는다. 기록을 받는 중·실패·없음이면 안내만 보인다. */
export function ReplayStage({
  session,
  range,
  selectedId,
  viewerRef,
  onPickWorker,
  onSelectionChange,
}: ReplayStageProps) {
  const { replay, index, holdMs, source } = session;
  const notice = stageNotice(session, range);
  if (notice !== null || source === null) return <Notice>{notice}</Notice>;
  return (
    <>
      <ReplayCctv replay={replay} index={index} holdMs={holdMs} />
      <ReplayWindow label={SHOW_3D ? '3D 다시 보기' : '2D 평면 다시 보기'}>
        {SHOW_3D ? (
          <Replay3D source={source} viewerRef={viewerRef} onSelectionChange={onSelectionChange} />
        ) : (
          <ReplayPlan
            replay={replay}
            index={index}
            holdMs={holdMs}
            selectedId={selectedId}
            onPick={onPickWorker}
          />
        )}
      </ReplayWindow>
    </>
  );
}
