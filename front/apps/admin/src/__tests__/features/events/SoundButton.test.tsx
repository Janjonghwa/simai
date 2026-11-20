import { SoundButton, soundLabel } from '@/features/events/SoundButton';
import type { AlarmSoundState } from '@/lib/alarmSound';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 소리 엔진은 따로 시험한다(__tests__/lib/alarmSound.test.ts). 여기서는 엔진 상태에 따른 버튼 모양과 누름 연결만 본다.
const fake = vi.hoisted(() => {
  const initial = { ready: true, muted: false, want: null } as const;
  let snapshot: AlarmSoundState = initial;
  const listeners = new Set<() => void>();
  return {
    canAck: true,
    toggle: vi.fn(),
    reset: () => {
      snapshot = initial;
    },
    get: () => snapshot,
    set(next: Partial<AlarmSoundState>) {
      snapshot = { ...snapshot, ...next };
      listeners.forEach((listener) => listener());
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
});

vi.mock('@/lib/alarmSound', () => ({
  alarmSound: {
    subscribe: fake.subscribe,
    getSnapshot: fake.get,
    toggle: fake.toggle,
  },
}));
vi.mock('@/features/events/ack', () => ({ useCanAck: () => fake.canAck }));

const button = () => screen.getByRole('button');
beforeEach(() => {
  fake.canAck = true;
  fake.reset();
});
afterEach(() => {
  cleanup();
  fake.toggle.mockClear();
});
describe('소리 단추', () => {
  it('상태를 툴팁과 접근 가능한 이름으로 알린다', () => {
    render(<SoundButton />);
    expect(soundLabel(false, false)).toBe('경보 소리 켜짐');
    expect(screen.getByRole('tooltip', { hidden: true }).textContent).toBe('경보 소리 켜짐');
    expect(button().querySelector('.lucide-volume-2')).toBeTruthy();
    act(() => fake.set({ ready: false }));
    expect(button().getAttribute('aria-label')).toBe('경보 소리 차단됨 · 눌러서 풀기');
    expect(button().querySelector('.lucide-volume-x')).toBeTruthy();
    act(() => fake.set({ muted: true }));
    expect(button().getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByRole('tooltip', { hidden: true }).textContent).toBe(
      '경보 소리 꺼짐 · 눌러서 켜기',
    );
    fireEvent.click(button());
    expect(fake.toggle).toHaveBeenCalledTimes(1);
  });
  it('확인 권한이 없으면 숨긴다', () => {
    fake.canAck = false;
    const { container } = render(<SoundButton />);
    expect(container.firstChild).toBeNull();
  });
});
