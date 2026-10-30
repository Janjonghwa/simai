import { replayKey, requestReplay, requestWorks, WORKS_KEY } from '@/features/history/api';
import { chooseStep, fitLog, holdFor, replayFiles } from '@/features/history/replay';
import {
  frameAt,
  indexReplay,
  markableAt,
  marksOf,
  noDataAt,
  type Frame,
  type ReplayIndex,
} from '@/features/history/replayIndex';
import type { ReplayResponse, WorkUnit, WorkUnitsResponse } from '@/features/history/types';
import { readWorks } from '@/features/history/works';
import { withZones, type ZonedReplaySource } from '@/features/history/zonedSource';
import type { PlanMarks } from '@/features/plan/plan';
import type { ReplaySpatialSource, ReplayStatus } from '@/features/viewer3d';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';

/** 응답에서 형식이 맞는 작업만 고른다. 함수가 바뀌지 않아야 받은 목록을 렌더마다 다시 검사하지 않는다. */
const selectWorks = (response: WorkUnitsResponse) => readWorks(response.works);

/** 최근 작업 목록. 받기 전이면 works 가 undefined. */
export function useWorks() {
  const query = useQuery({
    queryKey: WORKS_KEY,
    queryFn: ({ signal }) => requestWorks(api, signal),
    select: selectWorks,
  });
  return { works: query.data, failed: query.isError };
}

// 재생 엔진은 three.js 를 끌고 오는 입구에 있어 값은 동적 import 로 부른다(CONVENTION).
const loadViewer = () => import('@/features/viewer3d');
type Viewer = Awaited<ReturnType<typeof loadViewer>>;

interface Built {
  data: ReplayResponse;
  replay: ReplaySpatialSource;
  source: ZonedReplaySource;
  index: ReplayIndex;
  /** World 기록 하나가 보이는 시간(ms). 서버가 요청보다 넓은 간격으로 주었으면 그 간격으로 잡는다. */
  holdMs: number;
}

const IDLE: ReplayStatus = {
  playhead: null,
  start: null,
  end: null,
  playing: false,
  ended: false,
  speed: 1,
  worldT: null,
  recordAgeMs: null,
  gap: false,
};
const idle = () => IDLE;

const NO_MARKS: PlanMarks = { people: new Map(), zones: new Map() };

/** 재생 엔진은 구독자가 있을 때만 시계를 돌린다. 받은 메시지는 쓰지 않는 구독자다. */
const KEEP_CLOCK = { onMessage: () => {}, onConnectionChange: () => {} };

const playheadOf = (replay: ReplaySpatialSource | null) => replay?.getStatus().playhead ?? null;

/**
 * 구간 기록으로 재생 Source·구역을 함께 넣는 Source·화면 목록을 만든다.
 * 기록의 LIVE 이벤트는 replayFiles 가 REPLAY 출처로 바꿔 넣는다(엔진은 REPLAY 만 받는다).
 */
function buildReplay(viewer: Viewer, data: ReplayResponse, work: WorkUnit, holdMs: number): Built {
  const log = fitLog(viewer.parseReplayLog(replayFiles(data)), {
    from: work.from_ms,
    to: work.to_ms,
  });
  const replay = viewer.createReplaySource(log, { gapMs: holdMs, autoplay: false });
  return { data, replay, source: withZones(replay), index: indexReplay(log, data.gaps), holdMs };
}

/** 작업 구간 기록(GET /api/replay). 한 번 받으면 다시 받지 않는다. */
function useReplayData(work: WorkUnit | undefined, stepMs: number) {
  return useQuery({
    queryKey: replayKey(work?.work_id ?? ''),
    queryFn: ({ signal }) =>
      requestReplay(api, { from: work!.from_ms, to: work!.to_ms, stepMs }, signal),
    enabled: work !== undefined,
    staleTime: Infinity,
  });
}

/** 받은 기록으로 재생 Source 를 만든다(3D 입구를 받은 뒤). 기록이 바뀌면 새로 만들고, 만들지 못하면 loadFailed 다. */
function useBuiltReplay(
  work: WorkUnit | undefined,
  stepMs: number,
  data: ReplayResponse | undefined,
) {
  const [built, setBuilt] = useState<Built | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    if (!data || !work) return;
    let alive = true;
    // 서버가 요청보다 넓은 간격으로 줄 수 있다. 요청 간격으로 잡으면 기록 사이가 비어 보인다.
    const holdMs = holdFor(Math.max(stepMs, data.step_ms));
    loadViewer().then(
      (viewer) => {
        if (alive) setBuilt(buildReplay(viewer, data, work, holdMs));
      },
      () => {
        if (alive) setLoadFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [data, work, stepMs]);

  return { built, loadFailed };
}

/** 재생 Source 의 수명: 떠나거나 바뀌면 멈추고, 시계를 돌려 두고, 지금 구역을 넣는다. */
function useReplayPlayback(built: Built | null, zonesRaw: unknown) {
  // 떠나거나 다른 기록으로 바뀌면 멈춘다(타이머를 남기지 않는다).
  useEffect(() => () => built?.replay.pause(), [built]);

  // 3D 를 띄우지 않거나 3D 를 아직 받는 중이어도 CCTV·타임라인이 재생되게 따로 구독해 둔다.
  useEffect(() => built?.replay.subscribe(KEEP_CLOCK), [built]);

  useEffect(() => {
    if (built && zonesRaw !== undefined) built.source.setZones(zonesRaw);
  }, [built, zonesRaw]);
}

/** 작업 구간 다시 보기의 상태. 기록을 다 만들기 전이면 replay·source·index 가 null 이다. */
export interface WorkReplay {
  stepMs: number;
  holdMs: number;
  loading: boolean;
  failed: boolean;
  error: Error | null;
  replay: ReplaySpatialSource | null;
  source: ZonedReplaySource | null;
  index: ReplayIndex | null;
}

/**
 * 작업 구간 다시 보기. 구간 기록을 받아 재생 Source(-187)를 만들고, 지금 구역을 함께 넣는다(zonesRaw).
 * 기록 간격은 구간 길이로 정한다(chooseStep). 화면을 떠나면 멈춘다. 재생 상태는 useReplayStatus 로 따로 읽는다.
 */
export function useWorkReplay(work: WorkUnit | undefined, zonesRaw: unknown): WorkReplay {
  const stepMs = work ? chooseStep(work.from_ms, work.to_ms) : 0;
  const query = useReplayData(work, stepMs);
  const { built, loadFailed } = useBuiltReplay(work, stepMs, query.data);
  useReplayPlayback(built, zonesRaw);

  const ready = built !== null && built.data === query.data;
  return {
    stepMs,
    holdMs: ready ? built.holdMs : holdFor(stepMs),
    loading: work !== undefined && !ready && !query.isError && !loadFailed,
    failed: query.isError || loadFailed,
    error: query.error,
    replay: ready ? built.replay : null,
    source: ready ? built.source : null,
    index: ready ? built.index : null,
  };
}

const useStatusSubscribe = (replay: ReplaySpatialSource | null) =>
  useCallback(
    (listener: () => void) => (replay ? replay.subscribeStatus(listener) : () => {}),
    [replay],
  );

/**
 * 재생 상태(재생 시각·재생 중·배속·데이터 없음). 재생 중에는 0.1초마다 바뀌므로 그 값을 보이는 작은 컴포넌트에서만
 * 부른다(CONVENTION: 3D 가 있는 화면 전체를 다시 그리지 않는다).
 */
export function useReplayStatus(replay: ReplaySpatialSource | null): ReplayStatus {
  return useSyncExternalStore(useStatusSubscribe(replay), replay ? replay.getStatus : idle);
}

/**
 * 지금 보이는 World 기록(재생 시각 이하 마지막 기록, holdMs 안). 없으면 null 이다. 기록이 바뀔 때만 다시 그린다(CCTV·평면).
 * 기록 객체는 index 안의 것이라 같은 기록이면 같은 값이다.
 */
export function useReplayFrame(
  replay: ReplaySpatialSource | null,
  index: ReplayIndex | null,
  holdMs: number,
): Frame | null {
  return useSyncExternalStore(useStatusSubscribe(replay), () => {
    const playhead = playheadOf(replay);
    return index && playhead !== null ? frameAt(index, playhead, holdMs) : null;
  });
}

/** 지금 시각이 "데이터 없음"인지(보이는 기록이 없고 서버가 기록 없는 구간이라 한 시각). 바뀔 때만 다시 그린다. */
export function useReplayNoData(
  replay: ReplaySpatialSource | null,
  index: ReplayIndex | null,
  holdMs: number,
): boolean {
  return useSyncExternalStore(useStatusSubscribe(replay), () => {
    const playhead = playheadOf(replay);
    return index !== null && playhead !== null && noDataAt(index, playhead, holdMs);
  });
}

/**
 * 재생 시각의 이벤트 표시를 돌려주는 읽기 함수. 표시할 이벤트가 지난번과 같으면 지난번 값을 그대로 준다
 * (useSyncExternalStore 는 같은 값이면 다시 그리지 않는다).
 */
function createMarksSelector(
  replay: ReplaySpatialSource | null,
  index: ReplayIndex | null,
): () => PlanMarks {
  let lastKey = '';
  let lastMarks = NO_MARKS;
  return () => {
    const playhead = playheadOf(replay);
    const events = index && playhead !== null ? markableAt(index.events, playhead) : [];
    const key = events.map((event) => event.eventId).join('\n');
    if (key !== lastKey) {
      lastKey = key;
      lastMarks = marksOf(events);
    }
    return lastMarks;
  };
}

/**
 * 지금 2D 평면에 얹을 이벤트 표시. 표시할 이벤트가 바뀔 때만 새 값이다(재생 시계가 0.1초마다 돌아도 평면의 구역 층은
 * 다시 그려지지 않는다).
 */
export function useReplayMarks(
  replay: ReplaySpatialSource | null,
  index: ReplayIndex | null,
): PlanMarks {
  const select = useMemo(() => createMarksSelector(replay, index), [replay, index]);
  return useSyncExternalStore(useStatusSubscribe(replay), select);
}
