import { ReplayVideoWall } from '@/features/history/ReplayVideoWall';
import type { VideoSegment } from '@/features/history/types';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

// 다시 보기 영상 칸(S15P31A101-801): 올라온 조각은 영상을 받는 동안 「영상을 불러오는 중입니다」, 아직 안 올라온 카메라는 「업로드 중」.
// jsdom 의 <video> 는 영상을 읽지 않아 첫 화면(loadeddata)이 오지 않으므로 받는 중 글이 그대로 남는다.

const T = 1_762_732_800_000;
/** 재생 주소 요청(조각 id). */
let urlRequests: string[] = [];

const server = setupServer(
  http.get('http://api.test/api/videos/:segmentId', ({ params }) => {
    urlRequests.push(String(params.segmentId));
    return HttpResponse.json({
      segment_id: params.segmentId,
      url: `https://s3.test/${String(params.segmentId)}.mp4`,
      expires_at_ms: T + 300_000,
      content_type: 'video/mp4',
      container: 'mp4',
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
afterEach(() => {
  cleanup();
  urlRequests = [];
});

const cover: VideoSegment = {
  segment_id: 'cover',
  camera_id: 'cam_A',
  segment_start_capture_ms: T,
  segment_end_capture_ms: T + 30_000,
};

function renderWall(props: Partial<Parameters<typeof ReplayVideoWall>[0]>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReplayVideoWall segments={[]} upcoming={[]} replay={null} {...props} />
    </QueryClientProvider>,
  );
}

const tileOf = (name: RegExp) => screen.getByRole('button', { name });

describe('ReplayVideoWall 업로드 중 칸', () => {
  it('재생 시각의 조각이 아직 안 올라온 카메라는 진행과 함께 「업로드 중」을 보이고, 올라온 조각은 받는 중 글을 보인다', async () => {
    renderWall({
      segments: [cover],
      waiting: ['cam_B'],
      upload: { uploaded: 12, expected: 28 },
    });

    // 카메라 이름순: cam_A(조각 있음) → 카메라 1, cam_B(업로드 중) → 카메라 2
    const uploading = tileOf(/카메라 2 크게 보기/);
    expect(within(uploading).getByText('업로드 중 12/28')).toBeTruthy();
    expect(within(uploading).queryByText('영상을 불러오는 중입니다')).toBeNull();
    expect(uploading.querySelector('video')).toBeNull();

    const loading = tileOf(/카메라 1 크게 보기/);
    expect(within(loading).getByText('영상을 불러오는 중입니다')).toBeTruthy();
    expect(within(loading).queryByText(/업로드 중/)).toBeNull();
    // 올라온 조각만 재생 주소를 받는다(업로드 중 칸은 요청하지 않는다)
    await waitFor(() => expect(loading.querySelector('video')).not.toBeNull());
    expect(urlRequests).toEqual(['cover']);
  });

  it('예정 조각 수를 모르면 진행 없이 「업로드 중」만 보인다', () => {
    renderWall({ waiting: ['cam_A'], upload: { uploaded: 0, expected: null } });

    expect(within(tileOf(/카메라 1 크게 보기/)).getByText('업로드 중')).toBeTruthy();
    expect(urlRequests).toEqual([]);
  });

  it('업로드 중 칸이 없으면 올라온 조각 칸만 둔다', () => {
    renderWall({ segments: [cover] });

    expect(screen.getAllByRole('button', { name: /크게 보기/ })).toHaveLength(1);
    expect(screen.queryByText(/업로드 중/)).toBeNull();
    expect(screen.getByText('영상을 불러오는 중입니다')).toBeTruthy();
  });
});
