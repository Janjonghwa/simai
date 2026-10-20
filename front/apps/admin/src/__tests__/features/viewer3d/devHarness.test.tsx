import { MockApp } from '@/features/viewer3d/dev-harness/MockApp';
import { readMockUi, withMockUi } from '@/features/viewer3d/dev-harness/mockUi';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('목업 UI 주소 매개변수', () => {
  it('기본은 켜고 0·false·off 만 끈다', () => {
    expect(readMockUi('')).toBe(true);
    expect(readMockUi('?mockUi=1')).toBe(true);
    expect(readMockUi('?mockUi=0')).toBe(false);
    expect(readMockUi('?mockUi=OFF')).toBe(false);
    expect(readMockUi('?debug=1&mockUi=false')).toBe(false);
  });

  it('다른 매개변수와 해시는 두고 mockUi 만 바꾼다', () => {
    expect(withMockUi('http://localhost:5173/x/index.html?a=1#h', false)).toBe(
      'http://localhost:5173/x/index.html?a=1&mockUi=0#h',
    );
  });
});

// jsdom 에는 WebGL 이 없다. 크기 0 이면 렌더러를 만들지 않으므로 Canvas DOM 과 오버레이 전환만 본다.
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

describe('MockApp', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', StubResizeObserver);
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    window.history.replaceState(null, '', '/dev?mockUi=1');
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('메뉴 이동·패널 닫기·목업 ON/OFF 를 반복해도 같은 Canvas 를 쓰고 주소만 바꾼다', () => {
    const { container } = render(<MockApp />);
    const canvas = container.querySelector('canvas');
    expect(container.querySelectorAll('canvas')).toHaveLength(1);

    for (const menu of ['cctv', 'zones', 'tbm', 'system', 'monitor']) {
      fireEvent.click(container.querySelector(`[data-menu="${menu}"]`)!);
      expect(screen.getByTestId('mock-panel')).toBeTruthy();
    }
    fireEvent.click(screen.getByRole('button', { name: '패널 닫기' }));
    expect(screen.queryByTestId('mock-panel')).toBeNull();

    const toggle = screen.getByRole('button', { name: /목업 UI/ });
    for (let i = 0; i < 3; i++) {
      act(() => toggle.click());
      expect(screen.queryByTestId('mock-ui')).toBeNull();
      expect(window.location.search).toBe('?mockUi=0');
      act(() => toggle.click());
      expect(screen.getByTestId('mock-ui')).toBeTruthy();
      expect(window.location.search).toBe('?mockUi=1');
    }

    expect(container.querySelectorAll('canvas')).toHaveLength(1);
    expect(container.querySelector('canvas')).toBe(canvas);
  });

  it('mockUi=0 으로 열면 3D 만 그리고 토글만 남긴다', () => {
    window.history.replaceState(null, '', '/dev?mockUi=0');
    const { container } = render(<MockApp />);
    expect(container.querySelectorAll('canvas')).toHaveLength(1);
    expect(screen.queryByTestId('mock-ui')).toBeNull();
    expect(screen.getByRole('button', { name: '목업 UI OFF' })).toBeTruthy();
  });
});
