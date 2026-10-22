import { mergeEvent, type EventEntry } from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import { shouldRing, useAlarmSound } from '@/features/events/useAlarmSound';
import { ALARM_PERIOD_MS, alarmSound } from '@/lib/alarmSound';
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const link = vi.hoisted(() => ({ alive: true, canAck: true }));
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

const acked = entry({
  event_sequence: 2,
  lifecycle: 'UPDATED',
  updated_at: T + 1000,
  acks: [
    {
      ack_id: 'a-1',
      channel: 'CONTROL_ROOM',
      actor_role: 'ADMIN',
      actor_id: 'admin',
      acked_at: T + 1000,
    },
  ],
});

describe('shouldRing', () => {
  it('확인 대기 경보가 있고 EC2 와 이어져 있을 때만 울린다', () => {
    expect(shouldRing([entry()], true, true)).toBe(true);
    expect(shouldRing([entry()], false, true)).toBe(false);
    expect(shouldRing([], true, true)).toBe(false);
  });

  it('확인했거나 소리 대상 등급이 아니면 울리지 않는다', () => {
    expect(shouldRing([acked], true, true)).toBe(false);
    expect(shouldRing([entry({ severity: 'CAUTION' })], true, true)).toBe(false);
    expect(shouldRing([entry({ lifecycle: 'RESOLVED', resolved_at: T })], true, true)).toBe(false);
  });

  it('확인 권한이 없는 열람 계정은 끌 수 없으므로 울리지 않는다', () => {
    expect(shouldRing([entry()], true, false)).toBe(false);
  });
});

describe('useAlarmSound', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    link.alive = true;
    link.canAck = true;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('경보가 있는 동안 주기마다 되풀이하고, 확인하면 멈춘다', () => {
    const beep = vi.spyOn(alarmSound, 'beep').mockImplementation(() => undefined);
    const { rerender } = renderHook(({ entries }) => useAlarmSound(entries), {
      initialProps: { entries: [entry()] },
    });
    expect(beep).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(ALARM_PERIOD_MS * 2);
    expect(beep).toHaveBeenCalledTimes(3);

    rerender({ entries: [acked] });
    vi.advanceTimersByTime(ALARM_PERIOD_MS * 3);
    expect(beep).toHaveBeenCalledTimes(3);
  });

  it('연결이 끊기면 멈춘다', () => {
    const beep = vi.spyOn(alarmSound, 'beep').mockImplementation(() => undefined);
    const entries = [entry()];
    const { rerender } = renderHook(() => useAlarmSound(entries));
    expect(beep).toHaveBeenCalledTimes(1);

    link.alive = false;
    rerender();
    vi.advanceTimersByTime(ALARM_PERIOD_MS * 3);
    expect(beep).toHaveBeenCalledTimes(1);
  });
});
