import { startRealtimeMock, type RealtimeMock } from '@/mocks/realtime';
import { EVENT_CYCLE_MS, mockEventId } from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import { validateMessage, type ViewerHealthPayload } from '@simai/contracts';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T0 = 1_760_342_400_000;

let t = T0;
let sent: { topic: string; body: unknown }[] = [];
let mock: RealtimeMock;

const dropAll = vi.fn();
const rejectNextConnects = vi.fn();
const broker = {
  connections: 1,
  publish: (topic: string, body: unknown) => sent.push({ topic, body }),
  dropAll,
  rejectNextConnects,
} as unknown as StompBroker;

/** ms 만큼 시간을 보내고 그동안 보낸 메시지를 돌려준다. */
function run(ms: number) {
  sent = [];
  for (let i = 0; i < ms / 100; i++) {
    t += 100;
    vi.advanceTimersByTime(100);
  }
  const worlds = sent.filter((m) => m.topic === '/topic/world');
  return {
    worlds: worlds.length,
    worldScene: (worlds.at(-1)?.body as { payload: { scene_version: string } } | undefined)?.payload
      .scene_version,
    health: (
      sent.filter((m) => m.topic === '/topic/health').at(-1)?.body as {
        payload: ViewerHealthPayload;
      }
    )?.payload,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  dropAll.mockClear();
  rejectNextConnects.mockClear();
  t = T0;
  mock = startRealtimeMock(broker, { record: (message) => message }, () => t);
});
afterEach(() => {
  mock.stop();
  vi.useRealTimers();
});

describe('실시간 mock 장애 흉내', () => {
  it('장애를 켜면 world 가 끊기고 3초 뒤 health 가 장애를 알리며, 끄면 돌아온다', () => {
    expect(run(1_000).worlds).toBeGreaterThan(0);

    mock.outage('laptop');
    const first = run(1_000);
    expect(first.worlds).toBe(0);
    expect(first.health?.remote_state).toBe('NORMAL');

    const detected = run(3_000);
    expect(detected.worlds).toBe(0);
    expect(detected.health).toMatchObject({
      remote_state: 'CONTROL_HALTED',
      laptop: { link: 'DISCONNECTED' },
    });

    mock.outage(null);
    const back = run(1_000);
    expect(back.worlds).toBeGreaterThan(0);
    expect(back.health?.remote_state).toBe('NORMAL');
  });

  it('Scene 불일치를 켜고 끈다(health 와 world 가 같은 Scene 을 말한다)', () => {
    mock.sceneMismatch(true);
    const on = run(1_000);
    expect(on.health?.scene.mismatch).toBe(true);
    expect(on.worldScene).toBe(on.health?.scene.live_version);
    mock.sceneMismatch(false);
    const off = run(1_000);
    expect(off.health?.scene.mismatch).toBe(false);
    expect(off.worldScene).toBe('scene-3');
  });
});

describe('실시간 mock 조치 불일치(관리자 전용 토픽)', () => {
  const ADMIN = '/topic/admin/events';
  const mismatches = () =>
    sent
      .filter((m) => m.topic === ADMIN)
      .map((m) => m.body as { payload: Record<string, unknown> });

  it('한 바퀴에 18초에 불일치를, 이벤트가 해소되는 22초에 풀림을 /topic/admin/events 로 보낸다', () => {
    run(EVENT_CYCLE_MS);
    const messages = mismatches();
    expect(messages.map((m) => [m.payload.detected_at, m.payload.cleared])).toEqual([
      [T0 + 18_000, false],
      [T0 + 22_000, true],
    ]);
    // 이벤트 전이를 보낸 뒤에 푼다(같은 시각이어도 해소가 먼저다).
    const zoneId = mockEventId(T0 / EVENT_CYCLE_MS, 'zone');
    const resolved = sent.findIndex((m) => {
      const payload = (m.body as { payload: { event_id?: string; lifecycle?: string } }).payload;
      return (
        m.topic === '/topic/events' &&
        payload.event_id === zoneId &&
        payload.lifecycle === 'RESOLVED'
      );
    });
    expect(resolved).toBeGreaterThan(-1);
    const clearedAt = sent.findIndex(
      (m) => m.topic === ADMIN && (m.body as { payload: { cleared: boolean } }).payload.cleared,
    );
    expect(clearedAt).toBeGreaterThan(resolved);
    for (const message of messages) {
      const checked = validateMessage('action_mismatch', message);
      expect(checked.ok ? [] : checked.errors).toEqual([]);
    }
  });

  it('연결이 없으면 보내지 않는다', () => {
    const topics: string[] = [];
    const idle = {
      connections: 0,
      publish: (topic: string) => topics.push(topic),
      dropAll,
      rejectNextConnects,
    } as unknown as StompBroker;
    const other = startRealtimeMock(idle, { record: (message) => message }, () => t);
    try {
      run(EVENT_CYCLE_MS);
      expect(topics).toEqual([]);
    } finally {
      other.stop();
    }
  });

  it('actionMismatch(): 이번 바퀴 zone 이벤트의 불일치를 바로 보내고, cleared 를 주면 풀린 것으로 보낸다', () => {
    sent = [];
    mock.actionMismatch();
    mock.actionMismatch(undefined, true);
    const [raised, cleared] = mismatches();
    expect(raised!.payload).toMatchObject({
      event_id: mockEventId(T0 / EVENT_CYCLE_MS, 'zone'),
      event_lifecycle: 'UPDATED',
      cleared: false,
    });
    expect(cleared!.payload.cleared).toBe(true);
    for (const message of mismatches()) {
      expect(validateMessage('action_mismatch', message).ok).toBe(true);
    }
  });

  it('저장소가 아는 이벤트면 그 순번·단계를 따르고, 해소됐으면 풀린 것으로 보낸다', () => {
    const known = startRealtimeMock(
      broker,
      {
        record: (message) => message,
        find: () => ({ event_sequence: 4, lifecycle: 'RESOLVED', zone_id: 'z-09' }),
      },
      () => t,
    );
    try {
      sent = [];
      known.actionMismatch('0192a6f0-7c1e-7b3a-9f2d-00000000abcd');
      expect(mismatches()[0]!.payload).toMatchObject({
        event_id: '0192a6f0-7c1e-7b3a-9f2d-00000000abcd',
        event_sequence: 4,
        event_lifecycle: 'RESOLVED',
        zone_id: 'z-09',
        cleared: true,
      });
    } finally {
      known.stop();
    }
  });

  it('expireStomp(): 다음 연결을 ERROR 로 거절하게 하고 지금 연결을 끊는다', () => {
    mock.expireStomp();
    expect(rejectNextConnects).toHaveBeenCalledTimes(1);
    expect(dropAll).toHaveBeenCalledTimes(1);
    expect(rejectNextConnects.mock.invocationCallOrder[0]).toBeLessThan(
      dropAll.mock.invocationCallOrder[0]!,
    );
  });
});
