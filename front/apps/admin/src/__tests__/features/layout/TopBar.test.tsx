import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { TopBar } from '@/features/layout/TopBar';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useRoomBoundsStore } from '@/store/roomBounds.store';
import { useSessionStore, type Role } from '@/store/session.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const site = { site_id: 'demo-site', name: '부산 안전 실습장' };
const server = setupServer(
  http.get('*/api/sites/current', () => HttpResponse.json(site)),
  // GPU 점(S15P31A101-702)은 관리자만 상태를 묻는다
  http.get('*/api/gpu/sessions/current', () =>
    HttpResponse.json({
      session: null,
      server_time_ms: Date.now(),
      launch: null,
      power: 'OFF',
      stop_block: null,
    }),
  ),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

function renderTopBar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <TopBar floating={false} />
      </QueryClientProvider>,
    ),
  };
}

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
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
  server.resetHandlers();
  useSessionStore.getState().signOut();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useRoomBoundsStore.setState(useRoomBoundsStore.getInitialState(), true);
});

describe('TopBar', () => {
  it('로고·시계·관리자 메뉴만 두고 현장명·상태 값(System Health·데이터·카메라·보정·구성·장면)은 보이지 않는다', () => {
    seed({ scene: MISMATCH });
    const { container } = renderTopBar();

    const text = container.querySelector('header')!.textContent!;
    expect(text).not.toContain(site.name);
    expect(text).not.toContain('교실 테스트베드');
    expect(text).not.toContain('10×10m');
    for (const label of ['NOMINAL', '데이터', '카메라', '보정', '구성', '장면', 'scene-3']) {
      expect(text).not.toContain(label);
    }
    // 장면 불일치 경고(줄무늬)는 시스템 상태 화면에만 있다.
    expect(container.querySelector('.bg-stripes')).toBeNull();
  });

  it('판정 지연은 상단바 글에서 빠지고 GPU 점의 마우스 올림 정보창 한 줄이다. 기준(600ms) 이상이면 지연 색이다(S15P31A101-702)', () => {
    seed();
    useRealtimeStore.setState({
      lagSamples: [
        { at: T, lagMs: 140, cameras: {} },
        { at: T + 1000, lagMs: 220, cameras: {} },
      ],
    });
    const { container } = renderTopBar();
    expect(container.querySelector('header')!.textContent).not.toContain('판정 지연');
    expect(screen.getByTestId('gpu-indicator').textContent).toBe('GPU 연결');

    act(() => fireEvent.mouseEnter(screen.getByTestId('gpu-indicator')));
    const lag = screen.getByText('판정 지연 180 ms');
    expect(screen.getByRole('tooltip').contains(lag)).toBe(true);
    expect(lag.className).not.toContain('text-delay');

    act(() =>
      useRealtimeStore.setState({ lagSamples: [{ at: T + 2000, lagMs: 700, cameras: {} }] }),
    );
    expect(screen.getByText('판정 지연 700 ms').className).toContain('text-delay');
  });

  it('지연을 모르거나 EC2 와 끊겼으면 지연을 보이지 않는다(지어내지 않음)', () => {
    seed();
    const { container } = renderTopBar();
    expect(container.textContent).not.toContain('지연');

    act(() =>
      useRealtimeStore.setState({
        phase: 'CLOSED',
        lagSamples: [{ at: T, lagMs: 140, cameras: {} }],
      }),
    );
    expect(container.textContent).not.toContain('지연');
  });

  it('GPU(Tier 0)가 꺼져 있으면(EC2 는 이어짐) 점은 「GPU 꺼짐」이고 판정 지연을 숨긴다(S15P31A101-704)', () => {
    seed({ tier0_link: 'DISCONNECTED' });
    // 끊기기 전 표본이 남아 있어도 보이지 않는다.
    useRealtimeStore.setState({ lagSamples: [{ at: T, lagMs: 140, cameras: {} }] });
    const { container } = renderTopBar();
    act(() => fireEvent.mouseEnter(screen.getByTestId('gpu-indicator')));
    expect(screen.getByTestId('gpu-indicator').textContent).toBe('GPU 꺼짐');
    expect(container.textContent).not.toContain('지연');
    expect(container.textContent).not.toContain('끊김');

    act(() => seed());
    act(() => fireEvent.mouseEnter(screen.getByTestId('gpu-indicator')));
    expect(screen.getByText('판정 지연 140 ms')).toBeTruthy();
  });

  it('관리자 메뉴는 화이트·다크 모드와 로그아웃뿐이다(설치 · 보정은 작업 관리 단계 줄의 세팅으로 들어간다)', async () => {
    seed();
    renderTopBar();

    act(() => fireEvent.click(screen.getByRole('button', { name: /—/ })));
    const items = (await screen.findAllByRole('menuitem')).map((item) => item.textContent);
    expect(items).toEqual(['화이트 모드', '로그아웃']);
  });

  it.each([
    ['ADMIN', ['화이트 모드', 'GPU 설정', '로그아웃']],
    ['FOREMAN', ['화이트 모드', '로그아웃']],
  ] as const)(
    '%s 메뉴에 규칙 기준 선택이 없고 GPU 설정은 관리자에게만 있다(-669, -702)',
    async (role, expected) => {
      seed();
      signInAs(role);
      renderTopBar();
      act(() =>
        fireEvent.click(screen.getByRole('button', { name: new RegExp(ROLE_LABEL[role]) })),
      );
      const items = (await screen.findAllByRole('menuitem')).map((item) => item.textContent);
      expect(items).toEqual(expected);
      expect(screen.queryByRole('menuitemradio')).toBeNull();
      expect(screen.queryByText('규칙 기준')).toBeNull();
    },
  );

  it.each(['ADMIN', 'FOREMAN'] as const)(
    '%s 계정에는 시계와 관리자 메뉴 사이에 경보 소리 버튼이 있다',
    (role) => {
      seed();
      signInAs(role);
      const { container } = renderTopBar();

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
    renderTopBar();

    expect(screen.queryByRole('button', { name: /경보 소리/ })).toBeNull();
    expect(screen.getByRole('button', { name: new RegExp(ROLE_LABEL.VIEWER) })).toBeTruthy();
  });

  it('로그인 전에는 소리 버튼이 없다', () => {
    seed();
    renderTopBar();
    expect(screen.queryByRole('button', { name: /경보 소리/ })).toBeNull();
  });
});
