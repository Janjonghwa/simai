import { mergeEvent, type EventEntry } from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import { ringingSeverity, useAlarmSound } from '@/features/events/useAlarmSound';
import { alarmSound } from '@/lib/alarmSound';
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const link = vi.hoisted(() => ({ alive: true, canAck: true }));
// 소리는 EC2 연결과 상관없이 확인까지 이어진다. 끊김을 흉내 내도 훅이 연결을 보지 않음을 확인한다.
vi.mock('@/hooks/useLinkAlive', () => ({ useLinkAlive: () => link.alive }));
vi.mock('@/features/events/ack', () => ({ useCanAck: () => link.canAck }));

const T = 1_760_342_400_000;

function entry(change: Partial<EventItem> = {}): EventEntry {
  return mergeEvent(undefined, {
    event_id: 'e-1',
    event_sequence: 1,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity: 'CRITICAL',
    confidence: 'HIGH',
    needs_check: false,
    world_ids: ['w-1'],
    zone_id: 'z-04',
    metrics: {},
    rationale: null,
    proposed_severity: null,
    raised_at: T,
    updated_at: T,
    ...change,
  });
}

const ack = [
  {
    ack_id: 'a-1',
    channel: 'CONTROL_ROOM' as const,
    actor_role: 'ADMIN' as const,
    actor_id: 'admin',
    acked_at: T + 1000,
  },
];

const acked = (change: Partial<EventItem> = {}) =>
  entry({ event_sequence: 2, lifecycle: 'UPDATED', updated_at: T + 1000, acks: ack, ...change });

describe('ringingSeverity', () => {
  it('확인 대기 위험·경고 경보가 없으면 null 이다', () => {
    expect(ringingSeverity([], true)).toBeNull();
  });

  it('확인 대기 경보 중 가장 높은 등급 하나를 고른다', () => {
    const high = entry({ event_id: 'e-h', severity: 'HIGH' });
    const critical = entry({ event_id: 'e-c', severity: 'CRITICAL' });
    expect(ringingSeverity([entry()], true)).toBe('CRITICAL');
    expect(ringingSeverity([high], true)).toBe('HIGH');
    expect(ringingSeverity([high, critical], true)).toBe('CRITICAL');
    expect(ringingSeverity([critical, high], true)).toBe('CRITICAL');
  });

  it('높은 등급을 확인하면 남은 경보 등급으로 내려간다', () => {
    const high = entry({ event_id: 'e-h', severity: 'HIGH' });
    const criticalAcked = acked({ event_id: 'e-c' });
    expect(ringingSeverity([high, criticalAcked], true)).toBe('HIGH');
    expect(
      ringingSeverity([acked({ event_id: 'e-h', severity: 'HIGH' }), criticalAcked], true),
    ).toBeNull();
  });

  it('주의·안내 등급과 해소된 경보는 소리가 없다', () => {
    expect(ringingSeverity([entry({ severity: 'CAUTION' })], true)).toBeNull();
    expect(ringingSeverity([entry({ severity: 'INFO' })], true)).toBeNull();
    expect(ringingSeverity([entry({ lifecycle: 'RESOLVED', resolved_at: T })], true)).toBeNull();
  });

  it('시험(비 LIVE) 경보는 울리지 않는다', () => {
    expect(ringingSeverity([entry({ source: 'REPLAY' })], true)).toBeNull();
  });

  it('확인 권한이 없는 열람 계정은 끌 수 없으므로 울리지 않는다', () => {
    expect(ringingSeverity([entry()], false)).toBeNull();
  });
});

describe('useAlarmSound', () => {
  beforeEach(() => {
    link.alive = true;
    link.canAck = true;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('가장 높은 등급을 울리고, 확인하면 멈춘다', () => {
    const ring = vi.spyOn(alarmSound, 'ring').mockImplementation(() => undefined);
    const { rerender } = renderHook(({ entries }) => useAlarmSound(entries), {
      initialProps: { entries: [entry()] },
    });
    expect(ring).toHaveBeenLastCalledWith('CRITICAL');

    rerender({ entries: [acked()] });
    expect(ring).toHaveBeenLastCalledWith(null);
  });

  it('등급이 바뀌면 곧바로 새 등급으로 넘기고, 그대로면 다시 부르지 않는다', () => {
    const ring = vi.spyOn(alarmSound, 'ring').mockImplementation(() => undefined);
    const high = entry({ event_id: 'e-h', severity: 'HIGH' });
    const critical = entry({ event_id: 'e-c' });
    const { rerender } = renderHook(({ entries }) => useAlarmSound(entries), {
      initialProps: { entries: [high] },
    });
    expect(ring).toHaveBeenLastCalledWith('HIGH');

    rerender({ entries: [high, critical] });
    expect(ring).toHaveBeenLastCalledWith('CRITICAL');
    const calls = ring.mock.calls.length;
    // 같은 등급인 새 목록은 소리를 다시 시작하지 않는다.
    rerender({ entries: [critical, high] });
    expect(ring.mock.calls.length).toBe(calls);

    rerender({ entries: [high, acked({ event_id: 'e-c' })] });
    expect(ring).toHaveBeenLastCalledWith('HIGH');
  });

  it('연결이 끊겨도 확인(ACK)될 때까지 멈추지 않는다', () => {
    const ring = vi.spyOn(alarmSound, 'ring').mockImplementation(() => undefined);
    const entries = [entry()];
    const { rerender } = renderHook(({ list }) => useAlarmSound(list), {
      initialProps: { list: entries },
    });
    expect(ring).toHaveBeenLastCalledWith('CRITICAL');

    link.alive = false;
    rerender({ list: entries });
    expect(ring).toHaveBeenLastCalledWith('CRITICAL');
    expect(ring).not.toHaveBeenCalledWith(null);

    // 끊긴 화면에서 확인한 것이 얼어 있는 목록에 쓰이면 그때 멈춘다.
    rerender({ list: [acked()] });
    expect(ring).toHaveBeenLastCalledWith(null);
  });

  it('확인 권한이 없는 계정은 울리지 않는다', () => {
    link.canAck = false;
    const ring = vi.spyOn(alarmSound, 'ring').mockImplementation(() => undefined);
    renderHook(() => useAlarmSound([entry()]));
    expect(ring).toHaveBeenLastCalledWith(null);
    expect(ring).not.toHaveBeenCalledWith('CRITICAL');
  });

  it('화면이 사라지면 멈춘다', () => {
    const ring = vi.spyOn(alarmSound, 'ring').mockImplementation(() => undefined);
    const { unmount } = renderHook(() => useAlarmSound([entry()]));
    expect(ring).toHaveBeenLastCalledWith('CRITICAL');
    unmount();
    expect(ring).toHaveBeenLastCalledWith(null);
  });
});
