import { Since } from '@/features/events/Since';
import { useRealtimeStore } from '@/store/realtime.store';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T + 12_000);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('Since', () => {
  it('발생 뒤 경과를 보이고 매 초 늘어난다', () => {
    render(<Since raisedAt={T} />);
    expect(screen.getByText('12초 전')).toBeTruthy();

    act(() => void vi.advanceTimersByTime(1000));
    expect(screen.getByText('13초 전')).toBeTruthy();
  });

  it('여러 칸이 타이머 하나를 나눠 쓰고, 모두 사라지면 타이머도 멈춘다', () => {
    render(
      <>
        <Since raisedAt={T} />
        <Since raisedAt={T + 2000} />
        <Since raisedAt={T + 4000} />
      </>,
    );
    expect(vi.getTimerCount()).toBe(1);
    expect(screen.getByText('12초 전')).toBeTruthy();
    expect(screen.getByText('10초 전')).toBeTruthy();
    expect(screen.getByText('8초 전')).toBeTruthy();

    cleanup();
    expect(vi.getTimerCount()).toBe(0);
  });
});
