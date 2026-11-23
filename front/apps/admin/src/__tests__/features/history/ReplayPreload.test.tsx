import { ReplayStage } from '@/features/history/ReplayStage';
import type { WorkReplay } from '@/features/history/useWorkReplay';
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

const T = 1_762_732_800_000;
const range = { from: T, to: T + 60_000 };
const loading: WorkReplay = {
  range,
  stepMs: 1000,
  holdMs: 1000,
  loading: true,
  failed: false,
  error: null,
  replay: null,
  index: null,
};
let requests: string[] = [];
const server = setupServer(
  http.get('http://api.test/api/videos', () =>
    HttpResponse.json({
      segments: [
        {
          segment_id: 'first',
          camera_id: 'cam_A',
          segment_start_capture_ms: T,
          segment_end_capture_ms: T + 60_000,
        },
      ],
    }),
  ),
  http.get('http://api.test/api/videos/:id', ({ params }) => {
    requests.push(String(params.id));
    return HttpResponse.json({
      segment_id: params.id,
      url: 'https://s3.test/first.mp4',
      expires_at_ms: T + 300_000,
      content_type: 'video/mp4',
      container: 'mp4',
    });
  }),
);
// jsdom 은 load() 가 없다(칸에서 빠진 영상을 비울 때 부른다, S15P31A101-859)
vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  requests = [];
  useSessionStore.setState({ session: { accessToken: 'token', user: { id: 'a', role: 'ADMIN' } } });
});
afterEach(() => {
  cleanup();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

describe('기록을 받는 동안 영상 준비', () => {
  it('기록이 준비되기 전에 주소를 받고 같은 영상 요소를 그대로 사용한다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (session: WorkReplay) => (
      <QueryClientProvider client={client}>
        <ReplayStage session={session} range={range} />
      </QueryClientProvider>
    );
    const { container, rerender } = render(view(loading));
    expect(screen.getByText('기록을 불러오는 중입니다…')).toBeTruthy();
    await waitFor(() => expect(requests).toEqual(['first']));
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    const video = container.querySelector('video');
    expect(video?.closest('[hidden]')).not.toBeNull();
    const status = {
      playhead: T,
      start: T,
      end: range.to,
      playing: false,
      ended: false,
      speed: 1,
      worldT: null,
      recordAgeMs: null,
      gap: false,
    };
    rerender(
      view({
        ...loading,
        loading: false,
        replay: { getStatus: () => status, subscribeStatus: () => () => undefined } as never,
        index: {
          frames: [{ t: T, ids: [], tracks: [] }],
          workers: [],
          events: [],
          gaps: [],
          pending: null,
        } as never,
      }),
    );
    expect(container.querySelector('video')).toBe(video);
    expect(video?.closest('[hidden]')).toBeNull();
    expect(requests).toEqual(['first']);
  });

  it('기록 실패 시 기존 실패 안내를 보이고 준비한 영상을 제거한다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (session: WorkReplay) => (
      <QueryClientProvider client={client}>
        <ReplayStage session={session} range={range} />
      </QueryClientProvider>
    );
    const { container, rerender } = render(view(loading));
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    const error = new Error('실패');
    rerender(view({ ...loading, loading: false, failed: true, error }));
    expect(screen.getByText('기록을 불러오지 못했습니다.')).toBeTruthy();
    expect(container.querySelector('video')).toBeNull();
  });
});
