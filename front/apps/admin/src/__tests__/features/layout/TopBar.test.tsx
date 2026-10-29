import { TopBar } from '@/features/layout/TopBar';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();
vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
  useNavigate: () => navigate,
}));
vi.mock('@/features/auth/useLogout', () => ({ useLogout: () => () => undefined }));

const T = 1_760_342_400_000;
const MISMATCH = { committed_version: 'scene-3', live_version: 'scene-2', mismatch: true };

function seed(change: Partial<ViewerHealthPayload> = {}) {
  const now = Date.now();
  useRealtimeStore.setState({
    phase: 'OPEN',
    openedAt: now,
    health: {
      payload: { ...viewerHealthAt(T).payload, ...change },
      receivedAt: now,
      clockOffsetMs: 0,
    },
  });
}

afterEach(() => {
  cleanup();
  navigate.mockClear();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('TopBar', () => {
  it('로고·현장명·시계·관리자 메뉴만 두고 상태 값(System Health·데이터·카메라·보정·구성·장면)은 보이지 않는다', () => {
    seed({ scene: MISMATCH });
    const { container } = render(<TopBar floating={false} />);

    const text = container.querySelector('header')!.textContent!;
    expect(text).toContain('교실 테스트베드 · 10×10m');
    for (const label of ['NOMINAL', '데이터', '카메라', '보정', '구성', '장면', 'scene-3']) {
      expect(text).not.toContain(label);
    }
    // 장면 불일치 경고(줄무늬)는 시스템 상태 화면에만 있다.
    expect(container.querySelector('.bg-stripes')).toBeNull();
  });

  it('관리자 메뉴는 설치 · 보정, 화이트·다크 모드, 로그아웃이고 시험 경보는 없다', async () => {
    seed();
    render(<TopBar floating={false} />);

    act(() => fireEvent.click(screen.getByRole('button', { name: /—/ })));
    const items = (await screen.findAllByRole('menuitem')).map((item) => item.textContent);
    expect(items).toEqual(['설치 · 보정', '화이트 모드', '로그아웃']);

    act(() => fireEvent.click(screen.getByRole('menuitem', { name: '설치 · 보정' })));
    expect(navigate).toHaveBeenCalledWith({ to: '/setup' });
  });
});
