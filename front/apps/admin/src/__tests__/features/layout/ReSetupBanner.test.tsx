import { ReSetupBanner } from '@/features/layout/ReSetupBanner';
import { reSetupCameras, reSetupTitle } from '@/features/layout/status';
import { useHealthSnapshot } from '@/features/layout/useHealthSnapshot';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { HEALTH_TIMEOUT_MS, useRealtimeStore } from '@/store/realtime.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { http } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 현장 세팅 다시 하기 배너(S15P31A101-480). EC2 가 viewer_health.re_setup 으로 내려준 카메라를 보이고, 비면 사라진다.
// GET /api/health 는 MSW 로 대신 답한다(실제 서버 주소가 아닌 예약 도메인 .invalid).

const navigate = vi.fn();
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigate }));
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.invalid' }) };
});

const T = 1_760_342_400_000;
const CAM_C = { camera_id: 'cam_C', reason: '매칭 부족', detected_at: T - 5_000 };
const CAM_A = { camera_id: 'cam_A', reason: '위치 이동 의심', detected_at: T - 4_000 };

let reply = () => HttpResponse.json(viewerHealthAt(T));
const server = setupServer(http.get('http://api.invalid/api/health', () => reply()));
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  reply = () => HttpResponse.json(viewerHealthAt(T));
});
afterEach(() => {
  cleanup();
  navigate.mockClear();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

const banner = () => screen.queryByRole('group', { name: '현장 세팅 다시 하기' });

/** 연결이 열린 화면: GET /api/health 로 지금 상태를 채우고 배너를 그린다. */
function Harness() {
  useHealthSnapshot();
  return <ReSetupBanner />;
}

function open() {
  useRealtimeStore.setState({ phase: 'OPEN', openedAt: Date.now() });
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

/** /topic/health 로 받은 것처럼 새 상태를 넣는다. */
function receive(reSetup?: (typeof CAM_C)[]) {
  act(() =>
    useRealtimeStore.getState().receiveHealth({
      payload: viewerHealthAt(T + 1_000, { reSetup }).payload,
      receivedAt: Date.now(),
      clockOffsetMs: 0,
    }),
  );
}

describe('ReSetupBanner', () => {
  it('GET /api/health 가 재세팅이 필요한 카메라를 내려주면 안내 문구와 사유, 작업 관리로 버튼을 보인다', async () => {
    reply = () => HttpResponse.json(viewerHealthAt(T, { reSetup: [CAM_C] }));
    open();

    await waitFor(() => expect(banner()).not.toBeNull());
    const box = banner()!;
    expect(within(box).getByRole('status').textContent).toBe(
      '카메라 cam_C 위치가 바뀌었습니다 — 현장 세팅을 다시 하세요',
    );
    expect(box.textContent).toContain('cam_C · 매칭 부족');

    fireEvent.click(within(box).getByRole('button', { name: '작업 관리로' }));
    expect(navigate).toHaveBeenCalledExactlyOnceWith({ to: '/work' });
  });

  it('해소돼 EC2 가 빈 배열을 알리면 사라진다', async () => {
    reply = () => HttpResponse.json(viewerHealthAt(T, { reSetup: [CAM_C] }));
    open();
    await waitFor(() => expect(banner()).not.toBeNull());

    receive([]);
    expect(banner()).toBeNull();

    // 다시 감지되면 다시 뜬다
    receive([CAM_C]);
    expect(banner()).not.toBeNull();
  });

  it('카메라가 여럿이면 ID 를 한 줄에 모으고 사유는 카메라마다 보인다', async () => {
    reply = () => HttpResponse.json(viewerHealthAt(T, { reSetup: [CAM_A, CAM_C] }));
    open();

    await waitFor(() => expect(banner()).not.toBeNull());
    expect(within(banner()!).getByRole('status').textContent).toBe(
      '카메라 cam_A, cam_C 위치가 바뀌었습니다 — 현장 세팅을 다시 하세요',
    );
    expect(
      within(banner()!)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual(['cam_A · 위치 이동 의심', 'cam_C · 매칭 부족']);
  });

  it('EC2 가 모르면(필드 없음) 재세팅 없음으로 읽지 않아 배너가 없다', async () => {
    open();

    await waitFor(() => expect(useRealtimeStore.getState().health).not.toBeNull());
    expect(banner()).toBeNull();
  });

  it('배너는 경보 등급색·지연 색을 쓰지 않는다', async () => {
    reply = () => HttpResponse.json(viewerHealthAt(T, { reSetup: [CAM_C] }));
    open();

    await waitFor(() => expect(banner()).not.toBeNull());
    expect(banner()!.outerHTML).not.toMatch(/sev-|text-delay|bg-delay|bg-stripes/);
  });
});

describe('ReSetupBanner 연결', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T);
  });
  afterEach(() => vi.useRealTimers());

  it('EC2 와 끊기면(지금 상태를 모른다) 마지막으로 받은 재세팅 안내를 보이지 않고, 다시 오면 돌아온다', () => {
    const seed = () =>
      useRealtimeStore.setState({
        phase: 'OPEN',
        openedAt: Date.now(),
        health: {
          payload: viewerHealthAt(T, { reSetup: [CAM_C] }).payload,
          receivedAt: Date.now(),
          clockOffsetMs: 0,
        },
      });
    seed();
    render(<ReSetupBanner />);
    expect(banner()).not.toBeNull();

    act(() => vi.advanceTimersByTime(HEALTH_TIMEOUT_MS + 600));
    expect(banner()).toBeNull();

    act(() => seed());
    expect(banner()).not.toBeNull();
  });
});

describe('재세팅 값 읽기', () => {
  const snapshot = (reSetup?: (typeof CAM_C)[]) => ({
    payload: viewerHealthAt(T, { reSetup }).payload,
    receivedAt: T,
    clockOffsetMs: 0,
  });

  it('연결이 살아 있고 EC2 가 알린 값만 돌려준다', () => {
    expect(reSetupCameras(snapshot([CAM_C]), true)).toEqual([CAM_C]);
    expect(reSetupCameras(snapshot([]), true)).toEqual([]);
    expect(reSetupCameras(snapshot(undefined), true)).toEqual([]);
    expect(reSetupCameras(snapshot([CAM_C]), false)).toEqual([]);
    expect(reSetupCameras(null, true)).toEqual([]);
  });

  it('없을 때는 늘 같은 빈 배열을 돌려줘 구독이 다시 그려지지 않게 한다', () => {
    expect(reSetupCameras(null, true)).toBe(reSetupCameras(snapshot(undefined), true));
  });

  it('문구는 카메라 ID 를 쉼표로 잇는다', () => {
    expect(reSetupTitle([CAM_C])).toBe(
      '카메라 cam_C 위치가 바뀌었습니다 — 현장 세팅을 다시 하세요',
    );
    expect(reSetupTitle([CAM_A, CAM_C])).toContain('cam_A, cam_C');
  });
});
