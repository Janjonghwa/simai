import { useCamSplitStore } from '@/features/cctv/camSplit.store';
import { groupIncidents, mergeEvent, type EventEntry } from '@/features/events/events';
import { IncidentCard } from '@/features/events/IncidentCard';
import type { EventItem } from '@/features/events/types';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { WorldStatePayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

const navigate = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
}));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

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

const names: Record<string, string> = { 'z-04': '개구부', 'z-02': '단상' };

function renderCard(
  entries: EventEntry[],
  props: Partial<React.ComponentProps<typeof IncidentCard>> = {},
) {
  const incident = groupIncidents(entries)[0]!;
  const onSelect = vi.fn();
  const onOpen = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ul>
        <IncidentCard
          incident={incident}
          selected={false}
          zoneName={(id) => names[id]}
          onSelect={onSelect}
          onOpen={onOpen}
          {...props}
        />
      </ul>
    </QueryClientProvider>,
  );
  return { card: screen.getByRole('article'), onSelect, onOpen, incident };
}

beforeEach(() => {
  // 경과는 카드 안의 칸이 시계로 그린다. 발생(T) 12초 뒤로 맞춘다.
  vi.useFakeTimers();
  vi.setSystemTime(T + 12_000);
  navigate.mockReset();
  useCamSplitStore.getState().close();
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('IncidentCard', () => {
  it('머리줄은 등급 배지·대표 규칙 이름·미확인 칩이고, 둘째 줄은 대상·구역·경과다', () => {
    const { card } = renderCard([entry()]);

    expect(within(card).getByText('위험')).toBeTruthy();
    expect(within(card).getByText('개구부·가장자리 근접')).toBeTruthy();
    const chip = card.querySelector('[data-chip]');
    expect(chip?.getAttribute('data-chip')).toBe('unacked');
    expect(chip?.textContent).toBe('미확인');
    expect(chip?.className).toContain('border-sev-critical');
    expect(within(card).getByText('w-0015 · z-04 개구부')).toBeTruthy();
    expect(within(card).getByText('12초 전')).toBeTruthy();
  });

  it('개구부 수치와 나머지 규칙 줄은 카드에 없다(상세에만 있다)', () => {
    const { card } = renderCard([
      entry(),
      entry({ event_id: 'e-2', rule_id: 'R1', severity: 'HIGH', zone_id: 'z-02' }),
    ]);

    expect(card.textContent).not.toContain('0.82');
    expect(card.textContent).not.toContain('높이 차');
    // 대표 규칙(가장 높은 등급)만 이름으로 보이고 나머지 규칙 이름은 없다.
    expect(within(card).queryByText('위험 구역 진입')).toBeNull();
    // 구역은 첫 구역만 이름까지, 나머지는 번호만.
    expect(within(card).getByText('w-0015 · z-04 개구부 · z-02')).toBeTruthy();
    expect(card.querySelectorAll('li')).toHaveLength(0);
  });

  it('진입 예측만 셋째 줄에 N초 뒤 진입 예상을 보인다', () => {
    const { card } = renderCard([
      entry({
        rule_id: 'R5',
        severity: 'CAUTION',
        metrics: { time_to_entry_s: 1.6 },
        zone_id: 'z-02',
      }),
    ]);

    expect(within(card).getByText('1.6초 뒤 진입 예상')).toBeTruthy();
    // 주의 등급은 확인할 경보가 없어 처리 칩이 없다.
    expect(card.querySelector('[data-chip]')).toBeNull();
  });

  it('확인하면 확인 시각 칩이고, 불일치가 있으면 불일치가 앞선다', () => {
    const acked = entry({
      acks: [
        {
          ack_id: 'a-1',
          channel: 'CONTROL_ROOM',
          actor_role: 'ADMIN',
          actor_id: 'admin',
          acked_at: T + 3_000,
        },
      ],
      updated_at: T + 3_000,
    });
    renderCard([acked]);
    expect(screen.getByRole('article').querySelector('[data-chip]')?.textContent).toMatch(
      /^확인 \d{2}:\d{2}:\d{2}$/,
    );
    cleanup();

    renderCard([acked], {
      mismatches: {
        'e-1': { ticketId: 't', ticketStatus: 'DONE', eventSequence: 1, detectedAt: T },
      },
    });
    expect(screen.getByRole('article').querySelector('[data-chip]')?.textContent).toBe('불일치');
  });

  it('해소된 묶음은 해소 시각 칩만 있고 경과는 없다', () => {
    const { card } = renderCard([
      entry({ lifecycle: 'RESOLVED', resolved_at: T + 5_000, updated_at: T + 5_000 }),
    ]);

    expect(card.querySelector('[data-chip]')?.textContent).toMatch(/^해소 \d{2}:\d{2}:\d{2}$/);
    expect(card.textContent).not.toContain('초 전');
  });

  it('복원 이벤트는 확인 필요와 첫 이유를 꼬리표로 보인다', () => {
    const { card } = renderCard([
      entry({
        verification: 'UNVERIFIABLE',
        needs_check: true,
        confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
      }),
    ]);

    // 카드 꼬리표는 "확인 필요"만 적고 이유는 올려 보면 나온다.
    expect(within(card).getByText('확인 필요').getAttribute('title')).toBe('확인 필요 · 높이 모름');
    expect(card.textContent).not.toContain('높이 모름');
    expect(card.textContent).not.toContain('예측 위치');
  });

  it('지연 알림 꼬리표를 보인다', () => {
    const { card } = renderCard([entry({ delivery: 'DELAYED_ACTIVE' })]);
    expect(within(card).getByText('지연 알림')).toBeTruthy();
  });

  it('머리줄: 처음 누르면 선택, 이미 선택된 카드를 다시 누르면 상세를 연다', () => {
    const first = renderCard([entry()]);
    fireEvent.click(within(first.card).getByRole('button', { pressed: false }));
    expect(first.onSelect).toHaveBeenCalledWith(first.incident);
    expect(first.onOpen).not.toHaveBeenCalled();
    cleanup();

    const second = renderCard([entry()], { selected: true });
    fireEvent.click(within(second.card).getByRole('button', { pressed: true }));
    expect(second.onOpen).toHaveBeenCalledWith('e-1');
    expect(second.onSelect).not.toHaveBeenCalled();
  });

  it('선택되면 카드가 보이는 자리로 스크롤한다', () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    renderCard([entry()], { selected: true });
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  it('미확인 경보는 왼쪽 가장자리만 깜빡이고, 움직임 줄이기 설정이면 멈춘다', () => {
    const { card } = renderCard([entry()]);
    const blink = [...card.querySelectorAll<HTMLElement>('[aria-hidden="true"]')].filter((el) =>
      el.className.includes('animate-alarm-blink'),
    );
    expect(blink).toHaveLength(1);
    expect(blink[0]!.className).toContain('motion-safe:animate-alarm-blink');
    expect(blink[0]!.className).toContain('w-1');
  });

  it('등급마다 띠 색이 다르고, 깜빡이는 것은 미확인 경보뿐이다', () => {
    const barOf = (card: HTMLElement) =>
      card.querySelector<HTMLElement>('[aria-hidden="true"].w-1')!.className;

    const caution = renderCard([entry({ severity: 'CAUTION' })]);
    expect(barOf(caution.card)).toContain('bg-sev-caution');
    expect(barOf(caution.card)).not.toContain('animate-alarm-blink');
    cleanup();

    const info = renderCard([entry({ severity: 'INFO' })]);
    expect(barOf(info.card)).toContain('bg-faint');
  });

  it('사람을 담은 카메라가 있으면 카메라 버튼이 오른쪽 카메라 미리보기를 연다', () => {
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [{ world_id: 'w-0015', cameras: ['cam2', 'cam1'] }],
        } as unknown as WorldStatePayload,
      },
    });
    const { card } = renderCard([entry()]);

    const camera = within(card).getByRole('button', { name: /cam2로 확인/ });
    fireEvent.click(camera);
    // CCTV 화면으로 옮기지 않고 실시간 관제 오른쪽 칸을 연다.
    expect(navigate).not.toHaveBeenCalled();
    expect(useCamSplitStore.getState().split).toEqual({
      cam: 'cam2',
      target: { type: 'person', id: 'w-0015' },
    });
  });

  it('담은 카메라가 없으면 카메라 버튼이 없다', () => {
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [{ world_id: 'w-0015', cameras: [] }],
        } as unknown as WorldStatePayload,
      },
    });
    const { card } = renderCard([entry()]);
    expect(within(card).queryByRole('button', { name: /로 확인/ })).toBeNull();
  });
});
