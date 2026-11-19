import { useEventClearStore } from '@/features/events/eventClear.store';
import { applyEvents, LIVE_EVENTS_KEY, type LiveEvents } from '@/features/events/eventsCache';
import type { EventItem } from '@/features/events/types';
import { LivePlan } from '@/features/plan/LivePlan';
import type { PlanBounds, PlanMarks } from '@/features/plan/plan';
import { ROOM_BOUNDS } from '@/features/plan/plan';
import type { HeadingMemory } from '@/features/plan/planHeading';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { WorldStatePayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

vi.mock('@/lib/api', () => ({ api: vi.fn(() => Promise.reject(new Error('no api in test'))) }));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: undefined, failed: false }),
}));
vi.mock('@/features/plan/usePlanBackdrop', () => ({
  usePlanBackdrop: () => ({ bounds: ROOM_BOUNDS, backdrop: undefined }),
}));
// 캔버스를 그리지 않고, 평면이 받은 표시(marks)에 든 사람·비출 범위·사람 방향만 적는다.
vi.mock('@/features/plan/FloorPlan', () => ({
  FloorPlan: ({
    marks,
    view,
    headings,
  }: {
    marks: PlanMarks;
    view?: PlanBounds;
    headings?: HeadingMemory;
  }) => (
    <>
      <output aria-label="표시된 사람">{[...marks.people.keys()].join(',')}</output>
      <output aria-label="비출 범위">{view ? JSON.stringify(view) : ''}</output>
      <output aria-label="사람 방향">
        {[...(headings ?? [])]
          .map(([id, h]) => `${id}:${h.yaw === null ? '-' : h.yaw.toFixed(2)}`)
          .join(',')}
      </output>
    </>
  ),
}));

function item(change: Partial<EventItem> = {}): EventItem {
  return {
    event_id: 'e-1',
    event_sequence: 1,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity: 'CRITICAL',
    confidence: 'HIGH',
    needs_check: false,
    world_ids: ['w-0015'],
    zone_id: 'z-04',
    metrics: {},
    rationale: null,
    proposed_severity: null,
    raised_at: T,
    updated_at: T,
    ...change,
  };
}

const shown = () => screen.getByLabelText('표시된 사람').textContent;

beforeEach(() => {
  localStorage.clear();
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  useRealtimeStore.setState({ world: null });
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

describe('LivePlan 모두 지우기', () => {
  it('지운 경보는 평면 표시에서도 빠지고, 지운 뒤에 생긴 경보만 남는다', async () => {
    const queryClient = new QueryClient();
    applyEvents(queryClient, [item()]);
    queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => ({ ...old!, loaded: true }));
    render(
      <QueryClientProvider client={queryClient}>
        <LivePlan selectedKey={null} />
      </QueryClientProvider>,
    );
    expect(shown()).toBe('w-0015');

    act(() => useEventClearStore.getState().clearAll(T));
    expect(shown()).toBe('');

    act(() =>
      applyEvents(queryClient, [
        item({
          event_id: 'e-2',
          world_ids: ['w-0021'],
          raised_at: T + 1_000,
          updated_at: T + 1_000,
        }),
      ]),
    );
    await waitFor(() => expect(shown()).toBe('w-0021'));
  });
});

describe('LivePlan 미니맵 범위·사람 방향(-800)', () => {
  /** 위로(+Y) 걷는 사람 한 명의 world 를 시각 t 에 받는다. */
  function receive(t: number) {
    const payload = {
      t_latest_global: t,
      calibration_version: 'cal-1',
      scene_version: 'scene-1',
      tracks: [
        {
          world_id: 'w-0015',
          position_xy: [4, 5],
          position_xy_sigma: [0.1, 0.1],
          observation_state: 'OBSERVED',
          track_lifecycle: 'CONFIRMED',
          velocity: [0, 1, 0],
        },
      ],
    } as unknown as WorldStatePayload;
    useRealtimeStore.setState({ world: { payload, receivedAt: t } });
  }

  /** 첫 world 를 받은 뒤 그리고, 이동이 이어지게 100ms 마다 두 번 더 받는다. */
  function renderLive(fit: boolean) {
    receive(T);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <LivePlan selectedKey={null} fit={fit} />
      </QueryClientProvider>,
    );
    // 이동이 막 시작한 첫 world 에서는 방향을 보이지 않는다(점만).
    expect(screen.getByLabelText('사람 방향').textContent).toBe('w-0015:-');
    act(() => receive(T + 100));
    act(() => receive(T + 200));
  }

  it('미니맵(fit)은 사람이 있는 곳에 맞춘 범위를 넘기고, 사람 방향은 world 속도에서 온다', () => {
    renderLive(true);
    const view = JSON.parse(screen.getByLabelText('비출 범위').textContent!) as PlanBounds;
    expect(view.minX).toBeLessThan(4);
    expect(view.maxX).toBeGreaterThan(4);
    expect(view.maxX - view.minX).toBeLessThan(ROOM_BOUNDS.maxX - ROOM_BOUNDS.minX);
    expect(screen.getByLabelText('사람 방향').textContent).toBe('w-0015:1.57');
  });

  it('큰 평면(fit 아님)은 범위를 넘기지 않아 방 전체를 비춘다', () => {
    renderLive(false);
    expect(screen.getByLabelText('비출 범위').textContent).toBe('');
    expect(screen.getByLabelText('사람 방향').textContent).toBe('w-0015:1.57');
  });
});
