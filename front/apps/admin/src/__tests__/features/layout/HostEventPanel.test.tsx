import { useEventClearStore } from '@/features/events/eventClear.store';
import { applyEvents, LIVE_EVENTS_KEY, type LiveEvents } from '@/features/events/eventsCache';
import type { EventItem } from '@/features/events/types';
import { HostEventPanel } from '@/features/layout/spatialHost/HostEventPanel';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

/** 목록을 받은 상태의 캐시. useLiveEvents 는 받은 목록이면 다시 요청하지 않는다. */
function renderHost() {
  const queryClient = new QueryClient();
  applyEvents(queryClient, [
    item(),
    item({ event_id: 'e-2', world_ids: ['w-0021'], severity: 'HIGH' }),
  ]);
  queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => ({ ...old!, loaded: true }));
  render(
    <QueryClientProvider client={queryClient}>
      <HostEventPanel visible selectedKey={null} onSelect={vi.fn()} />
    </QueryClientProvider>,
  );
  return queryClient;
}

const clearButton = () => screen.getByRole('button', { name: '모두 지우기' });
const cards = () => screen.queryAllByRole('article');
const cardNames = () => cards().map((card) => card.getAttribute('aria-label'));

beforeEach(() => {
  localStorage.clear();
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
  apiCall.mockClear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

describe('HostEventPanel 모두 지우기', () => {
  it('누르면 목록이 비고 서버는 부르지 않으며 이벤트 캐시는 그대로다', () => {
    const queryClient = renderHost();
    const before = queryClient.getQueryData<LiveEvents>(LIVE_EVENTS_KEY)!;
    expect(cards()).toHaveLength(2);

    fireEvent.click(clearButton());

    expect(cards()).toHaveLength(0);
    expect(screen.queryByRole('button', { name: '모두 지우기' })).toBeNull();
    expect(screen.getByRole('button', { name: '이벤트 펼치기' })).toBeTruthy();
    expect(apiCall).not.toHaveBeenCalled();
    expect(queryClient.getQueryData<LiveEvents>(LIVE_EVENTS_KEY)).toBe(before);
    expect(before.entries).toHaveLength(2);
  });

  it('지운 시각은 목록에서 가장 늦게 시작된 경보의 시각이고 지운 사용자를 함께 남긴다', async () => {
    const queryClient = renderHost();
    act(() =>
      applyEvents(queryClient, [
        item({
          event_id: 'e-3',
          world_ids: ['w-0030'],
          raised_at: T + 7_000,
          updated_at: T + 7_000,
        }),
      ]),
    );
    await waitFor(() => expect(cards()).toHaveLength(3));
    fireEvent.click(clearButton());
    expect(useEventClearStore.getState()).toMatchObject({ clearedAt: T + 7_000, clearedBy: 'u' });
  });

  it('브라우저 시계가 하루 빨라도 지운 뒤에 생긴 경보는 숨지 않는다', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(T + 86_400_000);
    const queryClient = renderHost();
    fireEvent.click(clearButton());
    expect(useEventClearStore.getState().clearedAt).toBe(T);

    act(() =>
      applyEvents(queryClient, [
        item({
          event_id: 'e-3',
          world_ids: ['w-0030'],
          raised_at: T + 20_000,
          updated_at: T + 20_000,
        }),
      ]),
    );
    await waitFor(() => expect(cardNames()).toEqual(['w-0030 이벤트']));
  });

  it('시각을 아는 경보가 하나도 없으면 아무것도 지우지 않는다', () => {
    const queryClient = new QueryClient();
    applyEvents(queryClient, [item({ raised_at: undefined, updated_at: undefined })]);
    queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, (old) => ({ ...old!, loaded: true }));
    render(
      <QueryClientProvider client={queryClient}>
        <HostEventPanel visible selectedKey={null} onSelect={vi.fn()} />
      </QueryClientProvider>,
    );

    fireEvent.click(clearButton());

    expect(useEventClearStore.getState()).toMatchObject({ clearedAt: null, clearedBy: null });
    expect(cards()).toHaveLength(1);
  });

  it('지운 뒤에 생긴 이벤트만 다시 보인다', async () => {
    const queryClient = renderHost();
    fireEvent.click(clearButton());

    act(() =>
      applyEvents(queryClient, [
        item({
          event_id: 'e-3',
          world_ids: ['w-0030'],
          raised_at: T + 20_000,
          updated_at: T + 20_000,
        }),
      ]),
    );

    await waitFor(() => expect(cardNames()).toEqual(['w-0030 이벤트']));
    expect(clearButton()).toBeTruthy();
  });

  it('지운 뒤에 등급이 오른 이벤트는 새 단계로 다시 보인다', async () => {
    const queryClient = renderHost();
    fireEvent.click(clearButton());

    act(() =>
      applyEvents(queryClient, [
        item({
          event_id: 'e-2',
          event_sequence: 2,
          world_ids: ['w-0021'],
          lifecycle: 'UPDATED',
          severity: 'CRITICAL',
          updated_at: T + 20_000,
        }),
      ]),
    );

    await waitFor(() => expect(cardNames()).toEqual(['w-0021 이벤트']));
  });
});
