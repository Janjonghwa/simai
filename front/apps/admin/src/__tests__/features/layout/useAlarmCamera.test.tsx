import { mergeEvent, type EventEntry } from '@/features/events/entries';
import type { EventItem } from '@/features/events/types';
import { useAlarmCamera } from '@/features/layout/spatialHost/useAlarmCamera';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  entries: [] as EventEntry[],
  loaded: true,
  tracks: [] as { world_id: string; cameras: string[] }[],
}));
vi.mock('@/features/events/useLiveEvents', () => ({ useLiveEvents: () => state }));
vi.mock('@/store/realtime.store', () => ({
  useRealtimeStore: { getState: () => ({ world: { payload: { tracks: state.tracks } } }) },
}));

function entry(id: string, change: Partial<EventItem> = {}): EventEntry {
  return mergeEvent(undefined, {
    event_id: id,
    event_sequence: 1,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity: 'HIGH',
    confidence: 'HIGH',
    needs_check: false,
    world_ids: ['person'],
    zone_id: null,
    metrics: {},
    rationale: null,
    proposed_severity: null,
    raised_at: 100,
    updated_at: 100,
    ...change,
  });
}

describe('경보 CCTV 전환', () => {
  beforeEach(() => {
    state.entries = [];
    state.loaded = true;
    state.tracks = [{ world_id: 'person', cameras: ['cam-1'] }];
  });
  it('초기 목록과 단순 갱신은 무시하고 새 단계는 전환한다', () => {
    state.entries = [entry('old')];
    const onCamera = vi.fn();
    const { rerender } = renderHook(() =>
      useAlarmCamera({ enabled: true, now: Date.now, onCamera }),
    );
    state.entries = [entry('old', { event_sequence: 2, updated_at: 200 })];
    rerender();
    expect(onCamera).not.toHaveBeenCalled();
    state.entries = [...state.entries, entry('new')];
    rerender();
    expect(onCamera).toHaveBeenCalledWith('cam-1');
    state.entries = [entry('old', { event_sequence: 3, severity: 'CRITICAL', updated_at: 300 })];
    rerender();
    expect(onCamera).toHaveBeenCalledTimes(2);
    state.entries = [
      entry('old', { event_sequence: 4, severity: 'CRITICAL', resume_count: 1, updated_at: 400 }),
    ];
    rerender();
    expect(onCamera).toHaveBeenCalledTimes(3);
  });
  it('수동 선택 15초와 닫힌 창을 존중한다', () => {
    let time = 1000;
    const now = () => time;
    const onCamera = vi.fn();
    let enabled = true;
    const { result, rerender } = renderHook(() => useAlarmCamera({ enabled, now, onCamera }));
    act(() => result.current.markManual());
    state.entries = [entry('a')];
    rerender();
    expect(onCamera).not.toHaveBeenCalled();
    time += 15000;
    state.entries = [...state.entries, entry('b')];
    rerender();
    expect(onCamera).toHaveBeenCalledTimes(1);
    enabled = false;
    state.entries = [...state.entries, entry('c')];
    rerender();
    enabled = true;
    rerender();
    expect(onCamera).toHaveBeenCalledTimes(1);
  });
  it('동시 경보는 등급과 시각 순이며 대상이나 카메라가 없으면 그대로 둔다', () => {
    const onCamera = vi.fn();
    const { rerender } = renderHook(() =>
      useAlarmCamera({ enabled: true, now: Date.now, onCamera }),
    );
    state.entries = [entry('low'), entry('high', { severity: 'CRITICAL', world_ids: [] })];
    rerender();
    expect(onCamera).not.toHaveBeenCalled();
    state.entries = [...state.entries, entry('none', { world_ids: ['missing'] })];
    rerender();
    expect(onCamera).not.toHaveBeenCalled();
    state.tracks.push({ world_id: 'other', cameras: ['cam-2'] });
    state.entries = [
      ...state.entries,
      entry('high2', { severity: 'CRITICAL', world_ids: ['other'], updated_at: 300 }),
      entry('high3', { severity: 'CRITICAL', updated_at: 200 }),
    ];
    rerender();
    expect(onCamera).toHaveBeenCalledWith('cam-2');
  });
});
