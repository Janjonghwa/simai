import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { TopBar } from '@/features/layout/TopBar';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useRoomBoundsStore } from '@/store/roomBounds.store';
import { useSessionStore, type Role } from '@/store/session.store';
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

function signInAs(role: Role) {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u-1', role } });
}

afterEach(() => {
  cleanup();
  navigate.mockClear();
  useSessionStore.getState().signOut();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useRoomBoundsStore.setState(useRoomBoundsStore.getInitialState(), true);
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

  it('스캔이 보이면 목업 크기(10×10m)를 빼고 이름만 보인다', () => {
    seed();
    const { container } = render(<TopBar floating={false} />);
    const name = () => container.querySelector('header > span')!.textContent;
    expect(name()).toBe('교실 테스트베드 · 10×10m');

    act(() => useRoomBoundsStore.getState().set({ min: [0, 0, 0], max: [7.2, 5.8, 3] }));
    expect(name()).toBe('교실 테스트베드');

    act(() => useRoomBoundsStore.getState().set(null));
    expect(name()).toBe('교실 테스트베드 · 10×10m');
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

  it.each(['ADMIN', 'FOREMAN'] as const)(
    '%s 계정에는 시계와 관리자 메뉴 사이에 경보 소리 버튼이 있다',
    (role) => {
      seed();
      signInAs(role);
      const { container } = render(<TopBar floating={false} />);

      const sound = screen.getByRole('button', { name: /경보 소리/ });
      const clock = container.querySelector('header span.font-mono')!;
      const account = screen.getByRole('button', { name: new RegExp(ROLE_LABEL[role]) });
      expect(clock.compareDocumentPosition(sound) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(
        sound.compareDocumentPosition(account) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    },
  );

  it('열람 계정은 확인 권한이 없어 소리를 못 끄므로 소리 버튼이 없다', () => {
    seed();
    signInAs('VIEWER');
    render(<TopBar floating={false} />);

    expect(screen.queryByRole('button', { name: /경보 소리/ })).toBeNull();
    expect(screen.getByRole('button', { name: new RegExp(ROLE_LABEL.VIEWER) })).toBeTruthy();
  });

  it('로그인 전에는 소리 버튼이 없다', () => {
    seed();
    render(<TopBar floating={false} />);
    expect(screen.queryByRole('button', { name: /경보 소리/ })).toBeNull();
  });
});
