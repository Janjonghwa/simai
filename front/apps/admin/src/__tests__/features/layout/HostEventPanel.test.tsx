import { applyEvents, LIVE_EVENTS_KEY, type LiveEvents } from '@/features/events/eventsCache';
import type { EventItem } from '@/features/events/types';
import { HostEventPanel } from '@/features/layout/spatialHost/HostEventPanel';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

const apiCall = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no api in test'))));

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('@/lib/api', () => ({ api: apiCall }));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({ zones: [], raw: undefined, failed: false }),
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

const cards = () => screen.queryAllByRole('article');
const cardNames = () => cards().map((card) => card.getAttribute('aria-label'));

beforeEach(() => {
  localStorage.clear();
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
  apiCall.mockClear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

it('이전에 저장된 화면 정리 기록이 있어도 진행 중 경보를 표시한다', () => {
  localStorage.setItem(
    'simai.events.cleared',
    JSON.stringify({ state: { clearedAt: T + 1000, clearedBy: 'u' }, version: 0 }),
  );
  const client = new QueryClient();
  applyEvents(client, [item()]);
  client.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => ({ ...old!, loaded: true }));
  render(
    <QueryClientProvider client={client}>
      <HostEventPanel visible selectedKey={null} onSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(cards()).toHaveLength(1);
});

describe('HostEventPanel 확인한 지 오래된 이벤트', () => {
  const HOUR = 3_600_000;
  const ack = (at: number) => ({
    ack_id: `a-${at}`,
    channel: 'CONTROL_ROOM',
    actor_role: 'ADMIN',
    actor_id: 'admin',
    acked_at: at,
  });
  /** 목록을 받은 상태의 캐시로 이벤트들을 그린다. */
  function renderEvents(events: EventItem[]) {
    const queryClient = new QueryClient();
    applyEvents(queryClient, events);
    queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => ({ ...old!, loaded: true }));
    render(
      <QueryClientProvider client={queryClient}>
        <HostEventPanel visible selectedKey={null} onSelect={vi.fn()} />
      </QueryClientProvider>,
    );
  }
  const events = () => [
    // 확인한 지 25시간: 숨김
    item({ event_id: 'old', world_ids: ['w-0001'], acks: [ack(T)], updated_at: T }),
    // 확인한 지 23시간: 남음(1시간 뒤 숨김)
    item({
      event_id: 'recent',
      world_ids: ['w-0002'],
      acks: [ack(T + 2 * HOUR)],
      updated_at: T + 2 * HOUR,
    }),
    // 확인하지 않은 25시간 전 이벤트: 남음
    item({ event_id: 'unacked', world_ids: ['w-0003'], severity: 'HIGH' }),
  ];

  it('확인한 지 24시간이 지난 이벤트는 패널에서 빠지고, 확인하지 않은 이벤트는 남는다', () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(T + 25 * HOUR);
    renderEvents(events());

    expect(cardNames()).toEqual(['w-0002 이벤트', 'w-0003 이벤트']);
    // 남은 둘은 진행 중 칸에 있다(머리줄 사건 수 요약은 뺐다, S15P31A101-798).
    expect(
      within(screen.getByRole('region', { name: '진행 중' }))
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label')),
    ).toEqual(['w-0002 이벤트', 'w-0003 이벤트']);
  });

  it('시간이 흘러 24시간을 넘기는 순간(분 단위) 목록에서 빠진다', () => {
    vi.useFakeTimers();
    // 'recent' 는 T+2h 에 확인했으니 T+26h 에 24시간이 찬다. 그 90초 전에서 시작한다.
    vi.setSystemTime(T + 26 * HOUR - 90_000);
    renderEvents(events());
    expect(cardNames()).toContain('w-0002 이벤트');

    act(() => void vi.advanceTimersByTime(2 * 60_000));

    expect(cardNames()).toEqual(['w-0003 이벤트']);
  });
});
