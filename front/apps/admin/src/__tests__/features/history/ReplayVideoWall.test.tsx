import { ReplayVideoWall } from '@/features/history/ReplayVideoWall';
import { VIDEO_URL_CACHE_MS, type SegmentBlobCache } from '@/features/history/segmentPrefetch';
import type { VideoSegment } from '@/features/history/types';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
// jsdom 은 load() 가 없다(칸에서 빠진 영상을 비울 때 부른다)
const load = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
afterEach(() => {
  cleanup();
  urlRequests = [];
  load.mockClear();
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

// 다른 시각으로 넘어가기(S15P31A101-859): 서명 주소 4분 보관, 미리 받은 blob 재생, 넘어가는 동안 「불러오는 중」, 첫 화면 알림.

const next: VideoSegment = {
  segment_id: 'next',
  camera_id: 'cam_A',
  segment_start_capture_ms: T + 60_000,
  segment_end_capture_ms: T + 90_000,
};

/** 미리 받은 조각 보관소 흉내(get 만 쓴다). */
const blobsOf = (held: Record<string, string>) =>
  ({
    prefetch: () => {},
    get: (id: string) => held[id],
    subscribe: () => () => {},
    dispose: () => {},
  }) satisfies SegmentBlobCache;

function wallWith(client: QueryClient, props: Partial<Parameters<typeof ReplayVideoWall>[0]>) {
  return (
    <QueryClientProvider client={client}>
      <ReplayVideoWall segments={[]} upcoming={[]} replay={null} {...props} />
    </QueryClientProvider>
  );
}

describe('ReplayVideoWall 다른 시각으로 넘어가기', () => {
  it('서명 주소는 4분 보관해 이미 본 조각으로 돌아와도 다시 받지 않는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(wallWith(client, { segments: [cover] }));
    await waitFor(() => expect(document.querySelector('video')).not.toBeNull());
    view.rerender(wallWith(client, { segments: [next] }));
    await waitFor(() => expect(urlRequests).toEqual(['cover', 'next']));
    view.rerender(wallWith(client, { segments: [cover] }));
    await waitFor(() =>
      expect(document.querySelector('video')?.getAttribute('src')).toBe(
        'https://s3.test/cover.mp4',
      ),
    );
    expect(urlRequests).toEqual(['cover', 'next']);
    expect(
      client.getQueryCache().find({ queryKey: ['history', 'video-url', 'cover'] })?.gcTime,
    ).toBe(VIDEO_URL_CACHE_MS);
  });

  it('보관한 주소가 4분보다 오래됐으면 그 주소로 시작하지 않고 새로 받는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(
      ['history', 'video-url', 'cover'],
      {
        segment_id: 'cover',
        url: 'https://s3.test/old.mp4',
        expires_at_ms: T,
        content_type: 'video/mp4',
        container: 'mp4',
      },
      { updatedAt: Date.now() - VIDEO_URL_CACHE_MS - 1_000 },
    );
    render(wallWith(client, { segments: [cover] }));
    await waitFor(() =>
      expect(document.querySelector('video')?.getAttribute('src')).toBe(
        'https://s3.test/cover.mp4',
      ),
    );
    expect(urlRequests).toEqual(['cover']);
  });

  it('미리 받은 조각은 서명 주소를 받지 않고 blob 주소로 튼다', async () => {
    renderWall({ segments: [cover], blobs: blobsOf({ cover: 'blob:cover' }) });
    const video = await waitFor(() => {
      const found = document.querySelector('video');
      expect(found).not.toBeNull();
      return found!;
    });
    expect(video.getAttribute('src')).toBe('blob:cover');
    expect(urlRequests).toEqual([]);
  });

  it('blob 을 읽지 못하면 서명 주소로 다시 튼다', async () => {
    renderWall({ segments: [cover], blobs: blobsOf({ cover: 'blob:gone' }) });
    const video = await waitFor(() => {
      const found = document.querySelector('video');
      expect(found).not.toBeNull();
      return found!;
    });
    fireEvent.error(video);
    await waitFor(() =>
      expect(document.querySelector('video')?.getAttribute('src')).toBe(
        'https://s3.test/cover.mp4',
      ),
    );
    expect(urlRequests).toEqual(['cover']);
  });

  it('넘어가는 동안 앞 화면을 그대로 두고 「불러오는 중」을 띄우며, 새 조각이 첫 화면을 내면 걷는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(wallWith(client, { segments: [cover] }));
    const first = await waitFor(() => {
      const found = document.querySelector('video[src="https://s3.test/cover.mp4"]');
      expect(found).not.toBeNull();
      return found!;
    });
    fireEvent.loadedData(first);
    expect(screen.queryByText('불러오는 중')).toBeNull();

    view.rerender(wallWith(client, { segments: [next] }));
    const second = await waitFor(() => {
      const found = document.querySelector('video[src="https://s3.test/next.mp4"]');
      expect(found).not.toBeNull();
      return found!;
    });
    // 앞 조각이 보이는 채로(빈 칸·검은 화면 없음) 작게 알린다
    expect(first.classList.contains('opacity-0')).toBe(false);
    expect(screen.getByText('불러오는 중')).toBeTruthy();
    expect(screen.queryByText('영상을 불러오는 중입니다')).toBeNull();

    fireEvent.loadedData(second);
    await waitFor(() => expect(screen.queryByText('불러오는 중')).toBeNull());
    expect(second.classList.contains('opacity-0')).toBe(false);
  });

  it('같은 조각 안에서 옮기는 동안(seeking)에도 「불러오는 중」을 띄운다', async () => {
    renderWall({ segments: [cover] });
    const video = await waitFor(() => {
      const found = document.querySelector('video');
      expect(found).not.toBeNull();
      return found!;
    });
    fireEvent.loadedData(video);
    fireEvent.seeking(video);
    expect(screen.getByText('불러오는 중')).toBeTruthy();
    fireEvent.seeked(video);
    expect(screen.queryByText('불러오는 중')).toBeNull();
  });

  it('영상 칸이 모두 첫 화면을 내면 한 번 알린다(미리 받기 시작 신호)', async () => {
    const onFirstFrames = vi.fn();
    const other: VideoSegment = { ...cover, segment_id: 'other', camera_id: 'cam_B' };
    renderWall({ segments: [cover, other], onFirstFrames });
    const videos = await waitFor(() => {
      const found = document.querySelectorAll('video');
      expect(found).toHaveLength(2);
      return found;
    });
    fireEvent.loadedData(videos[0]!);
    expect(onFirstFrames).not.toHaveBeenCalled();
    fireEvent.loadedData(videos[1]!);
    await waitFor(() => expect(onFirstFrames).toHaveBeenCalledTimes(1));
  });

  it('칸에서 빠진 영상은 주소를 비워 받기·디코더를 바로 놓는다', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(wallWith(client, { segments: [cover] }));
    const first = await waitFor(() => {
      const found = document.querySelector('video[src="https://s3.test/cover.mp4"]');
      expect(found).not.toBeNull();
      return found!;
    });
    view.rerender(wallWith(client, { segments: [next] }));
    // 앞 조각이 첫 화면을 내지 않았으면 붙잡아 둘 것이 없어 바로 빠진다
    await waitFor(() => expect(first.isConnected).toBe(false));
    expect(first.hasAttribute('src')).toBe(false);
    expect(load).toHaveBeenCalled();
  });
});
