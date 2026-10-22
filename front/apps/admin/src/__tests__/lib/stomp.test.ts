import { createExternalSource, type SourceConnectionChange } from '@/features/viewer3d';
import { createRealtime, type Realtime } from '@/lib/stomp';
import { riskEventsBetween, viewerHealthAt, worldStateAt } from '@/mocks/realtimeScenario';
import { createStompBroker } from '@/mocks/stompBroker';
import { useRealtimeStore } from '@/store/realtime.store';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const URL = 'ws://stomp.test/ws';
/** 브로커가 받은 CONNECT 의 Authorization. */
const authorizations: (string | undefined)[] = [];
const broker = createStompBroker({
  url: URL,
  authorize: (a) => {
    authorizations.push(a);
    return a?.startsWith('Bearer t-') ?? false;
  },
});
const server = setupServer(broker.handler);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

let realtime: Realtime;
beforeEach(() => {
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  realtime = createRealtime({
    url: URL,
    store: useRealtimeStore,
    healthTimeoutMs: 150,
    reconnectDelayMs: 20,
    watchIntervalMs: 20,
  });
});
afterEach(() => realtime.stop());

const until = async (check: () => boolean, ms = 2000) => {
  const start = Date.now();
  while (!check()) {
    if (Date.now() - start > ms) throw new Error('시간 안에 조건이 맞지 않았다');
    await new Promise((r) => setTimeout(r, 10));
  }
};

/** Source 가 생기면 구독해 뷰어가 받는 것을 모은다. */
async function watchSource() {
  await until(() => useRealtimeStore.getState().source !== null);
  const source = useRealtimeStore.getState().source;
  if (!source) throw new Error('Source 가 없다');
  const messages: unknown[] = [];
  const links: string[] = [];
  source.subscribe({
    onMessage: (raw) => messages.push(raw),
    onConnectionChange: (c: SourceConnectionChange) =>
      links.push(c.reason ? `${c.state}:${c.reason}` : c.state),
  });
  return { source, messages, links };
}

/** 구독이 브로커에 닿을 때까지 health 를 보낸다. */
async function untilHealth() {
  await until(() => {
    broker.publish('/topic/health', viewerHealthAt(Date.now()));
    return useRealtimeStore.getState().health !== null;
  });
}

describe('createRealtime', () => {
  it('다시 연결할 때 가장 새 토큰을 싣는다(토큰 갱신이 연결을 다시 열지 않는다)', async () => {
    let token: string | undefined = 't-1';
    realtime.start({ userId: 'u-1', getToken: () => token });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    authorizations.length = 0;

    token = 't-2';
    broker.dropAll();
    await until(() => authorizations.length > 0 && useRealtimeStore.getState().phase === 'OPEN');
    expect(authorizations).toEqual(['Bearer t-2']);
  });

  it('토큰을 Promise 로 주면 기다렸다가 싣는다(만료 전 갱신)', async () => {
    authorizations.length = 0;
    realtime.start({
      userId: 'u-1',
      getToken: () => new Promise((resolve) => setTimeout(() => resolve('t-fresh'), 30)),
    });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    expect(authorizations).toEqual(['Bearer t-fresh']);
  });

  it('토큰이 없어졌으면 이전 토큰도 Bearer undefined 도 싣지 않는다', async () => {
    let token: string | undefined = 't-1';
    realtime.start({ userId: 'u-1', getToken: () => token });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    authorizations.length = 0;

    token = undefined;
    broker.dropAll();
    await until(() => authorizations.length > 0);
    expect(authorizations.every((a) => a === undefined)).toBe(true);
    expect(useRealtimeStore.getState().phase).not.toBe('OPEN');
  });

  it('토큰으로 연결하고, health 는 저장소에 두고 모든 본문을 뷰어 Source 로 넘긴다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    expect(useRealtimeStore.getState().phase).toBe('CONNECTING');
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');

    await untilHealth();
    broker.publish('/topic/world', worldStateAt(Date.now()));
    await until(() =>
      viewer.messages.some((m) => (m as { msg_type?: string }).msg_type === 'world_state'),
    );

    const { health, world, rejected } = useRealtimeStore.getState();
    expect(health?.payload.system_health).toBe('NOMINAL');
    expect(Math.abs(health?.clockOffsetMs ?? Infinity)).toBeLessThan(1000);
    expect(world?.payload.tracks.length).toBeGreaterThan(0);
    expect(rejected).toBe(0);
    expect(viewer.links).toContain('OPEN');
    expect(viewer.source.id).toBe('live:u-1:1');
  });

  it('계약에 맞지 않는 world 는 버리고, 늦게 온 이전 프레임은 지금 프레임을 바꾸지 않는다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    const t = Date.now();
    await until(() => {
      broker.publish('/topic/world', worldStateAt(t));
      return useRealtimeStore.getState().world !== null;
    });
    const latest = useRealtimeStore.getState().world;

    broker.publish('/topic/world', worldStateAt(t - 5000));
    broker.publish('/topic/world', { msg_type: 'world_state', payload: {} });
    await until(() => useRealtimeStore.getState().rejected === 1);
    expect(useRealtimeStore.getState().world).toBe(latest);
  });

  it('계약을 통과한 risk_event 만 알리고, 연결이 열릴 때마다 open 을 알린다', async () => {
    const events: string[] = [];
    let opens = 0;
    const offEvent = realtime.on('riskEvent', (event) => events.push(event.lifecycle));
    const offOpen = realtime.on('open', () => {
      opens += 1;
    });
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => opens === 1);

    const [raised] = riskEventsBetween(0, 40_000);
    await until(() => {
      broker.publish('/topic/events', raised);
      return events.length > 0;
    });
    const rejected = useRealtimeStore.getState().rejected;
    broker.publish('/topic/events', { msg_type: 'risk_event', payload: {} });
    await until(() => useRealtimeStore.getState().rejected === rejected + 1);
    expect(events.every((e) => e === 'RAISED')).toBe(true);

    broker.dropAll();
    await until(() => opens === 2);
    offEvent();
    offOpen();
  });

  it('3D 뷰어가 받다가 실패해도 앱은 world·health 를 계속 받는다', async () => {
    const rethrown: unknown[] = [];
    const original = globalThis.queueMicrotask;
    // 뷰어 오류는 따로 던져 콘솔에 남긴다. 시험에서는 그 오류를 모은다.
    const later = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation((fn) =>
      original(() => {
        try {
          fn();
        } catch (error) {
          rethrown.push(error);
        }
      }),
    );
    try {
      realtime.start({ userId: 'u-1', getToken: () => 't-1' });
      const { source } = await watchSource();
      source.subscribe({
        onMessage: () => {
          throw new Error('viewer broke');
        },
        onConnectionChange: () => undefined,
      });
      await until(() => useRealtimeStore.getState().phase === 'OPEN');
      await untilHealth();
      broker.publish('/topic/world', worldStateAt(Date.now()));
      await until(() => useRealtimeStore.getState().world !== null);
      expect(rethrown.length).toBeGreaterThan(0);
    } finally {
      later.mockRestore();
    }
  });

  it('3D 뷰어가 새로 구독하면(늦게 붙거나 다시 띄움) 마지막 world·health·이벤트를 다시 받는다', async () => {
    const events: string[] = [];
    const off = realtime.on('riskEvent', (event) => events.push(event.event_id));
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();
    const [raised] = riskEventsBetween(0, 40_000);
    await until(() => {
      broker.publish('/topic/events', raised);
      broker.publish('/topic/world', worldStateAt(Date.now()));
      return events.length > 0 && useRealtimeStore.getState().world !== null;
    });
    off();

    // 메시지를 더 보내지 않아도 새로 구독한 뷰어가 받는다.
    const viewer = await watchSource();
    const types = () => viewer.messages.map((m) => (m as { msg_type?: string }).msg_type);
    await until(() =>
      ['world_state', 'viewer_health', 'risk_event'].every((t) => types().includes(t)),
    );
  });

  it('한 번에 뷰어가 바뀌어도(구독 해제 뒤 바로 구독) 새 뷰어가 가장 새 이벤트를 곧바로 받는다', async () => {
    const events: number[] = [];
    const off = realtime.on('riskEvent', (event) => events.push(event.event_sequence ?? 0));
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => useRealtimeStore.getState().source !== null);
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    const [raised, updated] = riskEventsBetween(0, 40_000)
      .filter((m) => m.payload.rule_id === 'R2')
      .map((m, i) => ({ ...m, payload: { ...m.payload, event_sequence: i + 1 } }));
    // 순서가 뒤바뀌어 와도(2 다음 1) 다시 넘길 것은 순번이 큰 쪽이다.
    await until(() => {
      broker.publish('/topic/events', updated);
      return events.includes(2);
    });
    broker.publish('/topic/events', raised);
    await until(() => events.includes(1));
    off();

    const source = useRealtimeStore.getState().source!;
    const first = source.subscribe({
      onMessage: () => undefined,
      onConnectionChange: () => undefined,
    });
    first();
    const received: unknown[] = [];
    source.subscribe({
      onMessage: (raw) => received.push(raw),
      onConnectionChange: () => undefined,
    });
    const replayed = received.filter(
      (m) => (m as { msg_type?: string }).msg_type === 'risk_event',
    ) as { payload: { event_sequence: number } }[];
    expect(replayed.map((m) => m.payload.event_sequence)).toEqual([2]);
  });

  it('다시 연결되면 시계가 뒤로 간 world 도 받는다(서버 재시작·시계 보정)', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    const t = Date.now();
    await until(() => {
      broker.publish('/topic/world', worldStateAt(t));
      return useRealtimeStore.getState().world !== null;
    });

    broker.dropAll();
    await until(() => useRealtimeStore.getState().phase !== 'OPEN');
    await until(() => useRealtimeStore.getState().phase === 'OPEN' && broker.connections === 1);
    const earlier = worldStateAt(t - 60_000);
    await until(() => {
      broker.publish('/topic/world', earlier);
      return (
        useRealtimeStore.getState().world?.payload.t_latest_global ===
        earlier.payload.t_latest_global
      );
    });
  });

  it('JSON 이 아니거나 계약에 맞지 않는 health 는 버리고 개수만 센다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();
    const before = useRealtimeStore.getState().health;

    broker.publishText('/topic/health', '<html>');
    broker.publish('/topic/health', { msg_type: 'viewer_health', payload: {} });
    await until(() => useRealtimeStore.getState().rejected === 2);
    expect(useRealtimeStore.getState().health).toBe(before);
  });

  it('health 가 정한 시간 넘게 없으면 뷰어에 끊김을 알리고, 다시 오면 열림을 알린다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    await until(() => viewer.links.at(-1) === 'CLOSED:상태 메시지 끊김');
    broker.publish('/topic/health', viewerHealthAt(Date.now()));
    await until(() => viewer.links.at(-1) === 'OPEN');
  });

  it('서버가 연결을 끊으면 끊김을 알리고 다시 연결한다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');

    broker.dropAll();
    await until(() => viewer.links.includes('CLOSED:연결 끊김'));
    await until(() => useRealtimeStore.getState().phase === 'OPEN' && broker.connections === 1);
  });

  it('stop 하면 연결을 끊고 저장소를 비운다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    await realtime.stop();
    expect(useRealtimeStore.getState()).toMatchObject({
      phase: 'IDLE',
      health: null,
      world: null,
      source: null,
    });
    expect(viewer.links.at(-1)).toBe('CLOSED:연결 종료');
    await until(() => broker.connections === 0);
  });

  it('다른 계정으로 다시 start 하면 새 Source 로 바꾸고 이전 데이터를 비운다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 't-1' });
    const first = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    realtime.start({ userId: 'u-2', getToken: () => 't-2' });
    expect(useRealtimeStore.getState()).toMatchObject({ health: null, source: null });
    expect(first.links.at(-1)).toBe('CLOSED:연결 종료');
    await until(() => useRealtimeStore.getState().source?.id === 'live:u-2:2');
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await until(() => broker.connections === 1);
  });

  it('뷰어 입력을 불러오는 사이 stop 하면 늦게 온 입력을 붙이지 않는다', async () => {
    let finish: () => void = () => {};
    const delayed = createRealtime({
      url: URL,
      store: useRealtimeStore,
      loadSourceFactory: () =>
        new Promise((resolve) => {
          finish = () => resolve(createExternalSource);
        }),
    });
    delayed.start({ userId: 'u-1', getToken: () => 't-1' });
    expect(useRealtimeStore.getState().phase).toBe('CONNECTING');

    await delayed.stop();
    finish();
    await new Promise((r) => setTimeout(r, 100));
    expect(useRealtimeStore.getState()).toMatchObject({ phase: 'IDLE', source: null });
    await until(() => broker.connections === 0);
  });

  it('3D 뷰어 코드를 받지 못해도 연결해 world·health·이벤트를 받고, 다시 불러오면 뷰어 입력을 붙인다', async () => {
    let attempts = 0;
    let release: () => void = () => {};
    const retried = new Promise<void>((resolve) => {
      release = resolve;
    });
    const events: string[] = [];
    const flaky = createRealtime({
      url: URL,
      store: useRealtimeStore,
      reconnectDelayMs: 20,
      loadSourceFactory: () =>
        ++attempts === 1
          ? Promise.reject(new Error('chunk load failed'))
          : retried.then(() => createExternalSource),
    });
    const off = flaky.on('riskEvent', (event) => events.push(event.event_id));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    try {
      flaky.start({ userId: 'u-1', getToken: () => 't-1' });
      await until(() => useRealtimeStore.getState().phase === 'OPEN');
      await untilHealth();
      await until(() => {
        broker.publish('/topic/world', worldStateAt(Date.now()));
        broker.publish('/topic/events', riskEventsBetween(0, 40_000)[0]);
        return useRealtimeStore.getState().world !== null && events.length > 0;
      });
      expect(useRealtimeStore.getState().source).toBeNull();
      expect(warn).toHaveBeenCalledTimes(1);

      release();
      const viewer = await watchSource();
      expect(attempts).toBe(2);
      expect(viewer.links.at(-1)).toBe('OPEN');
      // 늦게 붙은 뷰어도 진행 중 이벤트를 받는다.
      expect(
        viewer.messages.some((m) => (m as { msg_type?: string }).msg_type === 'risk_event'),
      ).toBe(true);
    } finally {
      warn.mockRestore();
      off();
      await flaky.stop();
    }
  });

  it('인증이 거부되면 열리지 않는다', async () => {
    realtime.start({ userId: 'u-1', getToken: () => 'bad' });
    await new Promise((r) => setTimeout(r, 200));
    expect(useRealtimeStore.getState().phase).not.toBe('OPEN');
    expect(broker.connections).toBe(0);
  });
});
