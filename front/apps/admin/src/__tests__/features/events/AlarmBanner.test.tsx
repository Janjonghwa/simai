import { AlarmBanner } from '@/features/events/AlarmBanner';
import { useEventClearStore } from '@/features/events/eventClear.store';
import type { EventEntry } from '@/features/events/events';
import { ZONES_KEY } from '@/features/zones/api';
import { MOCK_ZONES } from '@/mocks/zones';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

const live = vi.hoisted(() => ({ entries: [] as EventEntry[] }));

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: live.entries, loaded: true, failed: false }),
}));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

const alarm: EventEntry = {
  event: {
    event_id: 'e-1',
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
  },
  stageStartedAt: T,
};

function renderBanner(props: { withPanel?: boolean; holdOnExit?: boolean } = {}) {
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  const view = render(
    <QueryClientProvider client={client}>
      <AlarmBanner withPanel={props.withPanel ?? true} holdOnExit={props.holdOnExit} />
    </QueryClientProvider>,
  );
  return {
    banner: screen.getByRole('group', { name: '확인 대기 경보' }),
    rerender: (next: { withPanel?: boolean; holdOnExit?: boolean } = props) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <AlarmBanner withPanel={next.withPanel ?? true} holdOnExit={next.holdOnExit} />
        </QueryClientProvider>,
      ),
  };
}

beforeEach(() => {
  live.entries = [alarm];
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
});

describe('AlarmBanner', () => {
  it('지금 Tier 0 가 들고 있지 않은 지난 관제 경보는 배너에 없다(S15P31A101-595)', () => {
    live.entries = [
      { ...alarm, event: { ...alarm.event, held_by_tier0: false } },
      {
        ...alarm,
        event: { ...alarm.event, event_id: 'e-2', severity: 'HIGH', held_by_tier0: true },
      },
    ];
    const { banner } = renderBanner();
    expect(within(banner).getByText('주의')).toBeTruthy();
  });

  it('깜빡이는 테두리는 배너 전체를 덮지만 클릭을 가로채지 않는다(버튼이 눌린다)', () => {
    const { banner } = renderBanner();
    const blink = [...banner.querySelectorAll<HTMLElement>('[aria-hidden="true"]')].find((el) =>
      el.className.includes('animate-alarm-blink'),
    );
    expect(blink?.className).toContain('absolute');
    expect(blink?.className).toContain('pointer-events-none');
  });

  it.each([
    ['CRITICAL', 'ring-sev-critical', 'ring-sev-caution'],
    ['HIGH', 'ring-sev-caution', 'ring-sev-critical'],
  ] as const)('깜빡이는 테두리는 경보 등급 %s 의 색(%s)이다', (severity, ring, other) => {
    live.entries = [{ ...alarm, event: { ...alarm.event, severity } }];
    const { banner } = renderBanner();
    const blink = [...banner.querySelectorAll<HTMLElement>('[aria-hidden="true"]')].find((el) =>
      el.className.includes('animate-alarm-blink'),
    );
    expect(blink?.className).toContain(ring);
    expect(blink?.className).not.toContain(other);
  });

  it('큰 배지 · 사건(굵게)/사람 · 경과(1초마다 갱신) · 큰 확인 단추만 있고 상세·CCTV·외 N건은 없다', () => {
    vi.useFakeTimers();
    vi.setSystemTime(T + 12_000);
    const second: EventEntry = {
      stageStartedAt: T,
      event: {
        ...alarm.event,
        event_id: 'e-2',
        rule_id: 'R1',
        severity: 'HIGH',
        world_ids: ['w-0021'],
      },
    };
    live.entries = [second, alarm];
    const { banner } = renderBanner();

    // 큰 배지(size="large")
    const badge = within(banner).getByText('위험');
    expect(badge.className).toContain('h-7');

    // 사건(굵게)과 대상 두 줄
    const alert = within(banner).getByRole('alert');
    expect(within(alert).getByText('추락 위험(가장자리)').className).toContain('font-bold');
    const person = within(alert).getByText('작업자 #0015');
    expect(person.getAttribute('title')).toBe('w-0015');
    expect(person.className).not.toContain('font-mono');

    // 경과는 role="alert" 밖에 있고 1초마다 갱신된다.
    expect(within(banner).getByText('12초 전').closest('[role="alert"]')).toBeNull();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(within(banner).getByText('13초 전')).toBeTruthy();

    // 큰 확인 단추(size="default", h-8)만 남고 상세·CCTV·외 N건은 없다.
    const ackBtn = within(banner).getByRole('button', { name: /확인$/ });
    expect(ackBtn.className).toContain('h-8');
    expect(within(banner).queryByRole('button', { name: '상세' })).toBeNull();
    expect(within(banner).queryByRole('button', { name: /CCTV/ })).toBeNull();
    expect(within(banner).queryByText(/확인 대기 외/)).toBeNull();
    expect(within(banner).queryByText('+1')).toBeNull();
    expect(within(banner).queryByRole('button', { name: '소리 켜기' })).toBeNull();
  });

  it('사람이 없는 경보는 구역 이름이 대상이고, 이름을 모르는 구역은 `구역`이며 구역 번호는 보이지 않는다', () => {
    live.entries = [{ ...alarm, event: { ...alarm.event, world_ids: [] } }];
    const named = renderBanner().banner;
    expect(within(named).getByText('추락 위험(가장자리)')).toBeTruthy();
    expect(within(named).getByText('개구부 구역')).toBeTruthy();
    expect(within(named).getByRole('button', { name: /개구부 구역 확인/ })).toBeTruthy();
    cleanup();

    live.entries = [{ ...alarm, event: { ...alarm.event, world_ids: [], zone_id: 'z-99' } }];
    const unknown = renderBanner().banner;
    expect(within(unknown).getByText('구역')).toBeTruthy();
    cleanup();

    live.entries = [{ ...alarm, event: { ...alarm.event, zone_id: 'z-99' } }];
    const person = renderBanner().banner;
    expect(within(person).getByText('작업자 #0015')).toBeTruthy();
    expect(person.textContent).not.toMatch(/z-\d/);
  });

  it('해석된 사람 표시 이름이 있으면 배너와 확인 버튼 이름에 표시 이름을 쓴다(-658)', () => {
    useRealtimeStore.setState({
      health: {
        receivedAt: T,
        clockOffsetMs: 0,
        payload: {
          person_names: [
            { world_id: 'w-0015', display_name: '김철수', name_source: 'WATCH_DEVICE' },
          ],
        } as never,
      },
    });
    const { banner } = renderBanner();
    expect(within(banner).getByText('추락 위험(가장자리)')).toBeTruthy();
    expect(within(banner).getByText('김철수')).toBeTruthy();
    expect(
      within(banner).getByRole('button', { name: '추락 위험(가장자리) 김철수 (w-0015) 확인' }),
    ).toBeTruthy();
  });
});

describe('AlarmBanner 모두 지우기', () => {
  const bannerOrNull = () => screen.queryByRole('group', { name: '확인 대기 경보' });
  const second: EventEntry = {
    stageStartedAt: T + 5_000,
    event: {
      ...alarm.event,
      event_id: 'e-2',
      rule_id: 'R1',
      severity: 'HIGH',
      world_ids: ['w-0021'],
      raised_at: T + 5_000,
      updated_at: T + 5_000,
    },
  };

  it('지우면 배너가 바로 사라진다', () => {
    renderBanner();
    expect(bannerOrNull()).toBeTruthy();

    act(() => useEventClearStore.getState().clearAll(T));
    expect(bannerOrNull()).toBeNull();
  });

  it('holdOnExit 가 켜져 있으면 경보가 사라진 뒤 150ms 동안 내용을 유지하다 사라진다', () => {
    vi.useFakeTimers();
    renderBanner({ holdOnExit: true });
    expect(bannerOrNull()).toBeTruthy();

    act(() => useEventClearStore.getState().clearAll(T));
    expect(bannerOrNull()).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(bannerOrNull()).toBeNull();
  });

  it('이미 지운 경보는 새로 열어도 배너에 없다', () => {
    useEventClearStore.getState().clearAll(T);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AlarmBanner withPanel />
      </QueryClientProvider>,
    );
    expect(bannerOrNull()).toBeNull();
  });

  it('지운 뒤에 생긴 경보는 배너에 뜬다', () => {
    useEventClearStore.getState().clearAll(T);
    live.entries = [alarm, second];
    const { banner } = renderBanner();

    expect(within(banner).getByRole('alert').textContent).toContain('0021');
  });

  it('다른 사용자가 지운 기록은 적용하지 않아 경보가 그대로 뜬다', () => {
    useEventClearStore.getState().clearAll(T);
    useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'other', role: 'ADMIN' } });
    const { banner } = renderBanner();
    expect(within(banner).getByRole('alert').textContent).toContain('0015');
  });

  it('지운 뒤에 새 단계가 열린 경보는 다시 뜬다', () => {
    useEventClearStore.getState().clearAll(T);
    live.entries = [{ ...alarm, stageStartedAt: T + 5_000 }];
    const { banner } = renderBanner();
    expect(within(banner).getByRole('alert').textContent).toContain('0015');
  });
});
