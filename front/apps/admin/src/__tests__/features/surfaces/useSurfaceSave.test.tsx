import { bodyOf } from '@/features/surfaces/surfaces';
import { surfaceSaveErrorText, useSurfaceSave } from '@/features/surfaces/useSurfaceSave';
import { useWorkSurfaces } from '@/features/surfaces/useWorkSurfaces';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ token: 'mock-token.admin' }));
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => auth.token }) };
});

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  auth.token = 'mock-token.admin';
  server.resetHandlers(...createStaticObjectsMock().handlers);
});
afterEach(cleanup);

async function run() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => ({ list: useWorkSurfaces(), writes: useSurfaceSave() }), {
    wrapper,
  });
  await waitFor(() => expect(view.result.current.list.surfaces).toHaveLength(2));
  return view;
}
type View = Awaited<ReturnType<typeof run>>;
const surfaceIn = (view: View, id: string) =>
  view.result.current.list.surfaces.find((s) => s.objectId === id)!;
const failure = async (promise: Promise<unknown>) => {
  let error: unknown;
  await act(async () => {
    error = await promise.catch((e: unknown) => e);
  });
  return error;
};
/** 지금부터 작업면 PUT 요청을 센다. 답은 원래 mock 이 한다. */
const countPuts = () => {
  const counter = { count: 0 };
  server.use(
    http.put('*/api/static-objects/:objectId', () => {
      counter.count += 1;
      return undefined;
    }),
  );
  return counter;
};
const otherAdminSaves = (view: View, id: string, name: string) => {
  const s = surfaceIn(view, id);
  return fetch(`http://api.test/api/static-objects/${id}`, {
    method: 'PUT',
    headers: { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' },
    body: JSON.stringify(bodyOf(s, name, s.geometry)),
  });
};

describe('useWorkSurfaces', () => {
  it('받기 전·받지 못하면 3D 에 넣을 묶음이 없다(빈 목록으로 3D 를 비우지 않는다)', async () => {
    server.use(http.get('*/api/static-objects', () => HttpResponse.error()));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = renderHook(() => useWorkSurfaces(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    expect(view.result.current.raw).toBeUndefined();
    await waitFor(() => expect(view.result.current.failed).toBe(true));
    expect(view.result.current).toMatchObject({ raw: undefined, loaded: false, surfaces: [] });
  });

  it('받은 작업면을 3D 뷰어 묶음으로도 준다', async () => {
    const view = await run();
    expect(view.result.current.list.raw?.work_surfaces.map((s) => s.surface_id)).toEqual([
      'ws-0a0c0001',
      'ws-0a0c0002',
    ]);
  });
});

describe('useSurfaceSave', () => {
  it('새 버전을 저장하면 응답(새 버전)으로 목록을 바꾼다', async () => {
    const view = await run();
    const desk = surfaceIn(view, 'ws-0a0c0002');
    const geometry = { ...desk.geometry, topHeightM: 0.8 };
    await act(() =>
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: bodyOf(desk, '교탁', geometry),
      }),
    );
    await waitFor(() =>
      expect(surfaceIn(view, 'ws-0a0c0002')).toMatchObject({
        version: 2,
        name: '교탁',
        geometry: { topHeightM: Math.fround(0.8) },
      }),
    );
  });

  it('다른 관리자가 먼저 새 버전을 저장했으면 보내지 않고 알리며, 다시 받은 최신 작업면을 보인다', async () => {
    const view = await run();
    const stale = surfaceIn(view, 'ws-0a0c0002');
    await otherAdminSaves(view, 'ws-0a0c0002', '다른 사람');
    const puts = countPuts();
    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: stale,
        body: bodyOf(stale, '책상', { ...stale.geometry, topHeightM: 0.9 }),
      }),
    );
    expect(surfaceSaveErrorText(error)).toContain('다른 버전');
    expect(puts.count).toBe(0);
    await waitFor(() =>
      expect(surfaceIn(view, 'ws-0a0c0002')).toMatchObject({ version: 2, name: '다른 사람' }),
    );
  });

  it('저장 전에 지금 버전을 확인하지 못하면 보내지 않는다', async () => {
    const view = await run();
    const desk = surfaceIn(view, 'ws-0a0c0002');
    const puts = countPuts();
    server.use(http.get('*/api/static-objects', () => HttpResponse.error()));
    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: bodyOf(desk, '책상', desk.geometry),
      }),
    );
    expect(surfaceSaveErrorText(error)).toContain('확인하지 못해 저장하지 않았습니다');
    expect(puts.count).toBe(0);
  });

  it('같은 순간 부딪쳐 서버가 409 로 거절하면 알리고, 관리자가 아니면 403 을 알린다', async () => {
    const view = await run();
    const desk = surfaceIn(view, 'ws-0a0c0002');
    server.use(
      http.put('*/api/static-objects/:objectId', () =>
        HttpResponse.json({ status: 409, code: 'CONCURRENT_EDIT' }, { status: 409 }),
      ),
    );
    const conflict = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: bodyOf(desk, '책상', desk.geometry),
      }),
    );
    expect(surfaceSaveErrorText(conflict)).toContain('다른 버전');

    server.resetHandlers(...createStaticObjectsMock().handlers);
    auth.token = 'mock-token.viewer';
    const forbidden = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: bodyOf(desk, '책상', desk.geometry),
      }),
    );
    expect(surfaceSaveErrorText(forbidden)).toBe('작업면을 저장할 권한이 없습니다.');
  });

  it('서버가 형식을 거절하면(INVALID_STATIC_OBJECT) 그렇게 알린다', async () => {
    const view = await run();
    const desk = surfaceIn(view, 'ws-0a0c0002');
    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: { ...bodyOf(desk, '책상', desk.geometry), top_height_m: 0 },
      }),
    );
    expect(surfaceSaveErrorText(error)).toBe('작업면 값이 올바르지 않아 저장하지 못했습니다.');
  });

  it('새 작업면은 서버가 정한 ws- ID 의 v1 로 목록 끝에 더한다', async () => {
    const view = await run();
    const created = await act(() =>
      view.result.current.writes.create.mutateAsync(
        bodyOf(null, '새 책상', {
          centerXy: [8, 2],
          sizeXy: [1.2, 0.6],
          yawRad: 0,
          topHeightM: 0.74,
        }),
      ),
    );
    expect(created.object_id).toMatch(/^ws-[0-9a-f]{8}$/);
    await waitFor(() =>
      expect(view.result.current.list.surfaces.map((s) => [s.objectId, s.version])).toEqual([
        ['ws-0a0c0001', 1],
        ['ws-0a0c0002', 1],
        [created.object_id, 1],
      ]),
    );
  });

  it('응답을 받지 못해 저장됐는지 모르면 그렇게 알리고 최신 작업면을 다시 받는다', async () => {
    const view = await run();
    const desk = surfaceIn(view, 'ws-0a0c0002');
    // 서버는 저장했는데(v2) 응답이 끊겼다. 안쪽 요청은 원래 mock 이 저장한다.
    let forwarded = false;
    server.use(
      http.put('*/api/static-objects/:objectId', async ({ request }) => {
        if (forwarded) return undefined;
        forwarded = true;
        await fetch(request.url, {
          method: 'PUT',
          headers: request.headers,
          body: await request.text(),
        });
        return HttpResponse.error();
      }),
    );
    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        surface: desk,
        body: bodyOf(desk, '저장됨', desk.geometry),
      }),
    );
    expect(surfaceSaveErrorText(error)).toContain('저장됐는지 확인하지 못해');
    await waitFor(() =>
      expect(surfaceIn(view, 'ws-0a0c0002')).toMatchObject({ version: 2, name: '저장됨' }),
    );
  });
});
