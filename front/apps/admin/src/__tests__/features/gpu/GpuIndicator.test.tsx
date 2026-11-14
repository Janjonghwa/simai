import { GpuIndicator } from '@/features/gpu/GpuIndicator';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const T = 1_760_342_400_000;
const H100 = 'NVIDIA H100 80GB HBM3';
const PCIE = 'NVIDIA H100 PCIe';

let current: Record<string, unknown> | null = null;
const posts: string[] = [];
const launchBodies: unknown[] = [];

const offers = {
  provider: 'RUNPOD',
  cloud_type: 'SECURE',
  interruptible: false,
  max_price_per_hour_usd: 3,
  gpu_types: [
    { gpu_type: H100, name: 'H100 SXM', memory_gb: 80 },
    { gpu_type: PCIE, name: 'H100 PCIe', memory_gb: 80 },
  ],
  regions: [
    { region: 'AP-JP-1', name: 'Asia Japan 1', preferred: true },
    { region: 'EU-RO-1', name: 'Europe Romania 1', preferred: true },
  ],
  cells: [
    {
      gpu_type: H100,
      region: 'AP-JP-1',
      stock: 'NONE',
      price_per_hour_usd: 2.99,
      within_cap: true,
    },
    {
      gpu_type: H100,
      region: 'EU-RO-1',
      stock: 'HIGH',
      price_per_hour_usd: 2.99,
      within_cap: true,
    },
    { gpu_type: PCIE, region: 'AP-JP-1', stock: 'LOW', price_per_hour_usd: 2.39, within_cap: true },
    {
      gpu_type: PCIE,
      region: 'EU-RO-1',
      stock: 'NONE',
      price_per_hour_usd: 2.39,
      within_cap: true,
    },
  ],
  default: { gpu_type: H100, region: 'EU-RO-1' },
  fetched_at_ms: T,
};

const server = setupServer(
  http.get('*/api/gpu/sessions/current', () => HttpResponse.json(current)),
  http.get('*/api/gpu/offers', () => HttpResponse.json(offers)),
  http.post('*/api/gpu/launches', async ({ request }) => {
    posts.push('launch');
    launchBodies.push(await request.json());
    return HttpResponse.json(launchView(), { status: 201 });
  }),
  http.post('*/api/gpu/stop', () => {
    posts.push('stop');
    return new HttpResponse(null, { status: 204 });
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
afterEach(() => {
  cleanup();
  server.resetHandlers();
  posts.length = 0;
  launchBodies.length = 0;
  useSessionStore.getState().signOut();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

function launchView() {
  return {
    launch_id: 'l-1',
    state: 'RUNNING',
    provider: 'RUNPOD',
    gpu_type: H100,
    region: 'EU-RO-1',
    cloud_type: 'SECURE',
    interruptible: false,
    price_per_hour_usd: 3,
    requested_by: 'admin1',
    requested_at_ms: Date.now() - 30 * 60_000,
    instance_id: 'pod-1',
    ended_at_ms: null,
    end_reason: null,
    failure_code: null,
  };
}

function sessionView(stage: string, link: 'CONNECTED' | 'DISCONNECTED' = 'DISCONNECTED') {
  return {
    session_id: 's-1',
    state: 'ACTIVE',
    stage,
    stage_reason_code: stage === 'FAILED' ? 'ENGINE_BUILD_FAILED' : null,
    provider: 'RUNPOD',
    instance_id: 'pod-1',
    gpu_type: H100,
    region: 'EU-RO-1',
    price_per_hour_usd: 3,
    registered_at_ms: Date.now() - 25 * 60_000,
    elapsed_ms: 25 * 60_000,
    tier0_link: link,
  };
}

function statusOf(power: string, extra: Record<string, unknown> = {}) {
  return {
    session: null,
    server_time_ms: Date.now(),
    launch: null,
    power,
    stop_block: null,
    ...extra,
  };
}

function signInAs(role: Role) {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u-1', role } });
}

function renderIndicator() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GpuIndicator />
    </QueryClientProvider>,
  );
}

const indicator = () => screen.getByTestId('gpu-indicator');

describe('GpuIndicator', () => {
  it.each([
    ['OFF', {}, 'OFF', 'GPU 꺼짐'],
    ['STARTING', { launch: launchView() }, 'PREPARING', 'GPU 준비 중'],
    [
      'PREPARING',
      { launch: launchView(), session: sessionView('ENGINE') },
      'PREPARING',
      'GPU 준비 중',
    ],
    [
      'CONNECTED',
      { launch: launchView(), session: sessionView('READY', 'CONNECTED') },
      'CONNECTED',
      'GPU 연결',
    ],
    ['ERROR', { launch: launchView(), session: sessionView('FAILED') }, 'ERROR', 'GPU 오류'],
  ])('서버 %s 는 점 %s 와 짧은 글자만 보인다', async (power, extra, dot, label) => {
    current = statusOf(power, extra);
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe(dot));
    expect(indicator().textContent).toBe(label);
  });

  it('마우스를 올리면 종류·지역, 경과·예상 비용, 판정 지연, 준비 단계를 보인다', async () => {
    current = statusOf('PREPARING', { launch: launchView(), session: sessionView('ENGINE') });
    signInAs('ADMIN');
    const now = Date.now();
    useRealtimeStore.setState({
      phase: 'OPEN',
      openedAt: now,
      health: { payload: viewerHealthAt(T).payload, receivedAt: now, clockOffsetMs: 0 },
      lagSamples: [{ at: T, lagMs: 180, cameras: {} }],
    });
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('PREPARING'));
    act(() => fireEvent.mouseEnter(indicator()));
    const tip = screen.getByRole('tooltip');
    expect(tip.textContent).toContain(`${H100} · EU-RO-1`);
    expect(tip.textContent).toContain('경과 30분 · 약 $1.50');
    expect(tip.textContent).toContain('판정 지연 180 ms');
    const steps = screen.getByRole('list', { name: '준비 단계' });
    expect(steps.textContent).toBe('켜는 중 → 가중치 → 엔진 → 준비 완료');
    expect(screen.getByText('엔진').closest('li')?.getAttribute('aria-current')).toBe('step');
    act(() => fireEvent.mouseLeave(indicator()));
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('누르면 켜기 패널: 기본은 서버가 고른 칸이고 다른 GPU 를 고르면 엔진 캐시 안내 뒤 확인을 거쳐 켠다', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    act(() => fireEvent.click(indicator()));
    const def = await screen.findByRole('button', { name: 'H100 SXM EU-RO-1 많음' });
    expect(def.getAttribute('aria-pressed')).toBe('true');
    expect(
      (screen.getByRole('button', { name: 'H100 SXM AP-JP-1 없음' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.queryByText(/엔진 캐시가 GPU 종류별/)).toBeNull();

    act(() => fireEvent.click(screen.getByRole('button', { name: 'H100 PCIe AP-JP-1 적음' })));
    expect(
      screen.getByText(/엔진 캐시가 GPU 종류별이라 첫 실행은 엔진 빌드로 몇 분 더 걸립니다/),
    ).toBeTruthy();
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    const confirm = screen.getByTestId('gpu-launch-confirm');
    expect(confirm.textContent).toContain('H100 PCIe · AP-JP-1 · 시간당 $2.39');
    expect(posts).toEqual([]);
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(posts).toEqual(['launch']));
    expect(launchBodies).toEqual([{ gpu_type: PCIE, region: 'AP-JP-1' }]);
  });

  it('기본 칸 그대로 켜면 서버가 고르게 빈 본문을 보낸다', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    act(() => fireEvent.click(indicator()));
    await screen.findByRole('button', { name: 'H100 SXM EU-RO-1 많음' });
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(launchBodies).toEqual([{}]));
  });

  it('관제 중이면 끄기를 막고 까닭을 보이며, 아니면 확인 뒤 끈다', async () => {
    current = statusOf('CONNECTED', {
      launch: launchView(),
      session: sessionView('READY', 'CONNECTED'),
      stop_block: 'LIVE',
    });
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('CONNECTED'));
    act(() => fireEvent.click(indicator()));
    expect(screen.getByText('관제(작업) 중에는 끌 수 없습니다')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'GPU 끄기' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    cleanup();

    current = statusOf('CONNECTED', {
      launch: launchView(),
      session: sessionView('READY', 'CONNECTED'),
    });
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('CONNECTED'));
    act(() => fireEvent.click(indicator()));
    act(() => fireEvent.click(screen.getByRole('button', { name: 'GPU 끄기' })));
    expect(screen.getByTestId('gpu-stop-confirm')).toBeTruthy();
    expect(posts).toEqual([]);
    act(() => fireEvent.click(screen.getByRole('button', { name: '끄기' })));
    await waitFor(() => expect(posts).toEqual(['stop']));
  });

  it('관리자가 아니면 상태를 묻지 않고 Tier 0 연결로만 점을 그리며 누를 수 없다', () => {
    signInAs('VIEWER');
    renderIndicator();
    expect(indicator().dataset.dot).toBe('OFF');
    expect((indicator() as HTMLButtonElement).disabled).toBe(true);
  });
});
