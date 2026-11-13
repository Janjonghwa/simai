import type { EventEntry } from '@/features/events/entries';
import { AppliedStatusCard } from '@/features/work/run/AppliedStatusCard';
import { EventListCard } from '@/features/work/run/EventListCard';
import { RunStatusCard } from '@/features/work/run/RunStatusCard';
import { ZONES_KEY } from '@/features/zones/api';
import { MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 작업 종료 단계 오른쪽 카드 셋. 관제 데이터 훅은 대역으로 바꿔 값마다 줄이 어떻게 보이는지 본다.

const START = new Date(2026, 10, 11, 9, 0).getTime();
const NOW = START + 125_000;
const data = vi.hoisted(() => ({
  entries: [] as EventEntry[],
  scene: null as unknown,
  zonesRaw: undefined as unknown,
  zoneCount: 0,
  cameras: [] as { id: string }[],
  down: [] as string[],
}));

vi.mock('@/features/layout/useNow', () => ({ useNow: () => NOW }));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));
vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: data.entries, loaded: true, failed: false }),
}));
vi.mock('@/features/layout/useCurrentScene', () => ({
  useCurrentScene: () => ({
    scene: data.scene,
    sceneStatus: null,
    onSceneStatusChange: () => undefined,
  }),
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({
    zones: Array.from({ length: data.zoneCount }, () => ({})),
    raw: data.zonesRaw,
    failed: false,
  }),
}));
vi.mock('@/features/cctv/useCameraStates', () => ({
  useCameraStates: () => ({
    cameras: data.cameras,
    fresh: true,
    stateOf: (id: string) => (data.down.includes(id) ? 'down' : 'live'),
  }),
}));

afterEach(() => {
  cleanup();
  data.entries = [];
  data.scene = null;
  data.zonesRaw = undefined;
  data.zoneCount = 0;
  data.cameras = [];
  data.down = [];
});

function withClient(node: ReactNode) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const entry = (
  id: string,
  raisedAt: number,
  severity: 'CRITICAL' | 'HIGH',
  zone: string | null,
): EventEntry => ({
  event: {
    event_id: id,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity,
    confidence: 'HIGH',
    needs_check: false,
    world_ids: [],
    zone_id: zone,
    metrics: {},
    rationale: null,
    proposed_severity: null,
    raised_at: raisedAt,
  },
  stageStartedAt: null,
});

describe('RunStatusCard', () => {
  it('진행 중 · 경과 시간 · 시작 시각 · 서명 인원을 보인다', () => {
    render(<RunStatusCard day={{ actual_started_at: START }} signerCount={3} />);
    expect(screen.getByRole('heading', { name: '작업 현황' })).toBeTruthy();
    expect(screen.getByText('진행 중')).toBeTruthy();
    expect(screen.getByRole('timer').textContent).toBe('00:02:05');
    expect(screen.getByText('시작 시각 2026-11-11 09:00')).toBeTruthy();
    expect(screen.getByText('서명 3명')).toBeTruthy();
  });

  it('서명자를 아직 모르거나 시작 기록이 없으면 그 줄을 감춘다', () => {
    render(<RunStatusCard day={{ actual_started_at: null }} signerCount={undefined} />);
    expect(screen.getByRole('timer').textContent).toBe('00:00:00');
    expect(screen.queryByText(/시작 시각/)).toBeNull();
    expect(screen.queryByText(/서명/)).toBeNull();
  });
});

describe('EventListCard', () => {
  it('시작 뒤 사건을 최근 것부터 시각·구역·등급으로 보이고, 구역이 없으면 `현장` 이다', () => {
    data.entries = [
      entry('a', START + 5_000, 'CRITICAL', 'z-04'),
      entry('b', START + 65_000, 'HIGH', null),
    ];
    withClient(<EventListCard startedAt={START} />);
    expect(screen.getByText('2건')).toBeTruthy();
    const rows = within(screen.getByRole('list', { name: '사건 목록' })).getAllByRole('listitem');
    expect(rows[0]?.textContent).toContain('현장');
    expect(rows[0]?.textContent).toContain('경고');
    expect(rows[1]?.textContent).toContain('개구부 구역');
    expect(rows[1]?.textContent).toContain('위험');
  });

  it('사건이 없으면 `사건 없음`', () => {
    withClient(<EventListCard startedAt={START} />);
    expect(screen.getByText('0건')).toBeTruthy();
    expect(screen.getByText('사건 없음')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });
});

describe('AppliedStatusCard', () => {
  it('3D 모델 · 구역 수 · 카메라 연결 수를 줄로 보인다(끊긴 카메라는 연결 수에서 뺀다)', () => {
    data.scene = { ok: true, raw: {} };
    data.zonesRaw = [];
    data.zoneCount = 5;
    data.cameras = [{ id: 'cam1' }, { id: 'cam2' }, { id: 'cam3' }, { id: 'cam4' }];
    data.down = ['cam2', 'cam4'];
    render(<AppliedStatusCard />);
    expect(screen.getByRole('heading', { name: '관제 적용 상태' })).toBeTruthy();
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      '3D 모델 적용됨',
      '구역 5개 적용됨',
      '카메라 2/4 연결',
    ]);
  });

  it('모르는 값은 줄을 감추고, 아무것도 모르면 카드를 그리지 않는다', () => {
    data.zonesRaw = [];
    data.zoneCount = 0;
    const { unmount } = render(<AppliedStatusCard />);
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      '구역 0개 적용됨',
    ]);
    unmount();

    data.zonesRaw = undefined;
    const { container } = render(<AppliedStatusCard />);
    expect(container.textContent).toBe('');
  });
});
