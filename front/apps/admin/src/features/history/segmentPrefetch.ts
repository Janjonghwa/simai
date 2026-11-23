import { loadPlayableClip } from '@/features/clips/clipMedia';
import { CONTAINER_MP4 } from '@/features/clips/constants';
import { requestVideoUrl } from '@/features/history/api';
import type { EventRow } from '@/features/history/replayIndex';
import { activeSegments } from '@/features/history/replaySegments';
import type { VideoSegment, VideoUrl } from '@/features/history/types';
import type { Http } from '@/lib/http';
import { queryOptions, type QueryClient } from '@tanstack/react-query';

// 다시 보기 영상 조각을 먼저 받아 두기(S15P31A101-859). 화면 없이 시험한다.
// 사용자 결정(2026-11-23): 시연에서는 위험(CRITICAL) 이벤트 부분만 영상으로 확인하므로, 화면의 4칸이 첫 화면을 낸 뒤
// 위험 이벤트 시각마다 그 시각을 덮는 조각을 통째로 받아 blob 으로 둔다. 그 조각이 화면에 올라오면 서명 주소 대신 blob 주소로 튼다.
// 마우스 올리기(hover·focus·pointerdown) 미리 받기는 하지 않는다.

/**
 * 서명 주소(서버 5분)를 이만큼 보관한다. 만료보다 1분 짧게 두어 이미 본 시각으로 돌아가도 주소를 다시 받지 않되, 만료 직전 주소를
 * 새 칸에 넣지 않는다(gcTime·staleTime 모두 이 값, 칸은 붙을 때 이보다 오래된 주소를 쓰지 않는다).
 */
export const VIDEO_URL_CACHE_MS = 4 * 60_000;

/** 미리 받는 위험 이벤트 수(시각순 앞에서부터). 4칸 × 6 = 조각 최대 24개 ≈ 100 MB(조각 약 4 MB). */
export const PREFETCH_MAX_EVENTS = 6;
/** 동시에 받는 조각 수. */
export const PREFETCH_CONCURRENCY = 4;

/** 이벤트를 누르면 이만큼 앞에서부터 튼다(발생 직전을 보이려고). */
export const EVENT_LEAD_MS = 3_000;

/** 이벤트를 눌렀을 때 옮겨 갈 재생 시각. 작업 구간 시작보다 앞으로 가지 않는다. */
export const eventJumpAt = (raisedAt: number, from: number | undefined) =>
  Math.max(from ?? raisedAt, raisedAt - EVENT_LEAD_MS);

/** 조각 재생 주소(서명 주소) 쿼리. 미리 받기와 칸이 같은 키를 쓴다. */
export const videoUrlQuery = (http: Http, segmentId: string) =>
  queryOptions({
    queryKey: ['history', 'video-url', segmentId],
    queryFn: ({ signal }) => requestVideoUrl(http, segmentId, signal),
    gcTime: VIDEO_URL_CACHE_MS,
    staleTime: VIDEO_URL_CACHE_MS,
    retry: false,
  });

/**
 * 미리 받을 조각: 위험 이벤트를 시각순으로 PREFETCH_MAX_EVENTS 개까지, 각 이벤트를 눌렀을 때 옮겨 갈 시각(eventJumpAt)을 덮는
 * 카메라별 조각. 같은 조각은 한 번만, 이벤트 순서대로다. 같은 조각들로 옮겨 가는 이벤트(같은 30초 안)는 한 번으로 센다.
 */
export function criticalPrefetchSegments(
  events: readonly EventRow[],
  segments: readonly VideoSegment[],
  from: number,
  maxEvents = PREFETCH_MAX_EVENTS,
): VideoSegment[] {
  const critical = events
    .filter((event) => event.severity === 'CRITICAL')
    .sort((a, b) => a.raisedAt - b.raisedAt || a.eventId.localeCompare(b.eventId));
  const out: VideoSegment[] = [];
  const taken = new Set<string>();
  let counted = 0;
  for (const event of critical) {
    if (counted >= maxEvents) break;
    const covering = activeSegments(segments, eventJumpAt(event.raisedAt, from));
    const fresh = covering.filter((segment) => !taken.has(segment.segment_id));
    if (fresh.length === 0) continue;
    counted += 1;
    for (const segment of fresh) {
      taken.add(segment.segment_id);
      out.push(segment);
    }
  }
  return out;
}

/** 받아 둔 조각 하나. release 는 blob 주소를 해제한다. */
interface Held {
  url: string;
  release: () => void;
}

export interface SegmentBlobCache {
  /** 받아 둘 조각을 줄 세운다. 이미 받았거나 받는 중·실패한 조각은 건너뛰고, 상한(maxSegments)을 넘는 조각은 받지 않는다. */
  prefetch: (segments: readonly VideoSegment[]) => void;
  /** 받아 둔 조각의 blob 주소. 없으면(아직·실패·해제) undefined. */
  get: (segmentId: string) => string | undefined;
  subscribe: (listener: () => void) => () => void;
  /** 받는 중인 것을 끊고 받아 둔 blob 을 모두 해제한다. 화면을 떠날 때 부른다. 그 뒤로는 받지 않는다. */
  dispose: () => void;
}

export type FetchSegment = (segment: VideoSegment, signal: AbortSignal) => Promise<Held>;

/**
 * 서명 주소로 조각을 통째로 받아 blob 주소로 만든다. 서명 주소는 칸과 같은 쿼리(videoUrlQuery)로 받아 4분 보관을 함께 쓴다.
 * mp4 는 바이트를 그대로 blob 으로, h264-annexb 는 클립과 같은 규칙으로 mp4 로 감싼 blob 으로 둔다.
 * 저장소(S3) CORS 가 이 앱 출처의 GET 을 허용해야 한다(docs exec/02_외부서비스.md 의 버킷 CORS). 막히면 실패로 두고 칸은 서명 주소로 튼다.
 */
export function fetchSegmentBlob(http: Http, client: QueryClient): FetchSegment {
  return async (segment, signal) => {
    const info: VideoUrl = await client.fetchQuery(videoUrlQuery(http, segment.segment_id));
    if (info.container !== CONTAINER_MP4) {
      const clip = await loadPlayableClip(
        { ...info, clip_id: info.segment_id },
        {
          clip_start_capture_ms: segment.segment_start_capture_ms,
          clip_end_capture_ms: segment.segment_end_capture_ms,
        },
        signal,
      );
      return { url: clip.url, release: clip.release };
    }
    const response = await fetch(info.url, { signal, credentials: 'omit' });
    if (!response.ok) throw new Error(`조각 받기 실패 (${response.status})`);
    const blob = await response.blob();
    const url = URL.createObjectURL(new Blob([blob], { type: info.content_type || 'video/mp4' }));
    return { url, release: () => URL.revokeObjectURL(url) };
  };
}

/**
 * 조각 blob 보관소. 줄 세운 순서대로 동시에 concurrency 개까지 받고, 받은 것은 dispose 까지 둔다(다시 보는 동안 해제하지 않는다).
 * 실패한 조각은 다시 받지 않는다(칸은 서명 주소로 튼다).
 */
export function createSegmentBlobCache(
  fetchSegment: FetchSegment,
  {
    concurrency = PREFETCH_CONCURRENCY,
    maxSegments = PREFETCH_MAX_EVENTS * 4,
  }: { concurrency?: number; maxSegments?: number } = {},
): SegmentBlobCache {
  const held = new Map<string, Held>();
  const known = new Set<string>();
  const queue: VideoSegment[] = [];
  const controller = new AbortController();
  const listeners = new Set<() => void>();
  let running = 0;
  let disposed = false;

  const notify = () => listeners.forEach((listener) => listener());

  const pump = () => {
    while (!disposed && running < concurrency && queue.length > 0) {
      const segment = queue.shift()!;
      running += 1;
      fetchSegment(segment, controller.signal)
        .then((result) => {
          if (disposed) {
            result.release();
            return;
          }
          held.set(segment.segment_id, result);
          notify();
        })
        .catch(() => {})
        .finally(() => {
          running -= 1;
          pump();
        });
    }
  };

  return {
    prefetch(segments) {
      if (disposed) return;
      for (const segment of segments) {
        if (known.size >= maxSegments) break;
        if (known.has(segment.segment_id)) continue;
        known.add(segment.segment_id);
        queue.push(segment);
      }
      pump();
    },
    get: (segmentId) => held.get(segmentId)?.url,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      queue.length = 0;
      controller.abort();
      held.forEach((item) => item.release());
      held.clear();
      notify();
      listeners.clear();
    },
  };
}
