import { FailureBand } from '@/features/layout/FailureBand';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { HEALTH_TIMEOUT_MS, useRealtimeStore } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** 브라우저 시각과 EC2 시각(as_of)을 같게 둔다. 마지막 world_state 는 12.4초 전인 14:29:52 다. */
const NOW = new Date(2026, 9, 23, 14, 30, 5).getTime();

function seed(change: Partial<ViewerHealthPayload>) {
  const receivedAt = Date.now();
  useRealtimeStore.setState({
    phase: 'OPEN',
    openedAt: receivedAt,
    health: {
      payload: { ...viewerHealthAt(NOW).payload, as_of: NOW, data_age_ms: 12_400, ...change },
      receivedAt,
      clockOffsetMs: 0,
    },
  });
}

const laptop = (link: 'CONNECTED' | 'DISCONNECTED') => ({ link, last_heartbeat_at: NOW });
const halted = { remote_state: 'CONTROL_HALTED', laptop: laptop('CONNECTED') } as const;
const band = () => screen.queryByRole('group', { name: '장애' });
const title = () => within(band()!).getByRole('alert').textContent;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('FailureBand', () => {
  it.each([
    ['CONTROL_HALTED', 'CONNECTED', '관제 정지'],
    ['CONTROL_HALTED', 'DISCONNECTED', '현장 장비 응답 없음'],
    ['REMOTE_LINK_LOST', 'CONNECTED', '원격 전달 단절 · 현장 경보 동작 중'],
  ] as const)(
    '%s · 노트북 %s 이면 「%s」 띠와 마지막 수신 시각·경과를 보인다',
    (remote, link, text) => {
      seed({ remote_state: remote, laptop: laptop(link), system_health: 'UNSAFE_UNKNOWN' });
      render(<FailureBand />);

      // 장애 종류만 읽는다(경과 시간은 1초마다 바뀌어 읽지 않는다).
      expect(title()).toBe(text);
      expect(band()!.textContent).toContain('마지막 수신 14:29:52 · 12초 경과');
    },
  );

  it('경과 시간은 시간이 지나면 늘어나고, 장애 종류가 바뀌면 문구도 바뀐다', () => {
    seed(halted);
    render(<FailureBand />);

    act(() => vi.advanceTimersByTime(2_000));
    expect(band()!.textContent).toContain('14초 경과');

    act(() => seed({ remote_state: 'CONTROL_HALTED', laptop: laptop('DISCONNECTED') }));
    expect(title()).toBe('현장 장비 응답 없음');
  });

  it('EC2 와 끊기면(health 가 정한 시간 넘게 안 오면) 띠를 거두고, 다시 오면 보인다', () => {
    seed(halted);
    render(<FailureBand />);
    expect(band()).toBeTruthy();

    act(() => vi.advanceTimersByTime(HEALTH_TIMEOUT_MS + 600));
    expect(band()).toBeNull();

    act(() => seed(halted));
    expect(title()).toBe('관제 정지');
  });

  it('장애 띠는 줄무늬로만 보이고 위험 등급 색·지연 색을 쓰지 않는다', () => {
    seed(halted);
    render(<FailureBand />);

    expect(band()!.className).toContain('bg-stripes');
    expect(band()!.outerHTML).not.toMatch(/sev-|text-delay|bg-delay/);
  });

  it('정상이면 띠가 없다', () => {
    seed({ remote_state: 'NORMAL' });
    render(<FailureBand />);
    expect(band()).toBeNull();
  });

  it('데이터를 받은 적 없으면 장애 문구만 보인다', () => {
    seed({ remote_state: 'CONTROL_HALTED', laptop: laptop('DISCONNECTED'), data_age_ms: null });
    render(<FailureBand />);
    expect(band()!.textContent).toBe('현장 장비 응답 없음');
  });
});
