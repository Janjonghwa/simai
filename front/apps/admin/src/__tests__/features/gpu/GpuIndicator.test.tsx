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
const RTX4090 = 'NVIDIA GeForce RTX 4090';
const A4000 = 'NVIDIA RTX A4000';

let current: Record<string, unknown> | null = null;
const posts: string[] = [];
const launchBodies: unknown[] = [];
let presences = 0;

const offers = {
  provider: 'RUNPOD',
  cloud_type: 'SECURE',
  interruptible: false,
  max_price_per_hour_usd: 3,
  // S15P31A101-705: 카탈로그의 모든 GPU(서버가 추천 → VRAM 등급 순으로 정렬해 준다)
  gpu_types: [
    {
      gpu_type: H100,
      name: 'H100 SXM',
      memory_gb: 80,
      preferred: true,
      recommended: true,
      tier: 'RECOMMENDED',
      available: true,
    },
    {
      gpu_type: PCIE,
      name: 'H100 PCIe',
      memory_gb: 80,
      preferred: true,
      recommended: true,
      tier: 'RECOMMENDED',
      available: true,
    },
    {
      gpu_type: RTX4090,
      name: 'RTX 4090',
      memory_gb: 24,
      preferred: false,
      recommended: false,
      tier: 'LIMITED',
      available: true,
    },
    {
      gpu_type: A4000,
      name: 'RTX A4000',
      memory_gb: 16,
      preferred: false,
      recommended: false,
      tier: 'RISKY',
      available: null,
    },
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
    {
      gpu_type: RTX4090,
      region: 'AP-JP-1',
      stock: 'HIGH',
      available: true,
      price_per_hour_usd: 0.69,
      within_cap: true,
    },
    {
      gpu_type: RTX4090,
      region: 'EU-RO-1',
      stock: 'NONE',
      available: false,
      price_per_hour_usd: 0.69,
      within_cap: true,
    },
    {
      gpu_type: A4000,
      region: 'AP-JP-1',
      stock: 'UNKNOWN',
      available: null,
      price_per_hour_usd: 0.25,
      within_cap: true,
    },
    {
      gpu_type: A4000,
      region: 'EU-RO-1',
      stock: 'NONE',
      available: false,
      price_per_hour_usd: 0.25,
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
  http.post('*/api/gpu/presence', () => {
    presences += 1;
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
  presences = 0;
  vi.restoreAllMocks();
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
  ])(
    '서버 %s 는 점 %s 와 「GPU」 글자만 보이고 상태는 화면 읽기 이름·정보창 첫 줄에 있다(-705)',
    async (power, extra, dot, label) => {
      current = statusOf(power, extra);
      signInAs('ADMIN');
      renderIndicator();
      await waitFor(() => expect(indicator().dataset.dot).toBe(dot));
      expect(indicator().textContent).toBe('GPU');
      expect(indicator().getAttribute('aria-label')).toBe(label);
      act(() => fireEvent.mouseEnter(indicator()));
      expect(screen.getByRole('tooltip').firstElementChild?.textContent).toBe(label);
    },
  );

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

  it('누르면 켜기 패널: 모든 GPU 행에 켤 수 있음·없음·모름, 기본은 서버가 고른 칸이고 다른 GPU 는 엔진 캐시 안내 뒤 확인을 거쳐 켠다', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    act(() => fireEvent.click(indicator()));
    const def = await screen.findByRole('button', { name: 'H100 SXM EU-RO-1 켤 수 있음' });
    expect(def.getAttribute('aria-pressed')).toBe('true');
    // 행 순서는 서버가 준 대로(추천이 위), 추천·등급 표시, 등급 글(많음·적음)은 없다
    const rows = screen.getAllByRole('rowheader').map((h) => h.textContent);
    expect(rows).toEqual([
      'H100 SXM80GB추천',
      'H100 PCIe80GB추천',
      'RTX 409024GB제한',
      'RTX A400016GB위험',
    ]);
    expect(screen.queryByText(/많음|보통|적음/)).toBeNull();
    // 모름은 고를 수 있고, 없음은 못 고른다
    expect(
      (screen.getByRole('button', { name: 'RTX A4000 AP-JP-1 모름' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(
      (screen.getByRole('button', { name: 'RTX 4090 EU-RO-1 없음' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: 'H100 SXM AP-JP-1 없음' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.queryByText(/엔진 캐시가 GPU 종류별/)).toBeNull();

    act(() =>
      fireEvent.click(screen.getByRole('button', { name: 'H100 PCIe AP-JP-1 켤 수 있음' })),
    );
    expect(
      screen.getByText(/엔진 캐시가 GPU 종류별이라 첫 실행은 엔진 빌드로 몇 분 더 걸립니다/),
    ).toBeTruthy();
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    const confirm = screen.getByTestId('gpu-launch-confirm');
    expect(confirm.textContent).toContain('H100 PCIe (80GB) · AP-JP-1 · 시간당 $2.39');
    expect(posts).toEqual([]);
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(posts).toEqual(['launch']));
    expect(launchBodies).toEqual([{ gpu_type: PCIE, region: 'AP-JP-1' }]);
  });

  it.each([
    ['RTX 4090 AP-JP-1 켤 수 있음', RTX4090, /세팅\(맵 굽기\)·VLM 근거·아바타가 안 됩니다/],
    ['RTX A4000 AP-JP-1 모름', A4000, /관제 준비가 실패할 수 있습니다/],
  ])(
    'VRAM 제한·위험 GPU 는 막지 않고 「안 됩니다」 경고 확인을 거쳐 켠다(-705): %s',
    async (name, gpu, text) => {
      current = statusOf('OFF');
      signInAs('ADMIN');
      renderIndicator();
      await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
      act(() => fireEvent.click(indicator()));
      const cell = await screen.findByRole('button', { name });
      act(() => fireEvent.click(cell));
      act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
      const warning = screen.getByTestId('gpu-tier-warning');
      expect(warning.textContent).toMatch(text);
      expect(warning.getAttribute('role')).toBe('alert');
      expect(posts).toEqual([]);
      act(() => fireEvent.click(screen.getByRole('button', { name: '그래도 켜기' })));
      await waitFor(() => expect(launchBodies).toEqual([{ gpu_type: gpu, region: 'AP-JP-1' }]));
    },
  );

  it('추천 GPU 를 켤 때는 등급 경고가 없다', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    act(() => fireEvent.click(indicator()));
    await screen.findByRole('button', { name: 'H100 SXM EU-RO-1 켤 수 있음' });
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    expect(screen.queryByTestId('gpu-tier-warning')).toBeNull();
    expect(screen.getByTestId('gpu-launch-confirm').textContent).toContain('H100 SXM (80GB)');
  });

  it('기본 칸 그대로 켜면 서버가 고르게 빈 본문을 보낸다', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    act(() => fireEvent.click(indicator()));
    await screen.findByRole('button', { name: 'H100 SXM EU-RO-1 켤 수 있음' });
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

  it('GPU 가 켜져 있으면 화면 입력 뒤 1분에 한 번만 조작 신호를 보내고 화면이 안 보이면 보내지 않는다(-708)', async () => {
    current = statusOf('CONNECTED', {
      launch: launchView(),
      session: sessionView('READY', 'CONNECTED'),
    });
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('CONNECTED'));
    const t0 = Date.now();
    const now = vi.spyOn(Date, 'now').mockReturnValue(t0);
    fireEvent.pointerDown(document.body);
    fireEvent.keyDown(document.body, { key: 'a' });
    await waitFor(() => expect(presences).toBe(1));
    now.mockReturnValue(t0 + 59_000);
    fireEvent.keyDown(document.body, { key: 'a' });
    now.mockReturnValue(t0 + 61_000);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent.pointerDown(document.body);
    visibility.mockReturnValue('visible');
    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(presences).toBe(2));
  });

  it('GPU 가 꺼져 있으면 조작 신호를 보내지 않는다(-708)', async () => {
    current = statusOf('OFF');
    signInAs('ADMIN');
    renderIndicator();
    await waitFor(() => expect(indicator().dataset.dot).toBe('OFF'));
    fireEvent.pointerDown(document.body);
    fireEvent.keyDown(document.body, { key: 'a' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(presences).toBe(0);
  });

  it('관리자가 아니면 상태를 묻지 않고 Tier 0 연결로만 점을 그리며 누를 수 없다(정보창에 상태는 보인다)', () => {
    signInAs('VIEWER');
    renderIndicator();
    expect(indicator().dataset.dot).toBe('OFF');
    expect((indicator() as HTMLButtonElement).disabled).toBe(true);
    expect(indicator().getAttribute('aria-label')).toBe('GPU 꺼짐');
    act(() => fireEvent.mouseEnter(indicator()));
    expect(screen.getByRole('tooltip').textContent).toBe('GPU 꺼짐');
  });
});
