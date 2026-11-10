import { LoginHero, MASCOT_MEDIA } from '@/features/auth/LoginHero';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 움직임 줄이기 설정을 흉내 낸다. change 를 보내면 useMediaQuery 가 다시 읽는다.
function stubReducedMotion(initial: boolean) {
  let matches = initial;
  const listeners = new Set<() => void>();
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      get matches() {
        return query === '(prefers-reduced-motion: reduce)' && matches;
      },
      media: query,
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    })),
  );
  return (next: boolean) => {
    matches = next;
    listeners.forEach((fn) => fn());
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('LoginHero', () => {
  it('평소에는 마스코트 영상을 소리 없이 되풀이해 틀고, 장식이라 보조 기술에서 숨긴다', () => {
    stubReducedMotion(false);
    const { container } = render(<LoginHero />);

    const video = container.querySelector('video');
    expect(video).not.toBeNull();
    expect(video?.autoplay).toBe(true);
    expect(video?.loop).toBe(true);
    expect(video?.muted).toBe(true);
    expect(video?.getAttribute('playsinline')).not.toBeNull();
    expect(video?.getAttribute('aria-hidden')).toBe('true');
    expect(video?.getAttribute('poster')).toBe(MASCOT_MEDIA.poster);
    expect([...container.querySelectorAll('source')].map((s) => s.getAttribute('src'))).toEqual([
      MASCOT_MEDIA.webm,
      MASCOT_MEDIA.mp4,
    ]);
    expect(screen.queryByTestId('login-hero-poster')).toBeNull();
  });

  it('움직임 줄이기 설정이면 영상 없이 첫 장면만 보이고, 설정을 바꾸면 따라간다', () => {
    const setReduced = stubReducedMotion(true);
    const { container } = render(<LoginHero />);

    expect(container.querySelector('video')).toBeNull();
    expect(screen.getByTestId('login-hero-poster').getAttribute('src')).toBe(MASCOT_MEDIA.poster);

    act(() => setReduced(false));
    expect(container.querySelector('video')).not.toBeNull();
    expect(screen.queryByTestId('login-hero-poster')).toBeNull();
  });

  it('기능 세 줄을 목록으로 보인다', () => {
    stubReducedMotion(false);
    render(<LoginHero />);

    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.getByText('실시간 위험 판정')).toBeTruthy();
  });

  it('글자는 위 칸, 영상은 그 아래 칸에 따로 두고 영상을 키워 자르지 않고 맞춘다(글자가 마스코트를 덮지 않는다)', () => {
    stubReducedMotion(false);
    const { container } = render(<LoginHero />);

    const list = screen.getByRole('list');
    const media = screen.getByTestId('login-hero-media');
    const video = container.querySelector('video');
    expect(media.contains(video)).toBe(true);
    expect(media.contains(list)).toBe(false);
    expect(list.compareDocumentPosition(media) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(video?.classList.contains('object-contain')).toBe(true);
    expect(video?.classList.contains('object-cover')).toBe(false);
  });
});
