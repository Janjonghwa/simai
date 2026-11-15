import { TestInputBadge } from '@/features/monitor/TestInputBadge';
import { useRealtimeStore } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

// S15P31A101-731: 녹화 파일 재생(시험 입력) 중 표시. 근거는 viewer_health 1.6.0 laptop.test_input 이다.

/** 계약 예시 viewer_health.test_input 과 같은 모양(laptop.test_input 만 바꾼다, 없으면 이전 EC2). */
const payload = (testInput: boolean | undefined): ViewerHealthPayload => ({
  as_of: 1_760_342_400_990,
  system_health: 'NOMINAL',
  remote_state: 'NORMAL',
  tier0_link: 'CONNECTED',
  laptop: {
    link: 'CONNECTED',
    last_heartbeat_at: 1_760_342_400_800,
    ...(testInput === undefined ? {} : { test_input: testInput }),
  },
  data_age_ms: 210,
  calibration_state: 'CALIBRATED',
  config: { published_version: 'cfg-12', applied_version: 'cfg-12', sync: 'CURRENT' },
  scene: { committed_version: 'scene-3', live_version: 'scene-3', mismatch: false },
  tier0_health: null,
});

const setHealth = (testInput: boolean | undefined) =>
  act(() =>
    useRealtimeStore.setState({
      health: { payload: payload(testInput), receivedAt: 0, clockOffsetMs: 0 },
    }),
  );

afterEach(() => {
  cleanup();
  useRealtimeStore.setState({ health: null });
});

describe('TestInputBadge', () => {
  it('재생 중이면 「녹화 재생 중 · 실제 알림 꺼짐」과 설명을 보인다', () => {
    setHealth(true);
    render(<TestInputBadge />);
    const badge = screen.getByRole('status');
    expect(badge.textContent).toBe('녹화 재생 중 · 실제 알림 꺼짐');
    expect(badge.getAttribute('title')).toContain('현장 경보 소리·워치 진동은 보내지 않습니다');
  });

  it('실제 카메라이거나 모르면(이전 EC2·health 없음) 그리지 않는다', () => {
    setHealth(false);
    const view = render(<TestInputBadge />);
    expect(screen.queryByRole('status')).toBeNull();
    setHealth(undefined);
    view.rerender(<TestInputBadge />);
    expect(screen.queryByRole('status')).toBeNull();
    act(() => useRealtimeStore.setState({ health: null }));
    expect(screen.queryByRole('status')).toBeNull();
  });
});
