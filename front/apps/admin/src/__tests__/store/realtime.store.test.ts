import { viewerHealthAt, worldStateAt } from '@/mocks/realtimeScenario';
import {
  HEALTH_TIMEOUT_MS,
  isLinkAlive,
  isWorldFresh,
  useRealtimeStore,
  WORLD_STALE_MS,
} from '@/store/realtime.store';
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
    useRealtimeStore.getState().receiveWorld({ payload: worldStateAt(T).payload, receivedAt: T });
    useRealtimeStore.getState().reset();
    expect(useRealtimeStore.getState()).toMatchObject({ phase: 'IDLE', world: null });
  });

  it('연결이 다시 열린 뒤 첫 world 는 순서와 상관없이 받고, 그다음부터 다시 새 것만 받는다', () => {
    const store = useRealtimeStore.getState();
    store.receiveWorld({ payload: worldStateAt(T).payload, receivedAt: T });
    store.opened(T + 1);
    store.receiveWorld({ payload: worldStateAt(T - 5000).payload, receivedAt: T + 2 });
    expect(useRealtimeStore.getState().world?.receivedAt).toBe(T + 2);
    store.receiveWorld({ payload: worldStateAt(T - 9000).payload, receivedAt: T + 3 });
    expect(useRealtimeStore.getState().world?.receivedAt).toBe(T + 2);
  });

  it('연결이 그대로여도 한동안 world 를 못 받았으면 시계가 뒤로 간 프레임도 받는다', () => {
    const store = useRealtimeStore.getState();
    store.receiveWorld({ payload: worldStateAt(T).payload, receivedAt: T });
    store.receiveWorld({ payload: worldStateAt(T - 5000).payload, receivedAt: T + 100 });
    expect(useRealtimeStore.getState().world?.receivedAt).toBe(T);
    store.receiveWorld({
      payload: worldStateAt(T - 5000).payload,
      receivedAt: T + WORLD_STALE_MS + 1,
    });
    expect(useRealtimeStore.getState().world?.receivedAt).toBe(T + WORLD_STALE_MS + 1);
  });

  it('world 를 정한 시간 안에 받았으면 새 것이다', () => {
    expect(isWorldFresh({ world: null }, T)).toBe(false);
    const world = { payload: worldStateAt(T).payload, receivedAt: T };
    expect(isWorldFresh({ world }, T + WORLD_STALE_MS)).toBe(true);
    expect(isWorldFresh({ world }, T + WORLD_STALE_MS + 1)).toBe(false);
  });

  it('world 는 더 새 프레임만 받는다(t_latest_global 기준)', () => {
    const store = useRealtimeStore.getState();
    store.receiveWorld({ payload: worldStateAt(T).payload, receivedAt: T });
    const first = useRealtimeStore.getState().world;
    store.receiveWorld({ payload: worldStateAt(T - 100).payload, receivedAt: T + 10 });
    expect(useRealtimeStore.getState().world).toBe(first);
    store.receiveWorld({ payload: worldStateAt(T + 100).payload, receivedAt: T + 20 });
    expect(useRealtimeStore.getState().world?.receivedAt).toBe(T + 20);
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
