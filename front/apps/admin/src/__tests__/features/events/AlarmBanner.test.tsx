import { AlarmBanner } from '@/features/events/AlarmBanner';
import { useEventClearStore } from '@/features/events/eventClear.store';
import type { EventEntry } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { WorldStatePayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

const live = vi.hoisted(() => ({ entries: [] as EventEntry[] }));
const navigate = vi.hoisted(() => vi.fn());

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: live.entries, loaded: true, failed: false }),
}));
vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
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

function renderBanner(withPanel: boolean) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AlarmBanner withPanel={withPanel} />
    </QueryClientProvider>,
  );
  return screen.getByRole('group', { name: '확인 대기 경보' });
}

beforeEach(() => {
  live.entries = [alarm];
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
  navigate.mockReset();
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useEventsStore.setState(useEventsStore.getInitialState(), true);
  useEventClearStore.setState({ clearedAt: null, clearedBy: null });
});

describe('AlarmBanner', () => {
  it('깜빡이는 테두리는 배너 전체를 덮지만 클릭을 가로채지 않는다(버튼이 눌린다)', () => {
    const banner = renderBanner(true);
    const blink = [...banner.querySelectorAll<HTMLElement>('[aria-hidden="true"]')].find((el) =>
      el.className.includes('animate-alarm-blink'),
    );
    expect(blink?.className).toContain('absolute');
    expect(blink?.className).toContain('pointer-events-none');
  });

  it('상세를 누르면 그 이벤트를 열고, 이벤트 패널이 없는 화면이면 실시간 관제로 간다', () => {
    const banner = renderBanner(false);
    fireEvent.click(within(banner).getByRole('button', { name: '상세' }));

    expect(useEventsStore.getState().openEventId).toBe('e-1');
    expect(navigate).toHaveBeenCalledWith({ to: '/' });
  });

  it('이벤트 패널이 있는 화면에서는 그 자리에서 상세를 연다', () => {
    const banner = renderBanner(true);
    fireEvent.click(within(banner).getByRole('button', { name: '상세' }));

    expect(useEventsStore.getState().openEventId).toBe('e-1');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('등급 배지, 규칙 · 대상 · 구역, 발생 뒤 경과를 이벤트 카드와 같은 규칙으로 보인다', () => {
    vi.useFakeTimers();
    vi.setSystemTime(T + 12_000);
    const banner = renderBanner(true);

    expect(within(banner).getByText('위험')).toBeTruthy();
    expect(within(banner).getByRole('alert').textContent).toBe(
      '개구부·가장자리 근접 · w-0015 · z-04',
    );
    // 경과는 낭독 영역(role=alert) 밖에 둔다.
    expect(within(banner).getByText('12초 전').closest('[role="alert"]')).toBeNull();
    // 개구부 수치 같은 근거 줄은 배너에 없다.
    expect(banner.textContent).not.toContain('가장자리 0');
  });

  it('확인 대기 경보가 더 있으면 +N 을 보이고 가장 높은 등급이 먼저다', () => {
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
    const banner = renderBanner(true);

    expect(within(banner).getByText('+1')).toBeTruthy();
    expect(within(banner).getByText('외 1건').className).toContain('sr-only');
    expect(within(banner).getByRole('alert').textContent).toContain('w-0015');
  });

  it('확인 버튼은 확인 대기 경보에만 있고, 확인을 누를 수 있다', () => {
    const banner = renderBanner(true);
    expect(within(banner).getByRole('button', { name: /확인$/ })).toBeTruthy();
    expect(within(banner).queryByRole('button', { name: '소리 켜기' })).toBeNull();
  });

  it('카메라 버튼은 이벤트 패널이 있는 화면에서 사람을 담은 카메라가 있을 때만 있다', () => {
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [{ world_id: 'w-0015', cameras: ['cam3'] }],
        } as unknown as WorldStatePayload,
      },
    });
    const withPanel = renderBanner(true);
    fireEvent.click(within(withPanel).getByRole('button', { name: /cam3로 확인/ }));
    expect(navigate).toHaveBeenCalledWith({ to: '/cctv' });
    cleanup();

    const without = renderBanner(false);
    expect(within(without).queryByRole('button', { name: /로 확인/ })).toBeNull();
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
    renderBanner(true);
    expect(bannerOrNull()).toBeTruthy();

    act(() => useEventClearStore.getState().clearAll(T));
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

  it('지운 뒤에 생긴 경보는 배너에 뜨고, 지운 경보는 +N 에 세지 않는다', () => {
    useEventClearStore.getState().clearAll(T);
    live.entries = [alarm, second];
    const banner = renderBanner(true);

    expect(within(banner).getByRole('alert').textContent).toContain('w-0021');
    expect(within(banner).queryByText('+1')).toBeNull();
  });

  it('다른 사용자가 지운 기록은 적용하지 않아 경보가 그대로 뜬다', () => {
    useEventClearStore.getState().clearAll(T);
    useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'other', role: 'ADMIN' } });
    const banner = renderBanner(true);
    expect(within(banner).getByRole('alert').textContent).toContain('w-0015');
  });

  it('지운 뒤에 새 단계가 열린 경보는 다시 뜬다', () => {
    useEventClearStore.getState().clearAll(T);
    live.entries = [{ ...alarm, stageStartedAt: T + 5_000 }];
    const banner = renderBanner(true);
    expect(within(banner).getByRole('alert').textContent).toContain('w-0015');
  });
});
