import type { EventEntry } from '@/features/events/entries';
import type { EventItem } from '@/features/events/types';
import { appliedStatusLines, workDurationMs, workEventsOf } from '@/features/work/run/run';
import { describe, expect, it } from 'vitest';

const START = new Date(2026, 10, 11, 9, 0).getTime();
const MIN = 60_000;

function entry(overrides: Partial<EventItem> & { event_id: string }): EventEntry {
  return {
    event: {
      rule_id: 'R2',
      source: 'LIVE',
      lifecycle: 'RAISED',
      verification: 'VERIFIED',
      severity: 'CRITICAL',
      confidence: 'HIGH',
      needs_check: false,
      world_ids: [],
      zone_id: 'z-04',
      metrics: {},
      rationale: null,
      proposed_severity: null,
      raised_at: START + MIN,
      ...overrides,
    },
    stageStartedAt: null,
  };
}

describe('workDurationMs', () => {
  it('끝난 시각이 있으면 시작~끝, 없으면 시작~지금이고 음수는 0 이다', () => {
    expect(workDurationMs(START, START + 90 * MIN, START + 999 * MIN)).toBe(90 * MIN);
    expect(workDurationMs(START, null, START + 5 * MIN)).toBe(5 * MIN);
    expect(workDurationMs(START, null, START - MIN)).toBe(0);
  });
});

describe('workEventsOf', () => {
  const entries = [
    entry({ event_id: 'before', raised_at: START - MIN }),
    entry({ event_id: 'first', raised_at: START + MIN }),
    entry({ event_id: 'later', raised_at: START + 10 * MIN, severity: 'HIGH' }),
    entry({ event_id: 'caution', raised_at: START + 2 * MIN, severity: 'CAUTION' }),
    entry({ event_id: 'planned', raised_at: START + 3 * MIN, source: 'PLANNED' }),
    entry({ event_id: 'no-time', raised_at: undefined }),
  ];

  it('작업을 시작한 뒤의 실제 위험·경고만 최근 것부터 고른다', () => {
    expect(workEventsOf(entries, START).map((e) => e.event_id)).toEqual(['later', 'first']);
  });

  it('끝난 시각이 있으면 그때까지만 본다', () => {
    expect(workEventsOf(entries, START, START + 5 * MIN).map((e) => e.event_id)).toEqual(['first']);
  });

  it('시작 기록이 없으면 비어 있다', () => {
    expect(workEventsOf(entries, null)).toEqual([]);
  });
});

describe('appliedStatusLines', () => {
  it('아는 값만 줄로 만든다', () => {
    expect(
      appliedStatusLines({ sceneReady: true, zoneCount: 3, cameras: { total: 4, live: 3 } }),
    ).toEqual(['3D 모델 적용됨', '구역 3개 적용됨', '카메라 3/4 연결']);
    expect(
      appliedStatusLines({ sceneReady: false, zoneCount: null, cameras: { total: 0, live: 0 } }),
    ).toEqual([]);
    expect(
      appliedStatusLines({ sceneReady: true, zoneCount: 0, cameras: { total: 0, live: 0 } }),
    ).toEqual(['3D 모델 적용됨', '구역 0개 적용됨']);
  });
});
