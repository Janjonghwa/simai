import { Route as IndexRoute } from '@/routes/_app/index';
import { useSessionStore, type Role } from '@/store/session.store';
import { stopZoneEdit, useZoneEditModeStore } from '@/store/zoneEditMode.store';
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from '@tanstack/react-router';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 관제 화면 자체는 이 시험의 관심 밖이다. 라우트가 주소의 `edit` 요청을 읽고 지우는 흐름만 진짜 라우터로 본다.
vi.mock('@/features/monitor/MonitorPage', () => ({
  MonitorPage: () => <div data-testid="monitor" />,
}));

/** 실제 관제 라우트(`/_app/`)를 진짜 라우터에 달아 주소 `url` 에서 시작한다. 로그인 검사 등 바깥 라우트는 뺐다. */
function mountAt(url: string, role: Role) {
  useSessionStore.setState({ session: { accessToken: 't', user: { id: role, role } } });
  const rootRoute = createRootRoute({ component: Outlet });
  const appRoute = createRoute({ getParentRoute: () => rootRoute, id: '/_app' });
  const monitorRoute = IndexRoute.update({
    id: '/',
    path: '/',
    getParentRoute: () => appRoute,
  } as never);
  const router = createRouter({
    routeTree: rootRoute.addChildren([appRoute.addChildren([monitorRoute])]),
    history: createMemoryHistory({ initialEntries: [url] }),
  });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  cleanup();
  stopZoneEdit();
  useSessionStore.setState({ session: undefined });
});

describe('관제 화면 편집 모드 진입(/?edit=true)', () => {
  it('관리자가 /?edit=true 로 열면 편집 모드가 켜지고 요청은 주소에서 지워진다', async () => {
    const router = mountAt('/?edit=true', 'ADMIN');
    await screen.findByTestId('monitor');

    await waitFor(() => expect(useZoneEditModeStore.getState().editing).toBe(true));
    await waitFor(() => expect(router.state.location.href).toBe('/'));
    expect(router.state.location.search.edit).toBeUndefined();
  });

  it('관리자가 아니면 편집 모드는 켜지지 않고 요청만 주소에서 지워진다', async () => {
    const router = mountAt('/?edit=true', 'VIEWER');
    await screen.findByTestId('monitor');

    await waitFor(() => expect(router.state.location.href).toBe('/'));
    expect(useZoneEditModeStore.getState().editing).toBe(false);
  });

  it('요청이 없으면 편집 모드를 켜지 않고 주소도 그대로다', async () => {
    const router = mountAt('/', 'ADMIN');
    await screen.findByTestId('monitor');

    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(router.state.location.href).toBe('/');
  });
});
