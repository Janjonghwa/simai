import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { HEALTH_TIMEOUT_MS, isLinkAlive, useRealtimeStore } from '@/store/realtime.store';
import { beforeEach, describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;
const health = (receivedAt: number) => ({
  payload: viewerHealthAt(receivedAt).payload,
  receivedAt,
  clockOffsetMs: 0,
});

describe('realtime.store', () => {
  beforeEach(() => useRealtimeStore.setState(useRealtimeStore.getInitialState(), true));

  it('begin 은 이전 세션 값을 모두 비우고 연결 중으로 둔다', () => {
    const store = useRealtimeStore.getState();
    store.opened(T);
    store.receiveHealth(health(T));
    store.reject();

    useRealtimeStore.getState().begin();
    expect(useRealtimeStore.getState()).toMatchObject({
      phase: 'CONNECTING',
      openedAt: null,
      health: null,
      rejected: 0,
      source: null,
    });
  });

  it('reset 하면 로그인 전 상태로 돌아간다', () => {
    useRealtimeStore.getState().opened(T);
    useRealtimeStore.getState().reset();
    expect(useRealtimeStore.getState().phase).toBe('IDLE');
  });
});

describe('isLinkAlive', () => {
  it('열려 있고 마지막 health(없으면 연결 시각)로부터 정한 시간 안이면 이어져 있다', () => {
    const opened = { phase: 'OPEN' as const, openedAt: T, health: null };
    expect(isLinkAlive(opened, T + HEALTH_TIMEOUT_MS)).toBe(true);
    expect(isLinkAlive(opened, T + HEALTH_TIMEOUT_MS + 1)).toBe(false);

    const withHealth = { ...opened, health: health(T + 10_000) };
    expect(isLinkAlive(withHealth, T + 12_000)).toBe(true);
    expect(isLinkAlive(withHealth, T + 13_001)).toBe(false);
  });

  it('열려 있지 않으면 끊긴 것이다', () => {
    for (const phase of ['IDLE', 'CONNECTING', 'CLOSED'] as const) {
      expect(isLinkAlive({ phase, openedAt: T, health: health(T) }, T)).toBe(false);
    }
  });
});
