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
  const on = { stage: 'on', problem: null } as const;
  const lines = (input: Parameters<typeof appliedStatusLines>[0]) =>
    appliedStatusLines(input).map((line) => `${line.tone}:${line.text}`);

  it('이번 작업 묶음이 적용되면 경보 켜짐·구역 수에 체크하고, 카메라는 모두 이어졌을 때만 체크한다', () => {
    expect(
      lines({ phase: on, sceneReady: true, appliedZoneCount: 3, cameras: { total: 4, live: 4 } }),
    ).toEqual([
      'ok:이번 작업 설정 현장 적용됨 · 경보 켜짐',
      'ok:3D 모델 적용됨',
      'ok:구역 3개 적용됨',
      'ok:카메라 4/4 연결',
    ]);
    // 카메라 0/4 는 체크가 아니라 경고다(-692)
    expect(
      lines({ phase: on, sceneReady: false, appliedZoneCount: 0, cameras: { total: 4, live: 0 } }),
    ).toEqual([
      'ok:이번 작업 설정 현장 적용됨 · 경보 켜짐',
      'ok:구역 0개 적용됨',
      'bad:카메라 0/4 연결',
    ]);
  });

  it('적용 전에는 대기·지연·거부·끊김을 가르고 구역 수 줄(이전 묶음 값)을 만들지 않는다', () => {
    const base = { sceneReady: false, appliedZoneCount: 5, cameras: { total: 0, live: 0 } };
    expect(lines({ ...base, phase: { stage: 'starting', problem: null } })).toEqual([
      'wait:현장 적용 대기 · 경보 꺼짐',
    ]);
    expect(lines({ ...base, phase: { stage: 'starting', problem: 'delayed' } })).toEqual([
      'bad:현장 적용 지연 · 경보 꺼짐',
    ]);
    expect(lines({ ...base, phase: { stage: 'starting', problem: 'rejected' } })).toEqual([
      'bad:현장 적용 거부 · 경보 꺼짐',
    ]);
    expect(lines({ ...base, phase: { stage: 'starting', problem: 'link_lost' } })).toEqual([
      'bad:현장 연결 끊김 · 경보 확인 불가',
    ]);
    expect(lines({ ...base, phase: { stage: 'on', problem: 'link_lost' } })).toEqual([
      'bad:현장 연결 끊김 · 경보 확인 불가',
      'ok:구역 5개 적용됨',
    ]);
  });

  it('모르는 값은 줄을 만들지 않는다', () => {
    expect(
      lines({
        phase: { stage: 'unknown', problem: null },
        sceneReady: false,
        appliedZoneCount: null,
        cameras: { total: 0, live: 0 },
      }),
    ).toEqual([]);
  });
});
