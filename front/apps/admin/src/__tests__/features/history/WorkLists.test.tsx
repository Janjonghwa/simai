import { EventLog } from '@/features/history/WorkLists';
import type { EventRow } from '@/features/history/replayIndex';
import type { HistoricalPersonNameSpan } from '@/features/people/personDisplay';
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

function renderLog(events: EventRow[], personNames?: readonly HistoricalPersonNameSpan[]) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  render(
    <QueryClientProvider client={client}>
      <EventLog events={events} playhead={null} personNames={personNames} onPick={vi.fn()} />
    </QueryClientProvider>,
  );
  const button = within(screen.getByRole('list', { name: '이벤트 로그' })).getByRole('button');
  return button.lastElementChild!.textContent;
}

afterEach(cleanup);

const T = 1_760_342_400_000;

function renderRows(events: EventRow[], personNames?: readonly HistoricalPersonNameSpan[]) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  render(
    <QueryClientProvider client={client}>
      <EventLog events={events} playhead={null} personNames={personNames} onPick={vi.fn()} />
    </QueryClientProvider>,
  );
  return within(screen.getByRole('list', { name: '이벤트 로그' })).queryAllByRole('button');
}

describe('이벤트 로그의 묶음(S15P31A101-751)', () => {
  it('같은 초의 여러 알림은 별도 줄로 보이고 원래 ID 는 hover 로 보인다', () => {
    const buttons = renderRows([
      row({
        eventId: 'a',
        ruleId: 'R7',
        worldIds: ['w-mwhubgp0-0010'],
        zoneId: 'z-04',
        raisedAt: T,
      }),
      row({
        eventId: 'b',
        ruleId: 'R7',
        worldIds: ['w-mwhubgp0-0011'],
        zoneId: 'z-04',
        raisedAt: T,
      }),
      row({
        eventId: 'c',
        ruleId: 'R7',
        worldIds: ['w-mwhubgp0-0012'],
        zoneId: 'z-04',
        raisedAt: T,
      }),
      row({
        eventId: 'd',
        ruleId: 'R7',
        worldIds: ['w-mwhubgp0-0013'],
        zoneId: 'z-04',
        raisedAt: T,
      }),
    ]);
    expect(buttons).toHaveLength(4);
    expect(buttons[0]!.textContent).not.toContain('×4');
    expect(buttons[0]!.textContent).toContain('작업자 #0010');
    expect(buttons[0]!.textContent).not.toContain('w-mwhubgp0');
    expect(buttons[0]!.querySelector('[title*="w-mwhubgp0-0010"]')).not.toBeNull();
    expect(screen.getByRole('heading', { name: '이벤트 로그 · 4건' })).toBeTruthy();
  });

  it('위험이나 구역이 다르거나 간격이 길면 따로 둔다', () => {
    const buttons = renderRows([
      row({ eventId: 'a', ruleId: 'R7', worldIds: ['w-0001'], zoneId: 'z-04', raisedAt: T }),
      row({ eventId: 'b', ruleId: 'R2', worldIds: ['w-0002'], zoneId: 'z-04', raisedAt: T }),
      row({ eventId: 'c', ruleId: 'R7', worldIds: ['w-0003'], zoneId: 'z-05', raisedAt: T }),
      row({
        eventId: 'd',
        ruleId: 'R7',
        worldIds: ['w-0004'],
        zoneId: 'z-04',
        raisedAt: T + 600_000,
      }),
    ]);
    expect(buttons).toHaveLength(4);
  });
});

describe('이벤트 로그의 대상', () => {
  it('작업자가 있으면 작업자만 보이고 구역 이름은 붙이지 않는다(-853, 구역 번호도 보이지 않는다)', () => {
    expect(renderLog([row({ worldIds: ['w-0015'], zoneId: 'z-04' })])).toBe('작업자 #0015');
    cleanup();

    expect(renderLog([row({ worldIds: ['w-0015'], zoneId: 'z-99' })])).toBe('작업자 #0015');
  });

  it('작업자가 없으면 구역이 대상이라 이름을 모르면 `구역` 이다', () => {
    expect(renderLog([row({ zoneId: 'z-04' })])).toBe('개구부 구역');
    cleanup();

    expect(renderLog([row({ zoneId: 'z-99' })])).toBe('구역');
  });

  it('그 시각 표시 이름이 있으면 이름으로 보이고, 외 n명과 hover World ID 는 유지하며, 이름이 없으면 작업자 #NNNN 으로 돌아간다(S15P31A101-835)', () => {
    const spans: HistoricalPersonNameSpan[] = [
      {
        world_id: 'w-0012',
        display_name: '추창만',
        name_source: 'WATCH_DEVICE',
        from_ms: T - 10_000,
        to_ms: T + 10_000,
      },
      {
        world_id: 'w-0012',
        display_name: '추반장',
        name_source: 'WORLD_MANUAL',
        from_ms: T + 10_000,
        to_ms: T + 60_000,
      },
    ];
    const buttons = renderRows(
      [
        row({
          eventId: 'e1',
          ruleId: 'R9',
          worldIds: ['w-0012', 'w-0015'],
          zoneId: 'z-04',
          raisedAt: T,
        }),
        row({
          eventId: 'e2',
          ruleId: 'R4',
          worldIds: ['w-0012'],
          zoneId: null,
          raisedAt: T + 20_000,
        }),
        row({
          eventId: 'e3',
          ruleId: 'R1',
          worldIds: ['w-0021'],
          zoneId: 'z-04',
          raisedAt: T + 30_000,
        }),
      ],
      spans,
    );
    expect(buttons[0]!.lastElementChild!.textContent).toBe('추창만 외 1명');
    expect(buttons[0]!.querySelector('[title="w-0012 · w-0015"]')).not.toBeNull();
    expect(buttons[1]!.lastElementChild!.textContent).toBe('추반장');
    expect(buttons[1]!.querySelector('[title="w-0012"]')).not.toBeNull();
    expect(buttons[2]!.lastElementChild!.textContent).toBe('작업자 #0021');
    expect(buttons[2]!.querySelector('[title="w-0021"]')).not.toBeNull();
  });
});
