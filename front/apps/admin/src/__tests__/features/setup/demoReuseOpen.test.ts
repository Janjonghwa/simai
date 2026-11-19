import { demoReuseOpen, demoReuseShown } from '@/features/demo/demoSetup';
import type { ReuseChoiceState, ReuseMapOption } from '@/features/setup/useReuseScene';
import { describe, expect, it } from 'vitest';

// 시연 계정의 이전 맵 재사용: 맵을 시점 확인·추천 없이 고르고(서버 SetupDemoAccounts 와 같은 판정), 하루 전·이틀 전 날짜로 보인다

const map = (overrides: Partial<ReuseMapOption>): ReuseMapOption => ({
  id: 'm',
  dateLabel: '11-17(화) 16:38',
  completedAtMs: 0,
  selectable: false,
  recommended: false,
  status: 'UNCONFIRMED',
  note: '카메라 위치를 확인하고 있어요',
  heightsLabel: '2.41·2.42 m',
  ...overrides,
});

const state = (overrides: Partial<ReuseChoiceState>): ReuseChoiceState => ({
  checking: false,
  blocked: { what: '카메라가 바뀐 것 같아요', how: '새 맵 만들기를 골라 주세요' },
  note: null,
  changedCameras: [],
  maps: [],
  recommendedId: null,
  ...overrides,
});

describe('demoReuseOpen', () => {
  it('제외가 아닌 맵은 상태와 관계없이 고를 수 있고 추천 표시·상태 문구가 없으며, 기본 선택은 가장 최근 맵이다', () => {
    const opened = demoReuseOpen(
      state({
        maps: [
          map({
            id: 'match',
            status: 'MATCH',
            selectable: true,
            recommended: true,
            completedAtMs: 100,
          }),
          map({
            id: 'changed',
            status: 'CHANGED',
            completedAtMs: 300,
            note: '카메라 배치가 그때와 달라요',
          }),
          map({ id: 'shifted', status: 'SHIFTED', completedAtMs: 200 }),
        ],
        recommendedId: 'match',
      }),
    );
    expect(opened.blocked).toBeNull();
    expect(opened.maps.every((m) => m.selectable && !m.recommended && m.note === '')).toBe(true);
    expect(opened.recommendedId).toBe('changed');
  });

  it('보정이 없거나 완료 전인 맵(EXCLUDED)은 그대로 꺼 두고 이유를 남긴다', () => {
    const excluded = map({
      id: 'x',
      status: 'EXCLUDED',
      note: '세팅 완료까지 하지 않은 맵이에요',
      completedAtMs: 900,
    });
    const opened = demoReuseOpen(
      state({ maps: [excluded, map({ id: 'ok', status: 'UNCONFIRMED', completedAtMs: 10 })] }),
    );
    expect(opened.maps[0]).toEqual(excluded);
    expect(opened.recommendedId).toBe('ok');
  });

  it('고를 맵이 하나도 없으면 막는 이유를 그대로 둔다', () => {
    const blocked = { what: '이전 맵이 없어요', how: '새 맵 만들기를 골라 주세요' };
    const opened = demoReuseOpen(state({ blocked, maps: [map({ status: 'EXCLUDED' })] }));
    expect(opened.blocked).toEqual(blocked);
    expect(opened.recommendedId).toBeNull();
  });
});

describe('demoReuseShown', () => {
  // 지금 = 11-19(목) 12:00 KST, 시작할 맵 = 11-16(월) 13:31 KST, 막힌 맵 = 11-17(화) 16:38 KST
  const now = Date.UTC(2026, 10, 19, 3, 0);
  const open = map({
    id: 'mon',
    status: 'MATCH',
    selectable: true,
    completedAtMs: Date.UTC(2026, 10, 16, 4, 31),
    note: '',
  });
  const excluded = map({
    id: 'tue',
    status: 'EXCLUDED',
    completedAtMs: Date.UTC(2026, 10, 17, 7, 38),
    note: '그날 카메라 확인을 통과하지 못한 맵이에요',
  });

  it('재사용이면 시작할 맵을 하루 전, 막힌 맵을 이틀 전 날짜로 보이고 막힌 이유는 그대로 둔다', () => {
    const shown = demoReuseShown(
      state({ maps: [open, excluded], recommendedId: 'mon' }),
      'REUSE',
      now,
    );
    expect(shown.maps.map((m) => [m.id, m.dateLabel, m.selectable, m.note])).toEqual([
      ['mon', '11-18(수) 13:31', true, ''],
      ['demo-blocked-map', '11-17(화) 16:38', false, '그날 카메라 확인을 통과하지 못한 맵이에요'],
    ]);
  });

  it('막힌 맵이 없으면 시작할 맵에 보정이 맞지 않는다는 이유를 달아 이틀 전으로 보인다', () => {
    const shown = demoReuseShown(state({ maps: [open], recommendedId: 'mon' }), 'REUSE', now);
    expect(shown.maps[1]).toMatchObject({
      dateLabel: '11-17(화) 13:31',
      selectable: false,
      note: '지금 카메라 보정과 맞지 않아요',
    });
  });

  it('새 맵이면 목록 없이, 시작할 맵이 없으면 서버 목록 그대로 보인다', () => {
    expect(demoReuseShown(state({ maps: [open], recommendedId: 'mon' }), 'NEW', now).maps).toEqual(
      [],
    );
    const none = state({ maps: [excluded] });
    expect(demoReuseShown(none, 'REUSE', now)).toBe(none);
  });
});
