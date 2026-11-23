import { GPU_STATUS_KEY } from '@/features/gpu/api';
import { GpuGateDialog } from '@/features/gpu/GpuGateDialog';
import { GpuPanel } from '@/features/gpu/GpuPanel';
import { problem } from '@/mocks/problem';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// GPU 패널의 공급자 탭(S15P31A101-841): 처음은 AWS 탭, AWS 준비 안 됨이면 까닭과 RunPod 탭으로 가는 단추,
// 켜진 GPU 가 있으면 그 공급자 탭, 켜기에 provider 를 싣는다. 게이트 창은 공급자 없이 서버 기본을 따른다.

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const G7E = 'g7e.4xlarge';
const G7E_NAME = `${G7E} · NVIDIA RTX PRO 6000 ×1`;
const H100 = 'NVIDIA H100 80GB HBM3';

function offersOf(provider: 'AWS' | 'RUNPOD') {
  const gpu = provider === 'AWS' ? G7E : H100;
  const region = provider === 'AWS' ? 'ap-northeast-2a' : 'AP-JP-1';
  return {
    provider,
    cloud_type: 'SECURE',
    interruptible: false,
    max_price_per_hour_usd: null,
    gpu_types: [
      {
        gpu_type: gpu,
        name: provider === 'AWS' ? G7E_NAME : 'H100 SXM',
        memory_gb: provider === 'AWS' ? 96 : 80,
        preferred: true,
        recommended: provider === 'RUNPOD',
        tier: 'RECOMMENDED',
        available: provider === 'AWS' ? null : true,
      },
    ],
    regions: [{ region, name: region, preferred: true }],
    cells: [
      {
        gpu_type: gpu,
        region,
        stock: provider === 'AWS' ? 'UNKNOWN' : 'HIGH',
        available: provider === 'AWS' ? null : true,
        price_per_hour_usd: provider === 'AWS' ? 4.9155 : 2.99,
        within_cap: true,
      },
    ],
    default: { gpu_type: gpu, region },
    fetched_at_ms: Date.now(),
  };
}

let awsReady = false;
let current: Record<string, unknown> = {};
const offerUrls: string[] = [];
const launchBodies: unknown[] = [];

const server = setupServer(
  http.get('*/api/gpu/sessions/current', () => HttpResponse.json(current)),
  http.get('*/api/gpu/offers', ({ request }) => {
    offerUrls.push(new URL(request.url).search);
    const provider = new URL(request.url).searchParams.get('provider');
    if (provider === 'AWS' && !awsReady) {
      return problem(503, 'GPU_PROVIDER_NOT_CONFIGURED', 'GPU 공급자 설정이 없습니다');
    }
    return HttpResponse.json(offersOf(provider === 'RUNPOD' ? 'RUNPOD' : 'AWS'));
  }),
  http.post('*/api/gpu/launches', async ({ request }) => {
    launchBodies.push(await request.json());
    return HttpResponse.json(
      { ...launchOf('AWS'), state: 'REQUESTED', instance_id: null },
      { status: 201 },
    );
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u-1', role: 'ADMIN' } });
  current = { session: null, server_time_ms: Date.now(), launch: null, power: 'OFF' };
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  awsReady = false;
  offerUrls.length = 0;
  launchBodies.length = 0;
  useSessionStore.getState().signOut();
});

function launchOf(provider: 'AWS' | 'RUNPOD') {
  return {
    launch_id: 'l-1',
    state: 'RUNNING',
    provider,
    gpu_type: provider === 'AWS' ? G7E : H100,
    region: provider === 'AWS' ? 'ap-northeast-2a' : 'AP-JP-1',
    cloud_type: 'SECURE',
    interruptible: false,
    price_per_hour_usd: 3,
    requested_by: 'admin1',
    requested_at_ms: Date.now() - 60_000,
    instance_id: provider === 'AWS' ? 'i-0123456789abcdef0' : 'pod-1',
    ended_at_ms: null,
    end_reason: null,
    failure_code: null,
  };
}

/** seedStatus: 상단바 GPU 점이 이미 받아 둔 상태(앱에서는 패널을 열 때 늘 있다) */
function renderWith(node: ReactNode, seedStatus = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (seedStatus) client.setQueryData(GPU_STATUS_KEY, { status: current, offsetMs: 0 });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const tab = (name: string) => screen.getByRole('tab', { name });

describe('GpuPanel 공급자 탭', () => {
  it('처음은 AWS 탭이고, AWS 가 준비 안 됐으면 까닭과 할 일을 보이며 「RunPod 탭으로」를 누르면 RunPod 재고 표로 간다', async () => {
    renderWith(<GpuPanel onClose={() => {}} />);
    expect(tab('AWS').getAttribute('aria-selected')).toBe('true');
    expect(tab('RunPod').getAttribute('aria-selected')).toBe('false');
    const notReady = await screen.findByTestId('gpu-provider-not-ready');
    expect(notReady.textContent).toContain('AWS GPU 는 아직 준비 중입니다');
    expect(notReady.textContent).toContain('지금은 RunPod 탭에서 켜세요');
    expect(screen.queryByRole('button', { name: '켜기' })).toBeNull();
    act(() => fireEvent.click(screen.getByRole('button', { name: 'RunPod 탭으로' })));
    expect(tab('RunPod').getAttribute('aria-selected')).toBe('true');
    await screen.findByRole('button', { name: 'H100 SXM AP-JP-1 켤 수 있음' });
    expect(offerUrls).toEqual(['?provider=AWS', '?provider=RUNPOD']);
  });

  it('탭은 좌우 화살표로 옮기면 바로 고르고, 고른 탭만 Tab 키로 닿는다', async () => {
    renderWith(<GpuPanel onClose={() => {}} />);
    expect(tab('AWS').tabIndex).toBe(0);
    expect(tab('RunPod').tabIndex).toBe(-1);
    act(() => fireEvent.keyDown(tab('AWS'), { key: 'ArrowRight' }));
    expect(tab('RunPod').getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(tab('RunPod'));
    expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe(
      'gpu-provider-tab-RUNPOD',
    );
    act(() => fireEvent.keyDown(tab('RunPod'), { key: 'ArrowRight' }));
    expect(tab('AWS').getAttribute('aria-selected')).toBe('true');
  });

  it('AWS 가 준비됐으면 AWS 탭의 재고 표로 켜고 켜기 요청에 provider 를 싣는다', async () => {
    awsReady = true;
    renderWith(<GpuPanel onClose={() => {}} />);
    const cell = await screen.findByRole('button', { name: `${G7E_NAME} ap-northeast-2a 모름` });
    expect(cell.getAttribute('aria-pressed')).toBe('true');
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(launchBodies).toEqual([{ provider: 'AWS' }]));
    // 칸을 직접 고르면 종류·영역도 함께
    cleanup();
    renderWith(<GpuPanel onClose={() => {}} />);
    act(() => fireEvent.click(tab('RunPod')));
    const h100 = await screen.findByRole('button', { name: 'H100 SXM AP-JP-1 켤 수 있음' });
    // 기본 칸이라도 직접 누르면 고른 칸으로 보낸다
    act(() => fireEvent.click(h100));
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(launchBodies).toHaveLength(2));
    expect(launchBodies[1]).toEqual({ provider: 'RUNPOD', gpu_type: H100, region: 'AP-JP-1' });
  });

  it('켜져 있는 GPU 가 있으면 그 공급자 탭으로 열고 끄기를 보이며, 다른 탭에는 한 번에 하나라는 안내를 보인다', async () => {
    current = {
      session: null,
      server_time_ms: Date.now(),
      launch: launchOf('RUNPOD'),
      power: 'STARTING',
      stop_block: null,
    };
    renderWith(<GpuPanel onClose={() => {}} />, true);
    expect(tab('RunPod').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('button', { name: 'GPU 끄기' })).toBeTruthy();
    act(() => fireEvent.click(tab('AWS')));
    const notice = screen.getByTestId('gpu-other-provider-on');
    expect(notice.textContent).toContain(
      '지금 RunPod GPU 가 켜져 있어 다른 공급자로는 켤 수 없습니다',
    );
    expect(screen.queryByRole('button', { name: 'GPU 끄기' })).toBeNull();
    act(() => fireEvent.click(screen.getByRole('button', { name: 'RunPod 탭 보기' })));
    expect(screen.getByRole('button', { name: 'GPU 끄기' })).toBeTruthy();
    expect(offerUrls).toEqual([]);
  });

  it('게이트 창의 켜기 표는 공급자 없이 서버 기본 공급자를 따른다', async () => {
    awsReady = true;
    renderWith(<GpuGateDialog onCancel={() => {}} />);
    act(() => fireEvent.click(screen.getByRole('button', { name: 'GPU 켜기' })));
    await screen.findByRole('button', { name: `${G7E_NAME} ap-northeast-2a 모름` });
    expect(offerUrls).toEqual(['']);
    expect(screen.queryByRole('tab')).toBeNull();
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    act(() => fireEvent.click(screen.getByRole('button', { name: '켜기' })));
    await waitFor(() => expect(launchBodies).toEqual([{}]));
  });
});
