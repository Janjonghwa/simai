import { EventClips } from '@/features/clips/EventClips';
import type { Clip } from '@/features/clips/types';
import { clipsKey } from '@/features/clips/useEventClips';
import { EventDetail } from '@/features/events/EventDetail';
import { formatTime } from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import { type Role, useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const T = 1_760_342_400_000;

const clip = (change: Partial<Clip> = {}): Clip => ({
  clip_id: 'c-1',
  camera_id: 'cam1',
  resume_count: 0,
  clip_start_capture_ms: T - 20_000,
  clip_end_capture_ms: T + 20_000,
  truncated_start: false,
  truncated_end: false,
  has_gaps: false,
  retention_hold: false,
  deleted_at_ms: null,
  ...change,
});

const event = (change: Partial<EventItem> = {}): EventItem => ({
  event_id: 'e-1',
  rule_id: 'R1',
  source: 'LIVE',
  lifecycle: 'RAISED',
  verification: 'VERIFIED',
  severity: 'HIGH',
  confidence: 'HIGH',
  needs_check: false,
  world_ids: ['w-0012'],
  zone_id: 'z-02',
  metrics: {},
  rationale: null,
  proposed_severity: null,
  raised_at: T,
  updated_at: T,
  ...change,
});

let clips: Clip[] = [];
let listCalls = 0;
let urlCalls = 0;
let puts: unknown[] = [];
let urlExpiresIn = 60_000;
/** 정해 두면 서버가 그때까지 답하지 않는다(답하기 전 화면을 본다). */
let urlGate: Promise<void> | undefined;
let putGate: Promise<void> | undefined;

/** 붙잡아 둔 답을 풀어 주는 함수와 함께 문을 만든다. */
function gate() {
  let open = () => {};
  const closed = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { closed, open };
}

const server = setupServer(
  http.get('http://api.test/api/events/:id/clips', ({ params }) => {
    listCalls += 1;
    return HttpResponse.json({ contract: 'DEMO_UNCONFIRMED', event_id: params.id, clips });
  }),
  http.get('http://api.test/api/clips/:id', async ({ params }) => {
    const n = ++urlCalls;
    await urlGate;
    return HttpResponse.json({
      contract: 'DEMO_UNCONFIRMED',
      clip_id: params.id,
      url: `/clips/${String(params.id)}-${n}.mp4`,
      expires_at_ms: Date.now() + urlExpiresIn,
    });
  }),
  http.put('http://api.test/api/clips/:id/retention', async ({ params, request }) => {
    const body = (await request.json()) as { retention_hold: boolean };
    puts.push(body);
    await putGate;
    clips = clips.map((c) =>
      c.clip_id === params.id ? { ...c, retention_hold: body.retention_hold } : c,
    );
    return HttpResponse.json(clips.find((c) => c.clip_id === params.id));
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

beforeEach(() => {
  clips = [
    clip(),
    clip({ clip_id: 'c-2', camera_id: 'cam2', truncated_start: true }),
    clip({ clip_id: 'c-3', camera_id: 'cam3', has_gaps: true }),
  ];
  listCalls = 0;
  urlCalls = 0;
  puts = [];
  urlExpiresIn = 60_000;
  urlGate = undefined;
  putGate = undefined;
  signIn('ADMIN');
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

function signIn(role: Role) {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role } });
}

function renderClips(item = event()) {
  const queryClient = new QueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <EventClips event={item} />
    </QueryClientProvider>,
  );
  return queryClient;
}

const playButton = (camera: string) => screen.getByRole('button', { name: `${camera} 재생` });
const video = (camera: string) => screen.getByLabelText(`${camera} 클립`) as HTMLVideoElement;

describe('EventClips', () => {
  it('관리자에게 카메라별 클립과 잘림·빈칸을 보이고, 재생을 누르기 전에는 주소를 받지 않는다', async () => {
    renderClips();

    const section = await screen.findByRole('region', { name: '클립' });
    expect(await within(section).findByText('cam1')).toBeTruthy();
    expect(within(section).getByText('cam2')).toBeTruthy();
    expect(within(section).getByText('앞 잘림')).toBeTruthy();
    expect(within(section).getByText('프레임 빠짐')).toBeTruthy();
    // 촬영 구간은 시작–끝 순서다.
    const range = `${formatTime(T - 20_000)}–${formatTime(T + 20_000)}`;
    expect(within(section).getAllByText(range)).toHaveLength(3);
    // 영상 주소를 받을 때 EC2 가 조회 기록을 남긴다. 상세를 열기만 해서는 받지 않는다.
    expect(urlCalls).toBe(0);
    expect(screen.queryByLabelText('cam1 클립')).toBeNull();
  });

  it('재생을 누르면 주소를 받아 영상을 보이고, 주소가 살아 있으면 다시 눌러도 받지 않고 처음부터 다시 튼다', async () => {
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4'));
    expect(urlCalls).toBe(1);
    // 브라우저 기본 메뉴의 내려받기를 감추고 우클릭 메뉴를 막는다.
    expect(video('cam1').getAttribute('controlslist')).toBe('nodownload');
    expect(fireEvent.contextMenu(video('cam1'))).toBe(false);

    const first = video('cam1');
    fireEvent.click(playButton('cam1'));
    // 요청을 보냈다면 이 사이에 도착한다(만료된 주소는 아래 시험에서 같은 시간 안에 새로 받는다).
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(urlCalls).toBe(1);
    expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4');
    // 영상을 새로 띄워 처음부터 다시 튼다.
    expect(video('cam1')).not.toBe(first);
  });

  it('주소를 받는 동안에는 재생을 다시 눌러도 주소를 또 받지 않는다', async () => {
    const answer = gate();
    urlGate = answer.closed;
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(urlCalls).toBe(1));
    fireEvent.click(playButton('cam1'));
    answer.open();

    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4'));
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(urlCalls).toBe(1);
  });

  it('주소가 만료됐으면 다시 누를 때 새 주소를 받는다', async () => {
    urlExpiresIn = 1_000;
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(urlCalls).toBe(1));
    fireEvent.click(playButton('cam1'));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-2.mp4'));
  });

  it('재생이 실패하면 한 번만 새 주소를 받고, 또 실패하면 알린다', async () => {
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4'));
    fireEvent.error(video('cam1'));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-2.mp4'));
    fireEvent.error(video('cam1'));

    expect(await screen.findByText('재생하지 못했습니다')).toBeTruthy();
    expect(screen.queryByLabelText('cam1 클립')).toBeNull();
    expect(urlCalls).toBe(2);
  });

  it('주소를 받은 지 오래된 뒤 재생이 실패하면(되감기 중 만료) 다시 받는다', async () => {
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4'));
    fireEvent.error(video('cam1'));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-2.mp4'));

    const realNow = Date.now.bind(Date);
    const later = vi.spyOn(Date, 'now').mockImplementation(() => realNow() + 31_000);
    try {
      fireEvent.error(video('cam1'));
      await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-3.mp4'));
      expect(screen.queryByText('재생하지 못했습니다')).toBeNull();
    } finally {
      later.mockRestore();
    }
  });

  it('재생 주소를 받지 못하면 알린다', async () => {
    server.use(
      http.get('http://api.test/api/clips/:id', () => HttpResponse.json({}, { status: 500 })),
    );
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    expect((await screen.findByRole('alert')).textContent).toBe('재생 주소를 받지 못했습니다');
  });

  it('지운 클립은 삭제됨이고 재생·보존 버튼이 없다', async () => {
    clips = [clip({ deleted_at_ms: T })];
    renderClips();

    expect(await screen.findByText('삭제됨')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'cam1 재생' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'cam1 보존' })).toBeNull();
  });

  const holdButton = () => screen.getByRole('button', { name: 'cam1 보존' });

  it('HIGH 이벤트 클립은 관리자가 보존 표시를 붙이고 떼며, 서버 값으로 다시 그린다', async () => {
    renderClips();

    await screen.findByRole('button', { name: 'cam1 보존' });
    expect(holdButton().getAttribute('aria-pressed')).toBe('false');
    const before = listCalls;
    fireEvent.click(holdButton());
    await waitFor(() => expect(holdButton().getAttribute('aria-pressed')).toBe('true'));
    expect(listCalls).toBeGreaterThan(before);

    fireEvent.click(holdButton());
    await waitFor(() => expect(holdButton().getAttribute('aria-pressed')).toBe('false'));
    expect(puts).toEqual([{ retention_hold: true }, { retention_hold: false }]);
  });

  it('보존 표시는 서버가 답하기 전에는 바꿔 그리지 않는다(낙관적 갱신 없음)', async () => {
    const answer = gate();
    putGate = answer.closed;
    renderClips();

    fireEvent.click(await screen.findByRole('button', { name: 'cam1 보존' }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(holdButton().getAttribute('aria-pressed')).toBe('false');

    answer.open();
    await waitFor(() => expect(holdButton().getAttribute('aria-pressed')).toBe('true'));
  });

  it('CRITICAL 이벤트 클립은 자동 보존이라 보존 표시만 있고 바꾸는 버튼이 없다', async () => {
    clips = [clip({ retention_hold: true })];
    renderClips(event({ severity: 'CRITICAL' }));

    const section = await screen.findByRole('region', { name: '클립' });
    expect(await within(section).findByText('보존')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'cam1 보존' })).toBeNull();
  });

  it('재개 구간이 있으면 발생·재개로 묶는다', async () => {
    clips = [clip(), clip({ clip_id: 'c-9', resume_count: 1 })];
    renderClips();

    expect(await screen.findByText('발생')).toBeTruthy();
    expect(screen.getByText('재개 1')).toBeTruthy();
  });

  it('클립이 아직 없으면 비우고, 목록을 받지 못하면 알린다', async () => {
    clips = [];
    renderClips();
    const section = await screen.findByRole('region', { name: '클립' });
    await waitFor(() => expect(listCalls).toBe(1));
    expect(within(section).getByText('—')).toBeTruthy();
    cleanup();

    server.use(
      http.get('http://api.test/api/events/:id/clips', () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    renderClips();
    expect(await screen.findByText('클립 목록을 받지 못했습니다')).toBeTruthy();
  });

  it('목록을 다시 받지 못해도 받아 둔 클립과 보던 영상은 그대로 두고 실패를 알린다', async () => {
    const queryClient = renderClips();
    fireEvent.click(await screen.findByRole('button', { name: 'cam1 재생' }));
    await waitFor(() => expect(video('cam1').getAttribute('src')).toBe('/clips/c-1-1.mp4'));
    const playing = video('cam1');

    server.use(
      http.get('http://api.test/api/events/:id/clips', () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    await act(() => queryClient.refetchQueries({ queryKey: clipsKey('e-1') }));

    expect(await screen.findByText('클립 목록을 받지 못했습니다')).toBeTruthy();
    expect(video('cam1')).toBe(playing);
    expect(screen.getByText('cam3')).toBeTruthy();
  });

  it('상세를 보는 동안 다시 받아 늦게 올라온 클립을 보인다', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      clips = [];
      renderClips();
      await waitFor(() => expect(listCalls).toBe(1));
      expect(screen.queryByText('cam1')).toBeNull();

      clips = [clip()];
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(await screen.findByText('cam1')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['목록이 404(주소 없음)이면 다시 받지 않는다', () => HttpResponse.json({}, { status: 404 }), 1],
    ['목록 형식이 다르면 다시 받지 않는다', () => HttpResponse.json({ clips: [] }), 1],
    ['목록이 500(서버 오류)이면 계속 다시 받는다', () => HttpResponse.json({}, { status: 500 }), 3],
  ] as const)('%s', async (_, reply, calls) => {
    let n = 0;
    server.use(
      http.get('http://api.test/api/events/:id/clips', () => {
        n += 1;
        return reply();
      }),
    );
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderClips();
      expect(await screen.findByText('클립 목록을 받지 못했습니다')).toBeTruthy();
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(n).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['FOREMAN', event()],
    ['VIEWER', event()],
    ['ADMIN', event({ severity: 'CAUTION' })],
    ['ADMIN', event({ source: 'PLANNED' })],
  ] as const)('%s 이거나 클립이 없는 이벤트면 그리지 않고 목록도 부르지 않는다', (role, item) => {
    act(() => signIn(role));
    const queryClient = renderClips(item);

    expect(screen.queryByRole('region', { name: '클립' })).toBeNull();
    // 꺼 둔 조회는 시간이 지나도 요청하지 않는다. 기다리지 않고 꺼져 있는지를 본다.
    expect(
      queryClient
        .getQueryCache()
        .find({ queryKey: clipsKey(item.event_id) })
        ?.isDisabled(),
    ).toBe(true);
    expect(listCalls).toBe(0);
  });

  it('이벤트 상세 아래에 클립 칸이 들어간다', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EventDetail entry={{ event: event(), stageStartedAt: T }} onBack={() => {}} />
      </QueryClientProvider>,
    );
    const detail = screen.getByRole('region', { name: '이벤트 상세' });
    expect(await within(detail).findByRole('button', { name: 'cam1 재생' })).toBeTruthy();
  });
});
