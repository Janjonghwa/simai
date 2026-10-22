import { createExternalSource, type SourceConnectionChange } from '@/features/viewer3d';
import { createRealtime, type Realtime } from '@/lib/stomp';
import { riskEventsBetween, viewerHealthAt, worldStateAt } from '@/mocks/realtimeScenario';
import { createStompBroker } from '@/mocks/stompBroker';
import { useRealtimeStore } from '@/store/realtime.store';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const URL = 'ws://stomp.test/ws';
const broker = createStompBroker({
  url: URL,
  authorize: (a) => a?.startsWith('Bearer t-') ?? false,
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
  it('토큰으로 연결하고, health 는 저장소에 두고 모든 본문을 뷰어 Source 로 넘긴다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    expect(useRealtimeStore.getState().phase).toBe('CONNECTING');
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');

    await untilHealth();
    broker.publish('/topic/world', worldStateAt(Date.now()));
    await until(() =>
      viewer.messages.some((m) => (m as { msg_type?: string }).msg_type === 'world_state'),
    );

    const { health, rejected } = useRealtimeStore.getState();
    expect(health?.payload.system_health).toBe('NOMINAL');
    expect(Math.abs(health?.clockOffsetMs ?? Infinity)).toBeLessThan(1000);
    expect(rejected).toBe(0);
    expect(viewer.links).toContain('OPEN');
    expect(viewer.source.id).toBe('live:u-1:1');
  });

  it('계약을 통과한 risk_event 만 알리고, 연결이 열릴 때마다 open 을 알린다', async () => {
    const events: string[] = [];
    let opens = 0;
    const offEvent = realtime.on('riskEvent', (event) => events.push(event.lifecycle));
    const offOpen = realtime.on('open', () => {
      opens += 1;
    });
    realtime.start({ token: 't-1', userId: 'u-1' });
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

  it('JSON 이 아니거나 계약에 맞지 않는 health 는 버리고 개수만 센다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();
    const before = useRealtimeStore.getState().health;

    broker.publishText('/topic/health', '<html>');
    broker.publish('/topic/health', { msg_type: 'viewer_health', payload: {} });
    await until(() => useRealtimeStore.getState().rejected === 2);
    expect(useRealtimeStore.getState().health).toBe(before);
  });

  it('health 가 정한 시간 넘게 없으면 뷰어에 끊김을 알리고, 다시 오면 열림을 알린다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    await until(() => viewer.links.at(-1) === 'CLOSED:상태 메시지 끊김');
    broker.publish('/topic/health', viewerHealthAt(Date.now()));
    await until(() => viewer.links.at(-1) === 'OPEN');
  });

  it('서버가 연결을 끊으면 끊김을 알리고 다시 연결한다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');

    broker.dropAll();
    await until(() => viewer.links.includes('CLOSED:연결 끊김'));
    await until(() => useRealtimeStore.getState().phase === 'OPEN' && broker.connections === 1);
  });

  it('stop 하면 연결을 끊고 저장소를 비운다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    const viewer = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    await realtime.stop();
    expect(useRealtimeStore.getState()).toMatchObject({
      phase: 'IDLE',
      health: null,
      source: null,
    });
    expect(viewer.links.at(-1)).toBe('CLOSED:연결 종료');
    await until(() => broker.connections === 0);
  });

  it('다른 계정으로 다시 start 하면 새 Source 로 바꾸고 이전 데이터를 비운다', async () => {
    realtime.start({ token: 't-1', userId: 'u-1' });
    const first = await watchSource();
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await untilHealth();

    realtime.start({ token: 't-2', userId: 'u-2' });
    expect(useRealtimeStore.getState()).toMatchObject({ health: null, source: null });
    expect(first.links.at(-1)).toBe('CLOSED:연결 종료');
    await until(() => useRealtimeStore.getState().source?.id === 'live:u-2:2');
    await until(() => useRealtimeStore.getState().phase === 'OPEN');
    await until(() => broker.connections === 1);
  });

  it('뷰어 입력을 불러오는 사이 stop 하면 연결을 열지 않는다', async () => {
    let finish: () => void = () => {};
    const delayed = createRealtime({
      url: URL,
      store: useRealtimeStore,
      loadSourceFactory: () =>
        new Promise((resolve) => {
          finish = () => resolve(createExternalSource);
        }),
    });
    delayed.start({ token: 't-1', userId: 'u-1' });
    expect(useRealtimeStore.getState().phase).toBe('CONNECTING');

    await delayed.stop();
    finish();
    await new Promise((r) => setTimeout(r, 100));
    expect(useRealtimeStore.getState()).toMatchObject({ phase: 'IDLE', source: null });
    expect(broker.connections).toBe(0);
  });

  it('뷰어 입력을 불러오지 못하면 다시 불러와 연결한다', async () => {
    let attempts = 0;
    const flaky = createRealtime({
      url: URL,
      store: useRealtimeStore,
      reconnectDelayMs: 20,
      loadSourceFactory: () =>
        ++attempts === 1
          ? Promise.reject(new Error('chunk load failed'))
          : Promise.resolve(createExternalSource),
    });
    try {
      flaky.start({ token: 't-1', userId: 'u-1' });
      await until(() => useRealtimeStore.getState().phase === 'OPEN');
      expect(attempts).toBe(2);
    } finally {
      await flaky.stop();
    }
  });

  it('인증이 거부되면 열리지 않는다', async () => {
    realtime.start({ token: 'bad', userId: 'u-1' });
    await new Promise((r) => setTimeout(r, 200));
    expect(useRealtimeStore.getState().phase).not.toBe('OPEN');
    expect(broker.connections).toBe(0);
  });
});
