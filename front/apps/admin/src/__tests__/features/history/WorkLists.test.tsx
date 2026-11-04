import { EventLog } from '@/features/history/WorkLists';
import type { EventRow } from '@/features/history/replayIndex';
import { ZONES_KEY } from '@/features/zones/api';
import { MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const row = (change: Partial<EventRow>): EventRow => ({
  eventId: 'e-1',
  ruleId: 'R2',
  severity: 'HIGH',
  worldIds: [],
  zoneId: null,
  raisedAt: 1_760_342_400_000,
  resolvedAt: null,
  ...change,
});

function renderLog(events: EventRow[]) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  render(
    <QueryClientProvider client={client}>
      <EventLog events={events} playhead={null} onPick={vi.fn()} />
    </QueryClientProvider>,
  );
  const button = within(screen.getByRole('list', { name: '이벤트 로그' })).getByRole('button');
  return button.lastElementChild!.textContent;
}

afterEach(cleanup);

describe('이벤트 로그의 대상', () => {
  it('작업자 뒤에 구역 이름을 붙이고, 이름을 모르는 구역은 뺀다(구역 번호는 보이지 않는다)', () => {
    expect(renderLog([row({ worldIds: ['w-0015'], zoneId: 'z-04' })])).toBe('w-0015 · 개구부 구역');
    cleanup();

    expect(renderLog([row({ worldIds: ['w-0015'], zoneId: 'z-99' })])).toBe('w-0015');
  });

  it('작업자가 없으면 구역이 대상이라 이름을 모르면 `구역` 이다', () => {
    expect(renderLog([row({ zoneId: 'z-04' })])).toBe('개구부 구역');
    cleanup();

    expect(renderLog([row({ zoneId: 'z-99' })])).toBe('구역');
  });
});
