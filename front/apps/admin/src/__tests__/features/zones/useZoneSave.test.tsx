import { ZONES_KEY } from '@/features/zones/api';
import { useZones } from '@/features/zones/useZones';
import { saveErrorText, useZoneSave } from '@/features/zones/useZoneSave';
import {
  defaultNewBox,
  NEW_EVAL_TYPE,
  NEW_ZONE_KIND,
  NEW_ZONE_NAME,
  NEW_ZONE_SEVERITY,
} from '@/features/zones/zoneEditModel';
import { bodyOf, formOf, newZoneBodyOf } from '@/features/zones/zoneForm';
import { ApiError } from '@/lib/http';
import { DEFAULT_ROOM_LIMIT } from '@/lib/roomLimit';
import { createZonesMock } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { delay } from 'msw';
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
  server.resetHandlers(...createZonesMock().handlers);
});
afterEach(cleanup);

let client: QueryClient;
async function run() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => ({ list: useZones(), writes: useZoneSave() }), { wrapper });
  await waitFor(() => expect(view.result.current.list.zones).toHaveLength(2));
  return view;
}
type View = Awaited<ReturnType<typeof run>>;
const zoneIn = (view: View, id: string) =>
  view.result.current.list.zones.find((z) => z.zoneId === id)!;
/** 지금부터 구역 목록 조회(GET)나 새 버전 저장(PUT) 요청을 센다. 답은 원래 mock 이 한다. */
const countRequests = (method: 'GET' | 'PUT') => {
  const counter = { count: 0 };
  const count = () => {
    counter.count += 1;
    return undefined;
  };
  server.use(
    method === 'GET' ? http.get('*/api/zones', count) : http.put('*/api/zones/:zoneId', count),
  );
  return counter;
};
const failure = async (promise: Promise<unknown>) => {
  let error: unknown;
  await act(async () => {
    error = await promise.catch((e: unknown) => e);
  });
  return error;
};

describe('useZoneSave', () => {
  it('저장하면 응답(새 버전)으로 목록을 바꾼다', async () => {
    const view = await run();
    const zone = zoneIn(view, 'z-04');

    await act(() =>
      view.result.current.writes.save.mutateAsync({
        zone,
        form: { ...formOf(zone), name: '개구부 위', severity: 'HIGH' },
        box: { min: [6, 1.2, 0], max: [9, 3.8, 3] },
      }),
    );

    await waitFor(() =>
      expect(zoneIn(view, 'z-04')).toMatchObject({
        version: 2,
        name: '개구부 위',
        severity: 'HIGH',
        min: [6, 1.2, 0],
        max: [9, 3.8, 3],
      }),
    );
  });

  it('형상을 고치지 않았으면 받은 다각형·높이를 그대로 보낸다', async () => {
    const view = await run();
    const zone = zoneIn(view, 'z-02');
    await act(() =>
      view.result.current.writes.save.mutateAsync({
        zone,
        form: { ...formOf(zone), zoneType: 'LIFTING' },
        box: null,
      }),
    );
    await waitFor(() =>
      expect(zoneIn(view, 'z-02')).toMatchObject({
        version: 2,
        zoneType: 'LIFTING',
        polygon: zone.polygon,
        zRange: zone.zRange,
      }),
    );
  });

  it('다른 관리자가 먼저 새 버전을 저장했으면 덮지 않고(보내지 않음) 알리며, 다시 받은 최신 구역을 보인다', async () => {
    const view = await run();
    const stale = zoneIn(view, 'z-04');
    // 다른 관리자가 먼저 v2 를 저장했다(이 화면의 목록은 아직 v1). 서버는 고치기 시작한 버전을 보지 않아 그대로 받는다.
    await fetch('http://api.test/api/zones/z-04', {
      method: 'PUT',
      headers: { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyOf(stale, { ...formOf(stale), name: '다른 사람' }, null)),
    });
    const puts = countRequests('PUT');

    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        zone: stale,
        form: { ...formOf(stale), severity: 'INFO' },
        box: null,
      }),
    );
    expect(saveErrorText(error)).toContain('다른 버전');
    expect(puts.count).toBe(0);
    await waitFor(() =>
      expect(zoneIn(view, 'z-04')).toMatchObject({
        version: 2,
        name: '다른 사람',
        severity: 'CRITICAL',
      }),
    );
  });

  it('확인한 뒤 같은 순간 부딪쳐 서버가 409(CONCURRENT_EDIT)로 거절하면 알리고 최신 구역을 다시 받는다', async () => {
    const view = await run();
    const zone = zoneIn(view, 'z-04');
    const gets = countRequests('GET');
    server.use(
      http.put('*/api/zones/:zoneId', () =>
        HttpResponse.json({ status: 409, code: 'CONCURRENT_EDIT' }, { status: 409 }),
      ),
    );

    const error = await failure(
      view.result.current.writes.save.mutateAsync({ zone, form: formOf(zone), box: null }),
    );
    expect(saveErrorText(error)).toContain('다른 버전');
    // 저장 전 확인 한 번, 거절 뒤 다시 받기 한 번.
    await waitFor(() => expect(gets.count).toBe(2));
  });

  it('저장 전에 지금 버전을 확인하지 못하면 보내지 않고 알린다', async () => {
    const view = await run();
    const zone = zoneIn(view, 'z-04');
    const puts = countRequests('PUT');
    server.use(http.get('*/api/zones', () => HttpResponse.error()));

    const error = await failure(
      view.result.current.writes.save.mutateAsync({ zone, form: formOf(zone), box: null }),
    );
    expect(saveErrorText(error)).toContain('확인하지 못해 저장하지 않았습니다');
    expect(puts.count).toBe(0);
    expect(zoneIn(view, 'z-04').version).toBe(1);
  });

  it('관리자가 아니면 403 을 알리고 목록은 그대로다', async () => {
    auth.token = 'mock-token.viewer';
    const view = await run();
    const zone = zoneIn(view, 'z-04');
    const error = await failure(
      view.result.current.writes.save.mutateAsync({
        zone,
        form: { ...formOf(zone), name: 'x' },
        box: null,
      }),
    );
    expect(saveErrorText(error)).toBe('구역을 저장할 권한이 없습니다.');
    expect(zoneIn(view, 'z-04').version).toBe(1);
  });

  it('새 구역은 서버가 정한 ID 의 v1 로 목록 끝에 더한다', async () => {
    const view = await run();
    const created = await act(() =>
      view.result.current.writes.create.mutateAsync(
        newZoneBodyOf(
          {
            name: NEW_ZONE_NAME,
            judgeType: NEW_EVAL_TYPE,
            zoneType: NEW_ZONE_KIND,
            severity: NEW_ZONE_SEVERITY,
          },
          defaultNewBox(view.result.current.list.zones, DEFAULT_ROOM_LIMIT),
        ),
      ),
    );
    expect(created.zone_id).toMatch(/^z-[0-9a-f]{8}$/);
    await waitFor(() =>
      expect(view.result.current.list.zones.map((z) => [z.zoneId, z.version])).toEqual([
        ['z-04', 1],
        ['z-02', 1],
        [created.zone_id, 1],
      ]),
    );
  });

  it('응답을 받지 못해 저장됐는지 모르면 그렇게 알리고 최신 구역을 다시 받는다', async () => {
    const view = await run();
    const zone = zoneIn(view, 'z-04');
    // 서버는 저장했는데(v2) 응답이 끊겼다. 안쪽 요청은 원래 mock 이 저장한다.
    let forwarded = false;
    server.use(
      http.put('*/api/zones/:zoneId', async ({ request }) => {
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
        zone,
        form: { ...formOf(zone), name: '저장됨' },
        box: null,
      }),
    );
    expect(saveErrorText(error)).toContain('저장됐는지 확인하지 못해');
    await waitFor(() => expect(zoneIn(view, 'z-04')).toMatchObject({ version: 2, name: '저장됨' }));
  });

  it('다시 받는 중이던 옛 목록이 늦게 와도 저장한 새 버전을 되돌리지 않는다', async () => {
    const view = await run();
    const before = await (
      await fetch('http://api.test/api/zones', {
        headers: { Authorization: 'Bearer mock-token.admin' },
      })
    ).json();
    let held = false;
    server.use(
      http.get('*/api/zones', async () => {
        if (held) return undefined;
        held = true;
        await delay(150);
        return HttpResponse.json(before);
      }),
    );
    act(() => {
      void client.invalidateQueries({ queryKey: ZONES_KEY });
    });
    const zone = zoneIn(view, 'z-04');
    await act(() =>
      view.result.current.writes.save.mutateAsync({
        zone,
        form: { ...formOf(zone), name: 'A' },
        box: null,
      }),
    );
    await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
    expect(zoneIn(view, 'z-04')).toMatchObject({ version: 2, name: 'A' });
  });

  it('구역을 보관(DELETE)하면 응답이 온 뒤 목록에서 뺀다', async () => {
    const view = await run();
    await act(() => view.result.current.writes.archive.mutateAsync('z-04'));
    await waitFor(() =>
      expect(view.result.current.list.zones.map((z) => z.zoneId)).toEqual(['z-02']),
    );
    // 서버도 목록에서 뺀다(이력은 남는다).
    const list = (await (
      await fetch('http://api.test/api/zones', {
        headers: { Authorization: 'Bearer mock-token.admin' },
      })
    ).json()) as { zone_id: string }[];
    expect(list.map((z) => z.zone_id)).toEqual(['z-02']);
  });

  it('보관이 실패하면 목록은 그대로이고 최신 목록을 다시 받는다', async () => {
    const view = await run();
    const gets = countRequests('GET');
    server.use(http.delete('*/api/zones/:zoneId', () => HttpResponse.json({}, { status: 500 })));
    const error = await failure(view.result.current.writes.archive.mutateAsync('z-04'));
    expect(error).toBeInstanceOf(ApiError);
    expect(view.result.current.list.zones.map((z) => z.zoneId)).toContain('z-04');
    await waitFor(() => expect(gets.count).toBeGreaterThan(0));
  });
});
