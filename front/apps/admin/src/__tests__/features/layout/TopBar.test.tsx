import { ROLE_LABEL } from '@/features/auth/roleLabel';
import { TopBar } from '@/features/layout/TopBar';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useRoomBoundsStore } from '@/store/roomBounds.store';
import { useSessionStore, type Role } from '@/store/session.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { delay } from 'msw';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const site = { site_id: 'demo-site', name: '부산 안전 실습장' };
const server = setupServer(http.get('*/api/sites/current', () => HttpResponse.json(site)));
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
  it('로고·서버 현장명·시계·관리자 메뉴만 두고 상태 값(System Health·데이터·카메라·보정·구성·장면)은 보이지 않는다', async () => {
    seed({ scene: MISMATCH });
    const { container } = renderTopBar();
    await screen.findByText(site.name);

    const text = container.querySelector('header')!.textContent!;
    expect(text).toContain(site.name);
    expect(text).not.toContain('교실 테스트베드');
    expect(text).not.toContain('10×10m');
    for (const label of ['NOMINAL', '데이터', '카메라', '보정', '구성', '장면', 'scene-3']) {
      expect(text).not.toContain(label);
    }
    // 장면 불일치 경고(줄무늬)는 시스템 상태 화면에만 있다.
    expect(container.querySelector('.bg-stripes')).toBeNull();
  });

  it('Tier 0 가 잰 처리 지연의 최근 평균을 시계 옆에 ms 로 보이고, 기준(600ms)을 넘으면 지연 색이다', () => {
    seed();
    useRealtimeStore.setState({
      lagSamples: [
        { at: T, lagMs: 140, cameras: {} },
        { at: T + 1000, lagMs: 220, cameras: {} },
      ],
    });
    renderTopBar();
    const lag = screen.getByText('지연 180 ms');
    expect(lag.className).not.toContain('text-delay');

    act(() =>
      useRealtimeStore.setState({ lagSamples: [{ at: T + 2000, lagMs: 700, cameras: {} }] }),
    );
    expect(screen.getByText('지연 700 ms').className).toContain('text-delay');
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

  it('스캔 유무와 관계없이 서버 현장명만 보인다', async () => {
    seed();
    const { container } = renderTopBar();
    await screen.findByText(site.name);
    const name = () => container.querySelector('header > span')!.textContent;
    expect(name()).toBe(site.name);

    act(() => useRoomBoundsStore.getState().set({ min: [0, 0, 0], max: [7.2, 5.8, 3] }));
    expect(name()).toBe(site.name);

    act(() => useRoomBoundsStore.getState().set(null));
    expect(name()).toBe(site.name);
  });

  it('로딩 중에는 현장 이름과 목업 크기를 비운다', () => {
    server.use(
      http.get('*/api/sites/current', async () => {
        await delay('infinite');
        return HttpResponse.json(site);
      }),
    );
    const { container } = renderTopBar();
    expect(container.querySelector('header > span')!.textContent).toBe('');
  });

  it.each([
    HttpResponse.json({ message: '조회 실패' }, { status: 503 }),
    HttpResponse.json({ site_id: 'broken', name: 42 }),
  ])('실패하거나 응답 형식이 다르면 교실 문구를 대신 표시하지 않는다', async (response) => {
    server.use(http.get('*/api/sites/current', () => response.clone()));
    const { container, client } = renderTopBar();
    await waitFor(() => expect(client.getQueryState(['sites', 'current'])?.status).toBe('error'));
    expect(container.querySelector('header > span')!.textContent).toBe('');
  });

  it('관리자 메뉴는 화이트·다크 모드와 로그아웃뿐이다(설치 · 보정은 작업 관리 단계 줄의 세팅으로 들어간다)', async () => {
    seed();
    renderTopBar();

    act(() => fireEvent.click(screen.getByRole('button', { name: /—/ })));
    const items = (await screen.findAllByRole('menuitem')).map((item) => item.textContent);
    expect(items).toEqual(['화이트 모드', '로그아웃']);
  });

  it('관리자 메뉴의 규칙 기준(-654): 지금 기준을 보이고, 교실 시험을 고르면 서버에 저장하고 새 묶음 발행을 알린다', async () => {
    const puts: unknown[] = [];
    server.use(
      http.get('*/api/sites/current', () =>
        HttpResponse.json({ ...site, rule_profile: 'OPERATION' }),
      ),
      http.put('*/api/sites/current/rule-profile', async ({ request }) => {
        puts.push(await request.json());
        return HttpResponse.json({
          rule_profile: 'CLASSROOM_TEST',
          bundle: { config_version: 'cfg-70' },
          publish_error: null,
        });
      }),
    );
    seed();
    signInAs('ADMIN');
    renderTopBar();
    await screen.findByText(site.name);

    act(() => fireEvent.click(screen.getByRole('button', { name: /관리자/ })));
    const operation = await screen.findByRole('menuitemradio', { name: '운영 (고소 2.0m)' });
    expect(operation.getAttribute('aria-checked')).toBe('true');
    act(() =>
      fireEvent.click(screen.getByRole('menuitemradio', { name: '교실 시험 (고소 0.35m)' })),
    );
    expect(await screen.findByText('새 구성 묶음 cfg-70 발행')).toBeTruthy();
    expect(puts).toEqual([{ rule_profile: 'CLASSROOM_TEST' }]);
  });

  it('관리자가 아니면 규칙 기준을 보이지 않는다', async () => {
    seed();
    signInAs('FOREMAN');
    renderTopBar();
    act(() =>
      fireEvent.click(screen.getByRole('button', { name: new RegExp(ROLE_LABEL.FOREMAN) })),
    );
    await screen.findAllByRole('menuitem');
    expect(screen.queryByRole('menuitemradio')).toBeNull();
  });

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
