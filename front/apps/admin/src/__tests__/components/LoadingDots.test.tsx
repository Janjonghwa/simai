import { LOADING_DOT_STEP_MS, LoadingDots } from '@/components/LoadingDots';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 움직임 줄이기 설정을 흉내 낸다(matchMedia 가 없는 jsdom 에서는 움직이는 쪽이다).
function stubReducedMotion(matches: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)' && matches,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

const dotsOf = (container: HTMLElement) =>
  container.querySelector('[data-slot="loading-dots"]')?.textContent;

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('LoadingDots', () => {
  it('글은 그대로 두고 점만 . → .. → ... → . 로 되풀이하며, 점은 보조 기술에서 숨긴다', () => {
    const { container } = render(<LoadingDots label="TBM 생성 중" />);

    expect(screen.getByText('TBM 생성 중')).toBeTruthy();
    expect(container.querySelector('[data-slot="loading-dots"]')?.getAttribute('aria-hidden')).toBe(
      'true',
    );
    const seen = [dotsOf(container)];
    for (let i = 0; i < 3; i += 1) {
      act(() => {
        vi.advanceTimersByTime(LOADING_DOT_STEP_MS);
      });
      seen.push(dotsOf(container));
    }
    expect(seen).toEqual(['.', '..', '...', '.']);
  });

  it('움직임 줄이기 설정이면 점 세 개를 멈춰 둔 정지 글이다', () => {
    stubReducedMotion(true);
    const { container } = render(<LoadingDots label="업로드 중 12/28" />);

    expect(screen.getByText('업로드 중 12/28')).toBeTruthy();
    expect(dotsOf(container)).toBe('...');
    act(() => {
      vi.advanceTimersByTime(LOADING_DOT_STEP_MS * 5);
    });
    expect(dotsOf(container)).toBe('...');
    expect(vi.getTimerCount()).toBe(0);
  });
});
