import {
  SpatialViewportContext,
  type SpatialViewportValue,
} from '@/features/layout/SpatialViewportHost';
import { WatchPanel } from '@/features/watch/WatchPanel';
import { useRealtimeStore } from '@/store/realtime.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const calls = vi.hoisted(() => ({
  list: [] as { path: string; method?: string; body?: unknown }[],
  role: 'ADMIN',
  bindings: [] as unknown[],
}));

vi.mock('@/features/auth/useCurrentRole', () => ({ useCurrentRole: () => calls.role }));
vi.mock('@/lib/api', () => ({
  api: (path: string, options: { method?: string; body?: unknown } = {}) => {
    calls.list.push({ path, method: options.method, body: options.body });
    if (options.method === 'PUT') {
      return Promise.resolve({
        binding_id: 1,
        device_id: 'watch-01',
        world_id: 'w-17',
        method: 'MANUAL',
        bound_at: 0,
        channel_state: 'BOUND',
      });
    }
    return Promise.resolve(calls.bindings);
  },
}));

function viewport(selectedKey: string | null): SpatialViewportValue {
  return {
    view: '3d',
    viewerReady: true,
    selectedKey,
    selectedZoneId: null,
    zones: [],
    zonesRaw: undefined,
    zonesFailed: false,
    selectTarget: () => undefined,
    clearSelection: () => {},
  } as SpatialViewportValue;
}

function renderPanel(selectedKey: string | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SpatialViewportContext value={viewport(selectedKey)}>
        <WatchPanel />
      </SpatialViewportContext>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  calls.list.length = 0;
  calls.role = 'ADMIN';
  calls.bindings = [];
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useRealtimeStore.setState({
    health: {
      payload: {
        watches: {
          registered: 1,
          connected: 1,
          items: [
            {
              device_id: 'watch-01',
              link: 'CONNECTED',
              last_heartbeat_at: 0,
              battery_pct: 80,
              transport: 'WIFI',
            },
          ],
        },
      } as never,
      receivedAt: 0,
      clockOffsetMs: 0,
    },
  });
});
afterEach(cleanup);

describe('워치 패널(-342)', () => {
  it('추적 인원 대 활성 워치 수와 워치 목록을 보이고, 고른 사람에게 워치를 잇는다', async () => {
    renderPanel('person:w-17');
    expect(screen.getByText(/활성 워치 1대/)).toBeTruthy();
    expect(screen.getByRole('list', { name: '워치 목록' }).textContent).toContain('watch-01 연결');
    expect(screen.getByText(/선택한 사람/).textContent).toContain('연결된 워치 없음');

    fireEvent.click(screen.getByRole('button', { name: '이 사람에게 잇기' }));
    await waitFor(() => expect(calls.list.some((c) => c.method === 'PUT')).toBe(true));
    const put = calls.list.find((c) => c.method === 'PUT');
    expect(put?.path).toBe('/api/watch-bindings/watch-01');
    expect(put?.body).toEqual({ world_id: 'w-17' });
  });

  it('열람 계정은 상태만 보고 잇기·풀기 버튼이 없다', () => {
    calls.role = 'VIEWER';
    renderPanel('person:w-17');
    expect(screen.queryByRole('button', { name: '이 사람에게 잇기' })).toBeNull();
    expect(screen.getByText(/선택한 사람/).textContent).toContain('연결된 워치 없음');
  });

  it('이미 워치가 이어진 사람을 고르면 그 워치가 기본이고 잇기를 막는다', async () => {
    calls.bindings = [
      {
        binding_id: 1,
        device_id: 'watch-01',
        world_id: 'w-17',
        method: 'MANUAL',
        bound_at: 0,
        channel_state: 'BOUND',
      },
    ];
    renderPanel('person:w-17');
    await waitFor(() => expect(screen.getByText(/선택한 사람/).textContent).toContain('연결됨'));
    const button = screen.getByRole('button', { name: '이 사람에게 잇기' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('combobox', { name: '이을 워치' }) as HTMLSelectElement).value).toBe(
      'watch-01',
    );
  });

  it('다른 사람에게 이어진 워치를 고르면 옮긴다고 알린다', async () => {
    calls.bindings = [
      {
        binding_id: 1,
        device_id: 'watch-01',
        world_id: 'w-3',
        method: 'MANUAL',
        bound_at: 0,
        channel_state: 'BOUND',
      },
    ];
    renderPanel('person:w-17');
    await waitFor(() =>
      expect(screen.getByText(/에 이어져 있습니다/).textContent).toContain('w-3'),
    );
  });

  it('사람을 고르지 않으면 고르라고 안내한다', () => {
    renderPanel(null);
    expect(screen.getByText('3D 에서 사람을 고르면 워치를 이을 수 있습니다')).toBeTruthy();
  });
});
