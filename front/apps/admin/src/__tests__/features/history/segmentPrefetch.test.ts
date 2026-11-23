import type { EventRow } from '@/features/history/replayIndex';
import {
  createSegmentBlobCache,
  criticalPrefetchSegments,
  eventJumpAt,
  fetchSegmentBlob,
  PREFETCH_CONCURRENCY,
  PREFETCH_MAX_EVENTS,
  VIDEO_URL_CACHE_MS,
  videoUrlQuery,
  type FetchSegment,
} from '@/features/history/segmentPrefetch';
import type { VideoSegment } from '@/features/history/types';
import { createHttp } from '@/lib/http';
import { QueryClient } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// 위험 이벤트 조각 미리 받기(S15P31A101-859, 사용자 결정): 고를 조각·상한·동시 받기·blob 해제.

const T = 1_762_732_800_000;
const CAMERAS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
const SEG_MS = 30_000;

/** T 부터 30초 조각, 카메라 4대, n 개 구간. */
const segments: VideoSegment[] = Array.from({ length: 20 }, (_, i) =>
  CAMERAS.map((camera_id) => ({
    segment_id: `${camera_id}-${i}`,
    camera_id,
    segment_start_capture_ms: T + i * SEG_MS,
    segment_end_capture_ms: T + (i + 1) * SEG_MS,
  })),
).flat();

const event = (id: string, raisedAt: number, severity: EventRow['severity']): EventRow => ({
  eventId: id,
  ruleId: 'R1',
  severity,
  worldIds: [],
  zoneId: null,
  raisedAt,
  resolvedAt: null,
});

const ids = (list: readonly VideoSegment[]) => list.map((segment) => segment.segment_id);

describe('eventJumpAt', () => {
  it('이벤트 3초 앞으로 가되 작업 구간 시작보다 앞으로 가지 않는다', () => {
    expect(eventJumpAt(T + 10_000, T)).toBe(T + 7_000);
    expect(eventJumpAt(T + 1_000, T)).toBe(T);
  });
});

describe('criticalPrefetchSegments', () => {
  it('위험 이벤트만, 시각순으로, 이벤트를 눌렀을 때 옮겨 갈 시각을 덮는 카메라별 조각을 고른다', () => {
    const picked = criticalPrefetchSegments(
      [
        event('late', T + 5 * SEG_MS + 10_000, 'CRITICAL'),
        event('caution', T + 2 * SEG_MS + 10_000, 'CAUTION'),
        event('high', T + 3 * SEG_MS + 10_000, 'HIGH'),
        // 조각 1 시작 2초 뒤 → 3초 앞은 조각 0
        event('early', T + SEG_MS + 2_000, 'CRITICAL'),
      ],
      segments,
      T,
    );
    expect(ids(picked)).toEqual([
      'cam_A-0',
      'cam_B-0',
      'cam_C-0',
      'cam_D-0',
      'cam_A-5',
      'cam_B-5',
      'cam_C-5',
      'cam_D-5',
    ]);
  });

  it('같은 조각으로 옮겨 가는 이벤트는 한 번만 받고 한 번으로 센다', () => {
    const picked = criticalPrefetchSegments(
      [event('a', T + 10_000, 'CRITICAL'), event('b', T + 20_000, 'CRITICAL')],
      segments,
      T,
    );
    expect(ids(picked)).toEqual(['cam_A-0', 'cam_B-0', 'cam_C-0', 'cam_D-0']);
  });

  it('위험 이벤트는 앞에서부터 6개까지(조각 최대 24개)만 받는다', () => {
    const many = Array.from({ length: 10 }, (_, i) =>
      event(`e${i}`, T + i * SEG_MS + 10_000, 'CRITICAL'),
    );
    const picked = criticalPrefetchSegments(many, segments, T);
    expect(PREFETCH_MAX_EVENTS).toBe(6);
    expect(picked).toHaveLength(24);
    expect(ids(picked).at(-1)).toBe('cam_D-5');
  });

  it('위험 이벤트가 없으면 받지 않는다', () => {
    expect(criticalPrefetchSegments([event('c', T + 10_000, 'CAUTION')], segments, T)).toEqual([]);
  });
});

/** 손으로 끝내는 가짜 받기. */
function controlledFetch() {
  const pending = new Map<
    string,
    { resolve: () => void; reject: () => void; signal: AbortSignal }
  >();
  const released: string[] = [];
  let running = 0;
  let maxRunning = 0;
  const fetchSegment: FetchSegment = (segment, signal) =>
    new Promise((resolve, reject) => {
      running += 1;
      maxRunning = Math.max(maxRunning, running);
      const done = () => (running -= 1);
      pending.set(segment.segment_id, {
        signal,
        resolve: () => {
          done();
          resolve({
            url: `blob:${segment.segment_id}`,
            release: () => released.push(segment.segment_id),
          });
        },
        reject: () => {
          done();
          reject(new Error('실패'));
        },
      });
    });
  return { fetchSegment, pending, released, maxRunning: () => maxRunning };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('createSegmentBlobCache', () => {
  it('동시에 4개까지만 받고, 끝나는 대로 다음 조각을 받는다', async () => {
    const fake = controlledFetch();
    const cache = createSegmentBlobCache(fake.fetchSegment);
    cache.prefetch(segments.slice(0, 8));
    expect(fake.pending.size).toBe(PREFETCH_CONCURRENCY);
    fake.pending.get('cam_A-0')!.resolve();
    await flush();
    expect(fake.pending.size).toBe(5);
    expect(fake.maxRunning()).toBe(4);
    expect(cache.get('cam_A-0')).toBe('blob:cam_A-0');
    expect(cache.get('cam_B-0')).toBeUndefined();
  });

  it('같은 조각은 한 번만 받고 상한(24개)을 넘는 조각은 받지 않는다', async () => {
    const calls: string[] = [];
    const cache = createSegmentBlobCache(async (segment) => {
      calls.push(segment.segment_id);
      return { url: `blob:${segment.segment_id}`, release: () => {} };
    });
    cache.prefetch(segments.slice(0, 4));
    cache.prefetch(segments.slice(0, 4));
    cache.prefetch(segments.slice(4, 40));
    for (let i = 0; i < 10; i += 1) await flush();
    expect(calls).toHaveLength(24);
    expect(new Set(calls).size).toBe(24);
  });

  it('받으면 구독자에게 알리고, 실패한 조각은 비워 둔 채 다시 받지 않는다', async () => {
    const fake = controlledFetch();
    const cache = createSegmentBlobCache(fake.fetchSegment);
    const listener = vi.fn();
    cache.subscribe(listener);
    cache.prefetch(segments.slice(0, 2));
    fake.pending.get('cam_A-0')!.resolve();
    fake.pending.get('cam_B-0')!.reject();
    await flush();
    expect(listener).toHaveBeenCalledTimes(1);
    expect(cache.get('cam_B-0')).toBeUndefined();
    fake.pending.clear();
    cache.prefetch(segments.slice(0, 2));
    expect(fake.pending.size).toBe(0);
  });

  it('화면을 떠나면(dispose) 받는 중인 것을 끊고 받아 둔 blob 을 모두 해제하며, 그 뒤에 끝난 것도 해제한다', async () => {
    const fake = controlledFetch();
    const cache = createSegmentBlobCache(fake.fetchSegment);
    cache.prefetch(segments.slice(0, 6));
    fake.pending.get('cam_A-0')!.resolve();
    await flush();
    const late = fake.pending.get('cam_B-0')!;

    cache.dispose();
    expect(late.signal.aborted).toBe(true);
    expect(fake.released).toEqual(['cam_A-0']);
    expect(cache.get('cam_A-0')).toBeUndefined();

    late.resolve();
    await flush();
    expect(fake.released).toEqual(['cam_A-0', 'cam_B-0']);
    // 줄 서 있던 조각은 받지 않고, 새로 줄 세워도 받지 않는다
    expect(fake.pending.has('cam_C-1')).toBe(false);
    cache.prefetch(segments.slice(10, 12));
    expect(fake.pending.has('cam_C-2')).toBe(false);
  });
});

describe('videoUrlQuery', () => {
  it('서명 주소(5분)를 만료보다 짧은 4분 보관한다', () => {
    const query = videoUrlQuery(
      createHttp({ baseUrl: 'http://api.test', getToken: () => 't' }),
      'x',
    );
    expect(VIDEO_URL_CACHE_MS).toBe(240_000);
    expect(query.gcTime).toBe(VIDEO_URL_CACHE_MS);
    expect(query.staleTime).toBe(VIDEO_URL_CACHE_MS);
    expect(query.queryKey).toEqual(['history', 'video-url', 'x']);
  });
});

describe('fetchSegmentBlob', () => {
  let urlRequests = 0;
  const server = setupServer(
    http.get('http://api.test/api/videos/:id', ({ params }) => {
      urlRequests += 1;
      return HttpResponse.json({
        segment_id: params.id,
        url: `https://s3.test/${String(params.id)}.mp4`,
        expires_at_ms: T + 300_000,
        content_type: 'video/mp4',
        container: 'mp4',
      });
    }),
    http.get('https://s3.test/:file', () => new HttpResponse(new Uint8Array([0, 0, 0, 24]))),
  );
  beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
  afterAll(() => server.close());
  let restore = () => {};
  afterEach(() => restore());

  it('서명 주소를 칸과 같은 쿼리로 받아(보관 재사용) 조각 전체를 blob 주소로 만들고, release 로 해제한다', async () => {
    const created: Blob[] = [];
    const revoked: string[] = [];
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = (blob: Blob | MediaSource) => {
      created.push(blob as Blob);
      return `blob:${created.length}`;
    };
    URL.revokeObjectURL = (url: string) => void revoked.push(url);
    restore = () => {
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    };
    const api = createHttp({ baseUrl: 'http://api.test', getToken: () => 't' });
    const client = new QueryClient();
    const load = fetchSegmentBlob(api, client);
    const signal = new AbortController().signal;

    const first = await load(segments[0]!, signal);
    expect(first.url).toBe('blob:1');
    expect(created[0]!.size).toBe(4);
    expect(created[0]!.type).toBe('video/mp4');
    // 같은 조각의 서명 주소는 보관한 것을 쓴다
    await load(segments[0]!, signal);
    expect(urlRequests).toBe(1);

    first.release();
    expect(revoked).toEqual(['blob:1']);
  });
});
