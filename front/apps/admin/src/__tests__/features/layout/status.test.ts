import {
  bandFailure,
  chipState,
  clockLabel,
  dataAgeSec,
  failureOf,
  lastDataClock,
} from '@/features/layout/status';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;

function snapshot(change: Partial<ViewerHealthPayload> = {}, receivedAt = T): HealthSnapshot {
  const payload = { ...viewerHealthAt(T).payload, ...change };
  return { payload, receivedAt, clockOffsetMs: 0 };
}

describe('chipState', () => {
  const open = { phase: 'OPEN' as const, openedAt: T, health: snapshot() };

  it('연결 전에는 비우고, 이어져 있으면 System Health, 끊기면 LOST 다', () => {
    expect(chipState({ phase: 'IDLE', openedAt: null, health: null }, false)).toBeUndefined();
    expect(chipState({ phase: 'CONNECTING', openedAt: null, health: null }, false)).toBeUndefined();
    expect(chipState(open, true)).toBe('NOMINAL');
    expect(chipState(open, false)).toBe('LOST');
    // 다시 연결하는 중에도 지금 상태를 모르므로 LOST 다.
    expect(chipState({ ...open, phase: 'CONNECTING' }, false)).toBe('LOST');
  });

  it('열렸지만 health 를 아직 못 받았으면 비운다', () => {
    expect(chipState({ ...open, health: null }, true)).toBeUndefined();
  });
});

describe('데이터 연령·시계', () => {
  it('데이터 연령은 EC2 가 잰 값에 받은 뒤 흐른 시간을 더한다', () => {
    const health = snapshot({ data_age_ms: 200 }, T);
    expect(dataAgeSec(health, T)).toBeCloseTo(0.2);
    expect(dataAgeSec(health, T + 5000)).toBeCloseTo(5.2);
    expect(dataAgeSec(snapshot({ data_age_ms: null }), T)).toBeUndefined();
    expect(dataAgeSec(null, T)).toBeUndefined();
  });

  it('시계는 EC2 와의 차이를 더한 시각이다', () => {
    const now = new Date(2026, 9, 21, 14, 30, 5).getTime();
    expect(clockLabel(0, now)).toBe('14:30:05');
    expect(clockLabel(-5000, now)).toBe('14:30:00');
    expect(clockLabel(undefined, now)).toBeUndefined();
  });

  it('마지막 데이터 시각은 EC2 시각에서 데이터 연령을 뺀 때다', () => {
    const asOf = new Date(2026, 9, 21, 14, 30, 5).getTime();
    expect(lastDataClock(snapshot({ as_of: asOf, data_age_ms: 12_000 }))).toBe('14:29:53');
    expect(lastDataClock(snapshot({ data_age_ms: null }))).toBeUndefined();
    expect(lastDataClock(null)).toBeUndefined();
  });
});

describe('failureOf', () => {
  const state = (
    remote_state: ViewerHealthPayload['remote_state'],
    laptop: ViewerHealthPayload['laptop']['link'] = 'CONNECTED',
  ) => snapshot({ remote_state, laptop: { link: laptop, last_heartbeat_at: T } });

  it('EC2 가 알린 remote_state 와 노트북 연결로 장애 종류를 정한다', () => {
    expect(failureOf(state('NORMAL'), true)).toBeUndefined();
    expect(failureOf(state('REMOTE_LINK_LOST'), true)).toBe('REMOTE_LINK_LOST');
    expect(failureOf(state('CONTROL_HALTED'), true)).toBe('CONTROL_HALTED');
    // 노트북 하트비트가 끊겨도 CONTROL_HALTED 다. 노트북 정지는 따로 알린다.
    expect(failureOf(state('CONTROL_HALTED', 'DISCONNECTED'), true)).toBe('DEVICE_DOWN');
  });

  it('EC2 와 끊겼으면 마지막으로 받은 상태로 장애를 알리지 않는다', () => {
    expect(failureOf(state('CONTROL_HALTED'), false)).toBeUndefined();
    expect(failureOf(null, true)).toBeUndefined();
  });
});

describe('bandFailure', () => {
  const health = snapshot({ remote_state: 'CONTROL_HALTED' });

  it('이어진 적이 있는데 지금 끊겼으면 마지막 상태가 아니라 연결 끊김이다', () => {
    expect(bandFailure({ phase: 'OPEN', openedAt: T, health }, false)).toBe('LINK_LOST');
    expect(bandFailure({ phase: 'CONNECTING', openedAt: T, health }, false)).toBe('LINK_LOST');
  });

  it('이어져 있으면 EC2 가 알린 장애이고, 연결 전이면 없다', () => {
    expect(bandFailure({ phase: 'OPEN', openedAt: T, health }, true)).toBe('CONTROL_HALTED');
    expect(bandFailure({ phase: 'IDLE', openedAt: null, health: null }, false)).toBeUndefined();
    expect(
      bandFailure({ phase: 'CONNECTING', openedAt: null, health: null }, false),
    ).toBeUndefined();
  });
});
