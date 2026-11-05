import { ec2Now } from '@/features/events/format';
import { replayKey, requestReplay, requestWorks } from '@/features/history/api';
import { chooseStep, fitLog, holdFor, replayFiles } from '@/features/history/replay';
import {
  frameAt,
  indexReplay,
  noDataAt,
  type Frame,
  type ReplayIndex,
} from '@/features/history/replayIndex';
import type { ReplayResponse, Span, WorkUnit, WorkUnitsResponse } from '@/features/history/types';
import { readWorks, replayRange, startedWorks } from '@/features/history/works';
import type { ReplaySpatialSource, ReplayStatus } from '@/features/viewer3d';
import { api } from '@/lib/api';
import { HISTORY_WORKS_KEY } from '@/lib/queryKeys';
import { useRealtimeStore } from '@/store/realtime.store';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';

/** 응답에서 형식이 맞고 시작한 작업만 고른다. 함수가 바뀌지 않아야 받은 목록을 렌더마다 다시 검사하지 않는다. */
const selectStartedWorks = (response: WorkUnitsResponse) => startedWorks(readWorks(response.works));

/** 시작한 최근 작업 목록(서버 시작 기록 기준). 받기 전이면 works 가 undefined. */
export function useWorks() {
  const query = useQuery({
    queryKey: HISTORY_WORKS_KEY,
    queryFn: ({ signal }) => requestWorks(api, signal),
    select: selectStartedWorks,
  });
  return { works: query.data, failed: query.isError };
}

// 재생 엔진은 three.js 를 끌고 오는 입구에 있어 값은 동적 import 로 부른다(CONVENTION).
const loadViewer = () => import('@/features/viewer3d');
type Viewer = Awaited<ReturnType<typeof loadViewer>>;

interface Built {
  data: ReplayResponse;
  replay: ReplaySpatialSource;
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

/** 재생 엔진은 구독자가 있을 때만 시계를 돌린다. 받은 메시지는 쓰지 않는 구독자다. */
const KEEP_CLOCK = { onMessage: () => {}, onConnectionChange: () => {} };

const playheadOf = (replay: ReplaySpatialSource | null) => replay?.getStatus().playhead ?? null;

/**
 * 구간 기록으로 재생 Source·화면 목록을 만든다.
 * 기록의 LIVE 이벤트는 replayFiles 가 REPLAY 출처로 바꿔 넣는다(엔진은 REPLAY 만 받는다).
 */
function buildReplay(viewer: Viewer, data: ReplayResponse, range: Span, holdMs: number): Built {
  const log = fitLog(viewer.parseReplayLog(replayFiles(data)), range);
  const replay = viewer.createReplaySource(log, { gapMs: holdMs, autoplay: false });
  return { data, replay, index: indexReplay(log, data.gaps), holdMs };
}

/** 작업 구간 기록(GET /api/replay). 같은 구간은 한 번 받으면 다시 받지 않는다. */
function useReplayData(work: WorkUnit | undefined, range: Span | null, stepMs: number) {
  return useQuery({
    queryKey: replayKey(work?.work_id ?? '', range ?? { from: 0, to: 0 }),
    queryFn: ({ signal }) => requestReplay(api, { ...range!, stepMs }, signal),
    enabled: work !== undefined && range !== null,
    staleTime: Infinity,
  });
}

/** 받은 기록으로 재생 Source 를 만든다(재생 엔진 입구를 받은 뒤). 기록이 바뀌면 새로 만들고, 만들지 못하면 loadFailed 다. */
function useBuiltReplay(range: Span | null, stepMs: number, data: ReplayResponse | undefined) {
  const [built, setBuilt] = useState<Built | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    if (!data || !range) return;
    let alive = true;
    // 서버가 요청보다 넓은 간격으로 줄 수 있다. 요청 간격으로 잡으면 기록 사이가 비어 보인다.
    const holdMs = holdFor(Math.max(stepMs, data.step_ms));
    loadViewer().then(
      (viewer) => {
        if (alive) setBuilt(buildReplay(viewer, data, range, holdMs));
      },
      () => {
        if (alive) setLoadFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [data, range, stepMs]);

  return { built, loadFailed };
}

/** 재생 Source 의 수명: 떠나거나 바뀌면 멈추고, 보는 동안 시계를 돌려 둔다. */
function useReplayPlayback(built: Built | null) {
  // 떠나거나 다른 기록으로 바뀌면 멈춘다(타이머를 남기지 않는다).
  useEffect(() => () => built?.replay.pause(), [built]);

  // CCTV·타임라인이 재생되게 구독해 둔다(재생 엔진은 구독자가 있을 때만 시계를 돌린다).
  useEffect(() => built?.replay.subscribe(KEEP_CLOCK), [built]);
}

/** 작업 구간 다시 보기의 상태. 기록을 다 만들기 전이면 replay·index 가 null 이다. */
export interface WorkReplay {
  /** 다시 보는 구간(실제 시작~종료, 진행 중이면 연 시각까지). 길이가 없으면 null 이다. */
  range: Span | null;
  stepMs: number;
  holdMs: number;
  loading: boolean;
  failed: boolean;
  error: Error | null;
  replay: ReplaySpatialSource | null;
  index: ReplayIndex | null;
}

/** 지금의 EC2 시각(화면 시계에 서버와의 차이를 더한다). */
const ec2Clock = () => ec2Now(Date.now(), useRealtimeStore.getState().health?.clockOffsetMs);

/** 작업이 처음 보인 때의 EC2 시각. 작업이 보이기 전이면 null 이다(목록을 받는 중이거나 아직 시작하지 않은 작업). */
function useSeenAt(workId: string | undefined): number | null {
  const [seen, setSeen] = useState<{ workId: string; at: number } | null>(null);
  // 렌더 중 맞춤(WatchPanel 과 같은 방식): 그 작업이 보이는 첫 렌더에서 한 번만 정한다(다른 작업으로 바뀌면 다시 정한다).
  if (workId !== undefined && seen?.workId !== workId) setSeen({ workId, at: ec2Clock() });
  return seen !== null && seen.workId === workId ? seen.at : null;
}

/**
 * 작업 구간 다시 보기. 실제 시작~종료 구간의 기록을 받아 재생 Source(-187)를 만든다.
 * 진행 중인 작업은 그 작업이 화면에 처음 보인 때(EC2 시계)까지를 보고(화면을 연 뒤에 시작한 작업도 시작 뒤 구간이 된다),
 * 끝나 작업이 바뀌면 끝난 구간으로 다시 받는다.
 * 기록 간격은 구간 길이로 정한다(chooseStep). 화면을 떠나면 멈춘다. 재생 상태는 useReplayStatus 로 따로 읽는다.
 */
export function useWorkReplay(work: WorkUnit | undefined): WorkReplay {
  const seenAt = useSeenAt(work?.work_id);
  const range = useMemo(
    () => (work && seenAt !== null ? replayRange(work, seenAt) : null),
    [work, seenAt],
  );
  const stepMs = range ? chooseStep(range.from, range.to) : 0;
  const query = useReplayData(work, range, stepMs);
  const { built, loadFailed } = useBuiltReplay(range, stepMs, query.data);
  useReplayPlayback(built);

  const ready = built !== null && built.data === query.data;
  return {
    range,
    stepMs,
    holdMs: ready ? built.holdMs : holdFor(stepMs),
    loading: range !== null && !ready && !query.isError && !loadFailed,
    failed: query.isError || loadFailed,
    error: query.error,
    replay: ready ? built.replay : null,
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
 * 부른다(화면 전체를 0.1초마다 다시 그리지 않는다).
 */
export function useReplayStatus(replay: ReplaySpatialSource | null): ReplayStatus {
  return useSyncExternalStore(useStatusSubscribe(replay), replay ? replay.getStatus : idle);
}

/**
 * 지금 보이는 World 기록(재생 시각 이하 마지막 기록, holdMs 안). 없으면 null 이다. 기록이 바뀔 때만 다시 그린다(CCTV).
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
