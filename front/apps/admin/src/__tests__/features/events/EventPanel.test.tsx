import { EventDetail } from '@/features/events/EventDetail';
import { EventPanel } from '@/features/events/EventPanel';
import { groupIncidents, mergeEvent, type EventEntry } from '@/features/events/events';
import { MISMATCH_KEY } from '@/features/events/mismatch';
import type { EventItem } from '@/features/events/types';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => ({
    zones: [{ zoneId: 'z-04', name: '개구부' }],
    raw: undefined,
    failed: false,
  }),
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
    metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 },
    rationale: null,
    proposed_severity: null,
    raised_at: T,
    updated_at: T,
    ...change,
  };
}

const entry = (change: Partial<EventItem> = {}): EventEntry => mergeEvent(undefined, item(change));

function panelProps(
  entries: EventEntry[],
  props: Partial<ComponentProps<typeof EventPanel>> = {},
): ComponentProps<typeof EventPanel> {
  return {
    entries,
    loaded: true,
    failed: false,
    selectedKey: null,
    onSelect: vi.fn(),
    openEventId: null,
    onOpen: vi.fn(),
    ...props,
  };
}

function renderPanel(
  entries: EventEntry[],
  props: Partial<ComponentProps<typeof EventPanel>> = {},
) {
  const queryClient = new QueryClient();
  const tree = (next: ComponentProps<typeof EventPanel>) => (
    <QueryClientProvider client={queryClient}>
      <EventPanel {...next} />
    </QueryClientProvider>
  );
  const view = render(tree(panelProps(entries, props)));
  return {
    rerender: (
      nextEntries: EventEntry[],
      nextProps: Partial<ComponentProps<typeof EventPanel>> = {},
    ) => view.rerender(tree(panelProps(nextEntries, nextProps))),
  };
}

const fold = () => screen.getByRole('button', { name: /이벤트 (접기|펼치기)/ });

beforeEach(() => {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});

describe('EventPanel 접기', () => {
  it('이벤트가 하나도 없으면 저절로 접히고, 이벤트가 생기면 펼쳐진다', () => {
    const view = renderPanel([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('활성 이벤트 없음')).toBeNull();

    view.rerender([entry()]);
    expect(fold().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('article', { name: 'w-0015 이벤트' })).toBeTruthy();

    // 다시 0개가 되면 접힌다.
    view.rerender([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
  });

  it('진행 중 이벤트가 없어도 해소된 묶음이 남아 있으면 접지 않는다', () => {
    renderPanel([entry({ lifecycle: 'RESOLVED', resolved_at: T + 1_000 })]);
    expect(fold().getAttribute('aria-expanded')).toBe('true');
  });

  it('목록을 받는 중이거나 못 받았으면 접지 않는다(실패 문구를 가리지 않는다)', () => {
    renderPanel([], { loaded: false, failed: true });
    expect(fold().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('이벤트 목록을 받지 못했습니다');
  });

  it('사용자가 누른 접기·펼치기는 이벤트 수가 0 이 되거나 생기기 전까지 따른다', () => {
    const view = renderPanel([entry()]);
    fireEvent.click(fold());
    expect(fold().getAttribute('aria-expanded')).toBe('false');

    // 같은 상태에서 이벤트만 바뀌어도 사용자가 접은 채로 둔다.
    view.rerender([entry(), entry({ event_id: 'e-2', world_ids: ['w-0021'] })]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(fold());
    expect(fold().getAttribute('aria-expanded')).toBe('true');

    // 비었는데 사용자가 펼쳐 볼 수도 있다.
    view.rerender([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(fold());
    expect(screen.getByText('활성 이벤트 없음')).toBeTruthy();
  });

  it('카드에 구역 이름과 경과를 넘긴다', () => {
    renderPanel([entry()]);
    const card = screen.getByRole('article', { name: 'w-0015 이벤트' });
    expect(within(card).getByText('w-0015 · z-04 개구부')).toBeTruthy();
    expect(card.textContent).toMatch(/초 전|분/);
  });
});

describe('EventDetail 불일치·확인 필요', () => {
  function renderDetail(entries: EventEntry[], mismatchOf?: string) {
    const queryClient = new QueryClient();
    if (mismatchOf) {
      queryClient.setQueryData(MISMATCH_KEY, {
        [mismatchOf]: { ticketId: 't-1', ticketStatus: 'DONE', eventSequence: 1, detectedAt: T },
      });
    }
    const incident = groupIncidents(entries)[0]!;
    render(
      <QueryClientProvider client={queryClient}>
        <EventDetail incident={incident} openEventId="e-1" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    return screen.getByRole('dialog', { name: '사건 상세' });
  }

  it('조치 완료인데 관측상 활성이면 불일치를 머리에 보인다', () => {
    const dialog = renderDetail([entry()], 'e-1');
    expect(within(dialog).getByText('불일치 · 조치 완료, 관측상 위험 지속')).toBeTruthy();
  });

  it('해소된 규칙의 불일치는 보이지 않는다', () => {
    const dialog = renderDetail([entry({ lifecycle: 'RESOLVED', resolved_at: T + 1_000 })], 'e-1');
    expect(within(dialog).queryByText(/불일치/)).toBeNull();
  });

  it('복원 이벤트는 확인 필요와 첫 이유를 머리에, 전체 이유를 규칙 줄에 보인다', () => {
    const dialog = renderDetail([
      entry({
        verification: 'UNVERIFIABLE',
        needs_check: true,
        confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
      }),
    ]);
    expect(within(dialog).getByText('확인 필요 · 높이 모름')).toBeTruthy();
    expect(within(dialog).getByText('확인 필요 · 높이 모름, 예측 위치')).toBeTruthy();
    expect(within(dialog).getByText('해소 확인 불가')).toBeTruthy();
  });

  it('미확인 경보는 작은 외곽선 표시만 깜빡이고 움직임 줄이기 설정이면 멈춘다', () => {
    const dialog = renderDetail([entry()]);
    const marker = within(dialog).getByText('미확인');
    const blink = marker.querySelector('[aria-hidden="true"]');
    expect(blink?.className).toContain('motion-safe:animate-alarm-blink');
    expect(blink?.className).toContain('border');
  });

  it('확인한 사건에는 미확인 표시가 없다', () => {
    const dialog = renderDetail([
      entry({
        acks: [
          {
            ack_id: 'a-1',
            channel: 'CONTROL_ROOM',
            actor_role: 'ADMIN',
            actor_id: 'admin',
            acked_at: T + 1_000,
          },
        ],
        updated_at: T + 1_000,
      }),
    ]);
    expect(within(dialog).queryByText('미확인')).toBeNull();
  });
});
