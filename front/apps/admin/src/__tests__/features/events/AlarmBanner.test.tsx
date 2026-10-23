import { AlarmBanner } from '@/features/events/AlarmBanner';
import type { EventEntry } from '@/features/events/events';
import { useEventsStore } from '@/features/events/events.store';
import { useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
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
  navigate.mockReset();
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useEventsStore.setState(useEventsStore.getInitialState(), true);
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
});
