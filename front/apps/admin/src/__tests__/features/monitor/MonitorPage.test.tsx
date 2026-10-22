import { MonitorPage } from '@/features/monitor/MonitorPage';
import type { Viewer3DHandle, Viewer3DProps, ViewerStatus } from '@/features/viewer3d';
import { worldStateAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen } from '@testing-library/react';
import { useEffect, useImperativeHandle, useRef, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ZONES_RAW = { contract: 'DEMO_UNCONFIRMED', zones: [] };

// 목록·구역은 이 시험의 관심 밖이다(앱 api 인스턴스는 import.meta.env 를 읽는다).
vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: ZONES_RAW, failed: false }),
}));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

// 3D 뷰어 대역: 마운트마다 상태를 한 번 알린다(실제 뷰어처럼 콜백이 바뀌어도 다시 알리지 않는다).
const fake = vi.hoisted(() => ({
  phase: 'ready' as 'initializing' | 'ready' | 'error',
  select: vi.fn(),
}));

function FakeViewer({ ref, onStatusChange }: Viewer3DProps) {
  useImperativeHandle(
    ref,
    () =>
      ({
        select: fake.select,
        focus: () => ({ ok: true }),
        clearSelection: () => ({ ok: true }),
        setMode: () => ({ ok: true }),
      }) as unknown as Viewer3DHandle,
  );
  const reported = useRef(false);
  useEffect(() => {
    if (reported.current) return;
    reported.current = true;
    onStatusChange?.({
      phase: fake.phase,
      mode: 'LIVE',
      error: fake.phase === 'error' ? { code: 'RENDER_FAILED', message: 'x' } : null,
    } as ViewerStatus);
  });
  return <div data-testid="viewer3d" />;
}

const loadFake = () => Promise.resolve({ default: FakeViewer });

function renderPage(loadViewer = loadFake) {
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<MonitorPage loadViewer={loadViewer} />, { wrapper });
}

const plan = () => screen.queryByRole('group', { name: '2D 평면' });
const toggle = (name: '3D' | '2D') => screen.getByRole('button', { name });

describe('MonitorPage 3D·2D 전환', () => {
  beforeEach(() => {
    useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
    fake.phase = 'ready';
    fake.select.mockReset().mockImplementation(() => ({ ok: true }));
  });
  afterEach(cleanup);

  it('3D 가 첫 화면을 그리기 전에는 2D 평면을 보이고, 준비되면 3D 만 보인다', async () => {
    fake.phase = 'initializing';
    renderPage();
    expect(await screen.findByTestId('viewer3d')).toBeTruthy();
    expect(plan()).toBeTruthy();
    expect(toggle('3D').getAttribute('aria-pressed')).toBe('true');
    cleanup();

    fake.phase = 'ready';
    renderPage();
    expect(await screen.findByTestId('viewer3d')).toBeTruthy();
    await vi.waitFor(() => expect(plan()).toBeNull());
  });

  it('토글로 2D 로 바꾸면 3D 뷰어를 그리지 않고, 다시 3D 로 돌아오면 준비된 뒤 2D 가 사라진다', async () => {
    renderPage();
    await screen.findByTestId('viewer3d');

    act(() => toggle('2D').click());
    expect(plan()).toBeTruthy();
    expect(screen.queryByTestId('viewer3d')).toBeNull();

    act(() => toggle('3D').click());
    expect(await screen.findByTestId('viewer3d')).toBeTruthy();
    await vi.waitFor(() => expect(plan()).toBeNull());
  });

  it('이미 3D 일 때 3D 를 다시 눌러도 2D 가 덮지 않는다', async () => {
    renderPage();
    await screen.findByTestId('viewer3d');
    await vi.waitFor(() => expect(plan()).toBeNull());

    act(() => toggle('3D').click());
    expect(plan()).toBeNull();
    expect(screen.getByTestId('viewer3d')).toBeTruthy();
  });

  it('3D 가 렌더에 실패하면(RENDER_FAILED) 2D 로 바꾼다', async () => {
    fake.phase = 'error';
    renderPage();
    await vi.waitFor(() => expect(toggle('2D').getAttribute('aria-pressed')).toBe('true'));
    expect(plan()).toBeTruthy();
    expect(screen.queryByTestId('viewer3d')).toBeNull();
  });

  it('3D 코드를 받지 못하면 2D 로 바꾸고, 3D 로 다시 바꾸면 새로 불러온다', async () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      let calls = 0;
      const flaky = () =>
        ++calls === 1 ? Promise.reject(new Error('chunk load failed')) : loadFake();
      renderPage(flaky);
      await vi.waitFor(() => expect(toggle('2D').getAttribute('aria-pressed')).toBe('true'));
      expect(screen.queryByTestId('viewer3d')).toBeNull();

      act(() => toggle('3D').click());
      expect(await screen.findByTestId('viewer3d')).toBeTruthy();
      expect(calls).toBe(2);
    } finally {
      quiet.mockRestore();
    }
  });

  it('3D 가 준비되면 구역을 넘기고, 2D 에서 고른 사람이 장면에 올라오면 3D 에서도 고른다', async () => {
    // 준비 직후에는 사람이 아직 장면에 없다(NOT_FOUND). 올라올 때까지 다시 시도해야 한다.
    let calls = 0;
    fake.select.mockImplementation(() =>
      ++calls < 3 ? { ok: false, reason: 'NOT_FOUND' } : { ok: true },
    );
    const pushZones = vi.fn();
    useRealtimeStore.setState({
      source: { pushZones } as unknown as ReturnType<typeof useRealtimeStore.getState>['source'],
      world: { payload: worldStateAt(Date.now()).payload, receivedAt: Date.now() },
    });
    renderPage();
    await screen.findByTestId('viewer3d');
    expect(pushZones).toHaveBeenLastCalledWith({ ok: true, raw: ZONES_RAW });

    act(() => toggle('2D').click());
    const person = screen.getByRole('button', { name: /^w-0012/ });
    act(() => person.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(person.getAttribute('aria-pressed')).toBe('true');

    act(() => toggle('3D').click());
    await screen.findByTestId('viewer3d');
    await vi.waitFor(() => expect(calls).toBe(3));
    expect(fake.select).toHaveBeenLastCalledWith({ type: 'person', id: 'w-0012' });
    // 고르고 나면 더 시도하지 않는다.
    await new Promise((r) => setTimeout(r, 500));
    expect(calls).toBe(3);
  });
});
