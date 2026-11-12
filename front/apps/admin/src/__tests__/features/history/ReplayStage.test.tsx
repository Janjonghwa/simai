import { ReplayStage } from '@/features/history/ReplayStage';
import type { VideoSegment } from '@/features/history/types';
import type { WorkReplay } from '@/features/history/useWorkReplay';
import type { ReplayStatus } from '@/features/viewer3d';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});
// 영상 칸은 어느 조각을 틀라고 받았는지만 보인다(<video>·S3 는 쓰지 않는다).
vi.mock('@/features/history/ReplayVideoWall', () => ({
  ReplayVideoWall: ({ segments }: { segments: readonly VideoSegment[] }) => (
    <div data-testid="video-wall">{segments.map((s) => s.segment_id).join(',')}</div>
  ),
}));

const T = 1_762_732_800_000;
const RANGE = { from: T, to: T + 600_000 };

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

/** GET /api/videos 요청 기록(요청이 들어온 순서대로 쌓인다). */
let videoRequests: string[] = [];
beforeEach(() => {
  videoRequests = [];
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
