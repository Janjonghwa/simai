import { createViewerStore } from '@/features/viewer3d/viewer3d.store';
import type { WatchBinding } from '@/features/watch/api';
import { useWatchBindDetector } from '@/features/watch/useWatch';
import { HIGHLIGHT_MS, useWatchBindEffectStore } from '@/features/watch/watchBindEffect.store';
import { bindKeyOf, newBinds } from '@/features/watch/watchStatus';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const calls = vi.hoisted(() => ({ bindings: [] as unknown[] }));
vi.mock('@/lib/api', () => ({ api: () => Promise.resolve(calls.bindings) }));

const bind = (device: string, world: string, method = 'MOTION_MATCH', state = 'BOUND') =>
  ({
    binding_id: 1,
    device_id: device,
    world_id: world,
    method,
    bound_at: 0,
    channel_state: state,
  }) as WatchBinding;

describe('newBinds', () => {
  it('처음 불러옴은 없고, 이전에 없던 (워치, World ID) 만 센다', () => {
    expect(newBinds(null, [bind('watch-01', 'w-1')])).toEqual([]);
    const prev = new Set([bindKeyOf(bind('watch-01', 'w-1'))]);
    expect(newBinds(prev, [bind('watch-01', 'w-1')])).toEqual([]);
    expect(newBinds(prev, [bind('watch-01', 'w-2', 'MANUAL')])).toEqual([
      { deviceId: 'watch-01', worldId: 'w-2', method: 'MANUAL' },
    ]);
  });
  it('모르는 방식은 자동, 끊긴 배정은 세지 않는다', () => {
    const prev = new Set<string>();
    expect(newBinds(prev, [bind('a', 'w-1', 'HAND_WAVE')])[0]?.method).toBe('AUTO');
    expect(newBinds(prev, [bind('a', 'w-1', 'MANUAL', 'LOST')])).toEqual([]);
  });
});

function Probe() {
  useWatchBindDetector();
  return null;
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <Probe />
      </QueryClientProvider>,
    ),
  };
}

describe('새 배정 효과(-509)', () => {
  beforeEach(() => {
    calls.bindings = [bind('watch-01', 'w-1')];
    useWatchBindEffectStore.getState().clear();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('처음 불러온 배정과 재로드는 효과가 없고, 새로 이어진 배정만 강조·알림이 뜬다', async () => {
    const { client } = mount();
    await waitFor(() => expect(client.getQueryData(['watch-bindings'])).toBeTruthy());
    expect(useWatchBindEffectStore.getState().highlights).toEqual({});

    // 같은 배정 유지(다시 받아도 효과 없음)
    await act(() => client.invalidateQueries({ queryKey: ['watch-bindings'] }));
    expect(useWatchBindEffectStore.getState().notice).toBeNull();

    // 새 배정
    calls.bindings = [bind('watch-01', 'w-1'), bind('watch-02', 'w-17', 'MANUAL')];
    await act(() => client.invalidateQueries({ queryKey: ['watch-bindings'] }));
    await waitFor(() =>
      expect(useWatchBindEffectStore.getState().highlights).toEqual({
        'w-17': '워치 watch-02 연결됨',
      }),
    );
    expect(useWatchBindEffectStore.getState().notice?.text).toBe('watch-02 → w-17 연결됨(수동)');
  });

  it('효과 시간이 지나면 강조가 사라진다', () => {
    vi.useFakeTimers();
    useWatchBindEffectStore
      .getState()
      .announce({ deviceId: 'watch-01', worldId: 'w-1', method: 'AUTO' });
    expect(useWatchBindEffectStore.getState().highlights['w-1']).toBe('워치 watch-01 연결됨');
    vi.advanceTimersByTime(HIGHLIGHT_MS + 1);
    expect(useWatchBindEffectStore.getState().highlights).toEqual({});
    vi.advanceTimersByTime(5_000);
    expect(useWatchBindEffectStore.getState().notice).toBeNull();
  });

  it('같은 사람에게 다시 이어지면 앞선 타이머가 새 강조를 일찍 지우지 않는다', () => {
    vi.useFakeTimers();
    const s = useWatchBindEffectStore.getState();
    s.announce({ deviceId: 'watch-01', worldId: 'w-1', method: 'AUTO' });
    vi.advanceTimersByTime(3_000);
    s.announce({ deviceId: 'watch-01', worldId: 'w-1', method: 'AUTO' });
    vi.advanceTimersByTime(2_000);
    expect(useWatchBindEffectStore.getState().highlights['w-1']).toBeDefined();
    vi.advanceTimersByTime(2_100);
    expect(useWatchBindEffectStore.getState().highlights).toEqual({});
  });
});

describe('뷰어 personHighlights 입력', () => {
  it('내용이 같으면 Store 를 바꾸지 않고, 비우면(편집 모드 undefined) 지운다', () => {
    const store = createViewerStore('LIVE');
    store.getState().setPersonHighlights({ 'w-1': '워치 watch-01 연결됨' });
    const first = store.getState().personHighlights;
    store.getState().setPersonHighlights({ 'w-1': '워치 watch-01 연결됨' });
    expect(store.getState().personHighlights).toBe(first);
    store.getState().setPersonHighlights(undefined);
    expect(store.getState().personHighlights).toEqual({});
  });
});
