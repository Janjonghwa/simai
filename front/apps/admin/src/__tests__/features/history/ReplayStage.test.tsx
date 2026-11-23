import { ReplayStage } from '@/features/history/ReplayStage';
import type { UploadProgress } from '@/features/history/replaySegments';
import type { StandbyEvent } from '@/features/history/segmentPrefetch';
import type { Span, VideoSegment } from '@/features/history/types';
import type { WorkReplay } from '@/features/history/useWorkReplay';
import type { ReplayStatus } from '@/features/viewer3d';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});
// 영상 칸은 어느 조각을 틀라고 받았는지와 업로드 중 카메라만 보인다(<video>·S3 는 쓰지 않는다).
vi.mock('@/features/history/ReplayVideoWall', () => ({
  ReplayVideoWall: ({
    segments,
    waiting = [],
    upload = null,
    onFailed,
    onFirstFrames,
    standby = null,
  }: {
    segments: readonly VideoSegment[];
    waiting?: readonly string[];
    upload?: UploadProgress | null;
    onFailed?: (id: string) => void;
    onFirstFrames?: () => void;
    standby?: StandbyEvent | null;
  }) => (
    <>
      <div data-testid="video-wall">{segments.map((s) => s.segment_id).join(',')}</div>
      {/* 대체 모드의 숨긴 영상(S15P31A101-862): 멈춰 둘 시각과 조각 */}
      <div data-testid="video-standby">
        {standby && `${standby.at}:${standby.segments.map((s) => s.segment_id).join(',')}`}
      </div>
      <div data-testid="video-waiting">
        {waiting.join(',')}
        {upload && ` ${upload.uploaded}/${upload.expected ?? '?'}`}
      </div>
      {/* 시험: 영상을 끝내 받지 못한 경우를 흉내 낸다 */}
      <button type="button" onClick={() => segments.forEach((s) => onFailed?.(s.segment_id))}>
        영상 실패
      </button>
      {/* 시험: 영상 칸이 모두 첫 화면을 낸 경우를 흉내 낸다 */}
      <button type="button" onClick={() => onFirstFrames?.()}>
        첫 화면
      </button>
    </>
  ),
}));

// 미리 받기 보관소는 무엇을 줄 세웠는지와 해제, 저장소 받기 막힘(S15P31A101-862)만 본다.
const blobCache = vi.hoisted(() => ({
  prefetched: [] as string[][],
  disposed: 0,
  blocked: false,
  listeners: new Set<() => void>(),
}));
vi.mock('@/features/history/segmentPrefetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/history/segmentPrefetch')>();
  return {
    ...actual,
    createSegmentBlobCache: () => ({
      prefetch: (segments: readonly VideoSegment[]) =>
        blobCache.prefetched.push(segments.map((segment) => segment.segment_id)),
      get: () => undefined,
      isBlocked: () => blobCache.blocked,
      subscribe: (listener: () => void) => {
        blobCache.listeners.add(listener);
        return () => blobCache.listeners.delete(listener);
      },
      dispose: () => (blobCache.disposed += 1),
    }),
  };
});

const T = 1_762_732_800_000;
const RANGE = { from: T, to: T + 600_000 };

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

/** GET /api/videos 요청 기록(요청이 들어온 순서대로 쌓인다). */
let videoRequests: string[] = [];
beforeEach(() => {
  videoRequests = [];
  blobCache.prefetched = [];
  blobCache.disposed = 0;
  blobCache.blocked = false;
  blobCache.listeners.clear();
  useSessionStore.setState({ session: { accessToken: 'token', user: { id: 'a', role: 'ADMIN' } } });
  server.resetHandlers(
    http.get('http://api.test/api/videos', ({ request }) => {
      videoRequests.push(request.url);
      return HttpResponse.json({
        segments: [
          {
            segment_id: 'cover',
            camera_id: 'cam_A',
            segment_start_capture_ms: T,
            segment_end_capture_ms: T + 60_000,
          },
          {
            segment_id: 'later',
            camera_id: 'cam_A',
            segment_start_capture_ms: T + 60_000,
            segment_end_capture_ms: T + 120_000,
          },
        ],
      });
    }),
  );
});
afterEach(() => {
  cleanup();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

function renderStage(session: WorkReplay) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReplayStage session={session} range={RANGE} />
    </QueryClientProvider>,
  );
}

const LOADING: WorkReplay = {
  range: RANGE,
  stepMs: 1000,
  holdMs: 1000,
  loading: true,
  failed: false,
  error: null,
  replay: null,
  index: null,
};

/** 재생 시각이 playhead 에 멈춰 있는 대역 재생 Source. */
function replayAt(playhead: number): WorkReplay['replay'] {
  const status: ReplayStatus = {
    playhead,
    start: null,
    end: null,
    playing: false,
    ended: false,
    speed: 1,
    worldT: null,
    recordAgeMs: null,
    gap: false,
  };
  return { getStatus: () => status, subscribeStatus: () => () => undefined } as never;
}

describe('ReplayStage 영상 조각 목록', () => {
  it('기록을 받는 중에도 영상 조각 목록을 먼저 요청한다', async () => {
    renderStage(LOADING);

    expect(screen.getByText('기록을 불러오는 중입니다…')).toBeTruthy();
    await waitFor(() => expect(videoRequests).toHaveLength(1));
    expect(new URL(videoRequests[0]!).searchParams.get('from')).toBe(String(RANGE.from));
    expect(new URL(videoRequests[0]!).searchParams.get('to')).toBe(String(RANGE.to));
  });

  it('기록이 준비되면 미리 받은 목록으로 재생 시각을 덮는 조각을 보이고 다시 요청하지 않는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (session: WorkReplay) => (
      <QueryClientProvider client={client}>
        <ReplayStage session={session} range={RANGE} />
      </QueryClientProvider>
    );
    const { rerender } = render(view(LOADING));
    await waitFor(() => expect(videoRequests).toHaveLength(1));

    rerender(
      view({
        ...LOADING,
        loading: false,
        replay: replayAt(T + 5_000),
        // 기록이 한 칸이라도 있어야 "잡힌 작업자 없음" 안내 대신 CCTV 칸이 나온다
        index: {
          frames: [{ t: T, ids: [], tracks: [] }],
          workers: [],
          events: [],
          gaps: [],
          pending: null,
        } as never,
      }),
    );

    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    expect(videoRequests).toHaveLength(1);
  });

  it('관리자가 아니면 영상 조각 목록을 요청하지 않는다', async () => {
    useSessionStore.setState({
      session: { accessToken: 'token', user: { id: 'v', role: 'VIEWER' } },
    });
    renderStage(LOADING);

    expect(screen.getByText('기록을 불러오는 중입니다…')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(videoRequests).toHaveLength(0);
  });
});

describe('ReplayStage 아직 받지 못한 뒷구간', () => {
  /** 앞 구간(T 부터 T+1초)만 받은 색인. 그 뒤는 아직 못 받았다. */
  const partial = {
    frames: [{ t: T, ids: [], tracks: [] }],
    workers: [],
    events: [],
    gaps: [],
    pending: { from: T + 1_000, to: RANGE.to },
  } as never;
  const ready = (playhead: number): WorkReplay => ({
    ...LOADING,
    loading: false,
    replay: replayAt(playhead),
    index: partial,
  });

  it('재생 시각이 받지 못한 구간이고 그 시각을 덮는 영상도 없으면 불러오는 중 안내를 보인다', async () => {
    // 영상 조각이 모두 재생 시각(T+5초) 뒤에서 시작한다.
    server.use(
      http.get('http://api.test/api/videos', () =>
        HttpResponse.json({
          segments: [
            {
              segment_id: 'later',
              camera_id: 'cam_A',
              segment_start_capture_ms: T + 60_000,
              segment_end_capture_ms: T + 120_000,
            },
          ],
        }),
      ),
    );
    renderStage(ready(T + 5_000));

    expect(await screen.findByText('기록을 불러오는 중입니다…')).toBeTruthy();
    expect(screen.queryByTestId('video-wall')).toBeNull();
  });

  it('받지 못한 구간이어도 그 시각을 덮는 영상이 있으면 영상은 기록과 상관없이 보인다', async () => {
    renderStage(ready(T + 5_000));

    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    expect(screen.queryByText('기록을 불러오는 중입니다…')).toBeNull();
  });
});

describe('ReplayStage 녹화 업로드 중(S15P31A101-801)', () => {
  const index = {
    frames: [{ t: T, ids: [], tracks: [] }],
    workers: [],
    events: [],
    gaps: [],
    pending: null,
  } as never;
  const uploads = (status: string) => [
    {
      schema_version: '1.0.0',
      work_id: 1,
      status,
      expected_segments: 28,
      uploaded_segments: status === 'COMPLETED' ? 28 : 12,
      failed_segments: 0,
    },
  ];
  /** cam_A 는 재생 시각(T+5초)을 덮는 조각이 있고, cam_B 는 뒷구간 조각만 올라왔다. 응답할 때마다 served 를 센다. */
  let served = 0;
  const videos = (status: string) =>
    http.get('http://api.test/api/videos', () => {
      served += 1;
      return HttpResponse.json({
        segments: [
          {
            segment_id: 'cover',
            camera_id: 'cam_A',
            segment_start_capture_ms: T,
            segment_end_capture_ms: T + 30_000,
          },
          {
            segment_id: 'b-later',
            camera_id: 'cam_B',
            segment_start_capture_ms: T + 30_000,
            segment_end_capture_ms: T + 60_000,
          },
        ],
        recording_uploads: uploads(status),
      });
    });
  const session = (playhead: number): WorkReplay => ({
    ...LOADING,
    loading: false,
    replay: replayAt(playhead),
    index,
  });

  it('업로드 중이면 재생 시각의 조각이 아직 없는 카메라를 진행과 함께 업로드 중 칸으로 넘긴다', async () => {
    server.use(videos('UPLOADING'));
    renderStage(session(T + 5_000));

    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    expect(screen.getByTestId('video-waiting').textContent).toBe('cam_B 12/28');
  });

  it('재생 시각을 덮는 조각이 하나도 없으면 업로드 중이어도 목 화면(3D 기록 먼저 보기)을 두고 칸마다 업로드 배지를 겹친다', async () => {
    server.use(videos('UPLOADING'));
    renderStage(session(T + 70_000));

    const badges = await screen.findAllByText('업로드 중 12/28');
    const tiles = screen.getAllByRole('button', { name: /크게 보기/ });
    expect(badges).toHaveLength(tiles.length);
    expect(tiles.every((tile) => tile.querySelector('svg[role="img"]') !== null)).toBe(true);
    expect(screen.queryByTestId('video-wall')).toBeNull();
  });

  it('업로드가 끝났으면 업로드 배지·업로드 중 칸을 두지 않는다(없는 시각은 목 화면 그대로)', async () => {
    served = 0;
    server.use(videos('COMPLETED'));
    renderStage(session(T + 70_000));

    await waitFor(() => expect(served).toBe(1));
    // 목록을 받은 뒤에도 목 화면(카메라 칸)이 그대로다
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByRole('button', { name: /카메라 1 크게 보기/ })).toBeTruthy();
    expect(screen.queryByText(/업로드 중 \d/)).toBeNull();
    expect(screen.queryByTestId('video-waiting')).toBeNull();
  });
});

describe('ReplayStage 영상 실패', () => {
  it('재생 시각을 덮는 영상을 끝내 받지 못하면 그 조각을 빼고 3D 다시 보기(목 화면)로 돌아간다', async () => {
    renderStage({
      ...LOADING,
      loading: false,
      replay: replayAt(T + 5_000),
      index: {
        frames: [{ t: T, ids: [], tracks: [] }],
        workers: [],
        events: [],
        gaps: [],
        pending: null,
      } as never,
    });

    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toContain('cover'));
    fireEvent.click(screen.getByRole('button', { name: '영상 실패' }));
    await waitFor(() => expect(screen.queryByTestId('video-wall')).toBeNull());
  });

  it('3D 기록이 비어 있으면 상단 영상 문구에서 `3D 기록 먼저 보기`를 빼고 본문에 빈 기록 사유를 보인다', async () => {
    server.use(
      http.get('http://api.test/api/videos', () =>
        HttpResponse.json({
          segments: [],
          recording_uploads: [
            {
              schema_version: '1.0.0',
              work_id: 1,
              status: 'UPLOADING',
              expected_segments: 1,
              uploaded_segments: 0,
              failed_segments: 0,
            },
          ],
        }),
      ),
    );
    renderStage({
      ...LOADING,
      loading: false,
      replay: replayAt(T),
      index: {
        frames: [],
        workers: [],
        events: [],
        gaps: [RANGE],
        pending: null,
      } as never,
    });

    expect(
      await screen.findByText('작업 구간 동안 관제 데이터를 받지 못해 저장된 3D 기록이 없습니다.'),
    ).toBeTruthy();
    const status = await screen.findByRole('status');
    expect(status.textContent).toContain('영상 업로드 중');
    expect(status.textContent).not.toContain('3D 기록 먼저 보기');
  });
});

describe('ReplayStage 3D 다시 보기(목 화면)·빈 기록 사유(S15P31A101-749)', () => {
  const index = (gaps: Span[], frames: unknown[] = []) =>
    ({ frames, workers: [], events: [], gaps, pending: null }) as never;

  it('영상이 없고 기록이 있으면 카메라 칸을 flex 열 안에 두어 남은 높이를 받게 한다', async () => {
    server.use(http.get('http://api.test/api/videos', () => HttpResponse.json({ segments: [] })));
    renderStage({
      ...LOADING,
      loading: false,
      replay: replayAt(T + 5_000),
      index: index([], [{ t: T, ids: [], tracks: [] }]),
    });

    const tile = await screen.findByRole('button', { name: /카메라 1 크게 보기/ });
    // 칸 배치(CctvWall)의 바깥 칸 → 그 부모가 flex 열이어야 컨테이너 높이(cqh)가 0 으로 접히지 않는다.
    const wallRoot = tile.closest('.grid')!.parentElement!.parentElement!;
    expect(wallRoot.parentElement!.className).toMatch(/\bflex\b.*\bflex-col\b/);
  });

  it.each([
    ['1초 미만 작업', { from: T, to: T + 500 }, [], '1초 미만'],
    [
      '일부만 데이터 없음',
      RANGE,
      [{ from: T, to: T + 1_000 }],
      '감지된 작업자와 위험 이벤트가 없습니다',
    ],
  ])('빈 기록 사유: %s', async (_, range, gaps, text) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ReplayStage
          session={{ ...LOADING, range, loading: false, replay: replayAt(T), index: index(gaps) }}
          range={range}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(new RegExp(text))).toBeTruthy();
  });
});

describe('ReplayStage 위험 이벤트 조각 미리 받기', () => {
  const index = {
    frames: [{ t: T, ids: [], tracks: [] }],
    workers: [],
    events: [
      // 위험: 3초 앞(T+67초)은 'later' 조각
      {
        eventId: 'crit',
        ruleId: 'R1',
        severity: 'CRITICAL',
        worldIds: [],
        zoneId: null,
        raisedAt: T + 70_000,
        resolvedAt: null,
      },
      // 주의는 미리 받지 않는다(눌렀을 때 받는다)
      {
        eventId: 'caution',
        ruleId: 'R2',
        severity: 'CAUTION',
        worldIds: [],
        zoneId: null,
        raisedAt: T + 30_000,
        resolvedAt: null,
      },
    ],
    gaps: [],
    pending: null,
  } as never;

  it('지금 보이는 칸이 모두 첫 화면을 낸 뒤에 위험 이벤트 조각만 줄 세우고, 화면을 떠나면 해제한다', async () => {
    const view = renderStage({ ...LOADING, loading: false, replay: replayAt(T + 5_000), index });
    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    expect(blobCache.prefetched).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: '첫 화면' }));
    await waitFor(() => expect(blobCache.prefetched).toEqual([['later']]));

    view.unmount();
    expect(blobCache.disposed).toBeGreaterThan(0);
  });

  it('blob 받기가 되면(CORS 열림) 숨긴 영상을 두지 않는다', async () => {
    renderStage({ ...LOADING, loading: false, replay: replayAt(T + 5_000), index });
    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    fireEvent.click(screen.getByRole('button', { name: '첫 화면' }));
    await waitFor(() => expect(blobCache.prefetched).toEqual([['later']]));
    expect(screen.getByTestId('video-standby').textContent).toBe('');
  });

  it('저장소 받기가 막히면(CORS 차단) 다음 위험 이벤트의 조각을 그 이벤트로 옮겨 갈 시각과 함께 숨긴 영상으로 넘긴다', async () => {
    renderStage({ ...LOADING, loading: false, replay: replayAt(T + 5_000), index });
    await waitFor(() => expect(screen.getByTestId('video-wall').textContent).toBe('cover'));
    fireEvent.click(screen.getByRole('button', { name: '첫 화면' }));
    await waitFor(() => expect(blobCache.prefetched).toEqual([['later']]));

    act(() => {
      blobCache.blocked = true;
      blobCache.listeners.forEach((listener) => listener());
    });
    // 위험 이벤트 T+70초 → 3초 앞 T+67초, 'later' 조각(주의 이벤트는 고르지 않는다)
    await waitFor(() =>
      expect(screen.getByTestId('video-standby').textContent).toBe(`${T + 67_000}:later`),
    );
  });
});
