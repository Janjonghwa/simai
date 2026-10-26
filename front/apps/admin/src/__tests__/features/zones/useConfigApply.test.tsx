import { BUNDLES_KEY } from '@/features/zones/api';
import {
  publishErrorText,
  readBundles,
  unpublishedZoneIds,
  useConfigApply,
} from '@/features/zones/useConfigApply';
import { useZones } from '@/features/zones/useZones';
import { useZoneSave } from '@/features/zones/useZoneSave';
import { bodyOf, formOf } from '@/features/zones/zoneForm';
import { ApiError } from '@/lib/http';
import { createZonesMock, type ZonesMock } from '@/mocks/zones';
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

let mock: ZonesMock;
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  auth.token = 'mock-token.admin';
  // Tier 0 ACK 는 발행 0.2초 뒤에 온다.
  mock = createZonesMock({ ackDelayMs: 200 });
  server.resetHandlers(...mock.handlers);
});
afterEach(cleanup);

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

async function run() {
  const view = renderHook(
    () => {
      const list = useZones();
      return { list, writes: useZoneSave(), apply: useConfigApply(list.zones) };
    },
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.list.zones).toHaveLength(2));
  await waitFor(() => expect(view.result.current.apply.unpublished).not.toBeNull());
  return view;
}
type View = Awaited<ReturnType<typeof run>>;
const saveFirstZone = async (view: View) => {
  const zone = view.result.current.list.zones[0]!;
  await act(() =>
    view.result.current.writes.save.mutateAsync({ zone, form: formOf(zone), box: null }),
  );
};

describe('발행 전 판정', () => {
  const bundle = {
    config_version: 'cfg-12',
    issued_at: 100,
    status: 'APPLIED' as const,
    applied_at: 150,
  };
  const zones = [
    { zoneId: 'z-04', validFrom: 50 },
    { zoneId: 'z-02', validFrom: 100 },
    { zoneId: 'z-05', validFrom: 101 },
  ];
  it('마지막 묶음보다 늦게 저장한 구역만 발행 전이고, 묶음이 없으면 모두, 목록을 모르면 판정하지 않는다', () => {
    expect([...unpublishedZoneIds(zones, bundle)!]).toEqual(['z-05']);
    expect([...unpublishedZoneIds(zones, null)!]).toEqual(['z-04', 'z-02', 'z-05']);
    expect(unpublishedZoneIds(zones, undefined)).toBeNull();
  });

  it('형식이 틀린 묶음은 빼고 읽는다', () => {
    expect(readBundles([bundle, { config_version: 'cfg-11', status: 'DONE' }])).toEqual([bundle]);
  });

  it('발행 실패 이유를 서버 오류 코드로 알린다', () => {
    expect(publishErrorText(new ApiError(403))).toBe('현장에 적용할 권한이 없습니다.');
    expect(publishErrorText(new ApiError(409, { code: 'CALIBRATION_MISSING' }))).toContain(
      '보정 문서',
    );
    expect(publishErrorText(new ApiError(409, { code: 'RULES_MISSING' }))).toContain('규칙 버전');
    expect(publishErrorText(new ApiError(503, { code: 'PUBLIC_URL_NOT_SET' }))).toContain(
      '공개 주소',
    );
    expect(publishErrorText(new ApiError(500))).toBe(
      '현장에 적용하지 못했습니다. 다시 시도하세요.',
    );
  });
});

describe('useConfigApply', () => {
  it('최근 묶음 목록의 첫 묶음으로 적용 상태를 보이고, 저장한 구역은 발행 전이다', async () => {
    const view = await run();
    expect(view.result.current.apply.latest).toMatchObject({
      config_version: 'cfg-12',
      status: 'APPLIED',
    });
    expect(view.result.current.apply.unpublished?.size).toBe(0);

    await saveFirstZone(view);
    await waitFor(() => expect([...view.result.current.apply.unpublished!]).toEqual(['z-04']));
  });

  it('발행하면 응답한 새 묶음을 적용 대기로 보이고 발행 전이 사라지며, 적용 대기 동안 다시 받아 현장 적용됨이 된다', async () => {
    const view = await run();
    await saveFirstZone(view);
    await act(() => view.result.current.apply.publish.mutateAsync());

    await waitFor(() =>
      expect(view.result.current.apply.latest).toMatchObject({
        config_version: 'cfg-13',
        status: 'PENDING',
      }),
    );
    expect(view.result.current.apply.unpublished?.size).toBe(0);
    await waitFor(
      () => expect(view.result.current.apply.latest).toMatchObject({ status: 'APPLIED' }),
      { timeout: 5_000 },
    );
  });

  it('발행 전 구역이 없으면 발행을 막고, Tier 0 가 거부하면 적용 거부와 계속 쓰는 묶음(이전 묶음)을 주며 다시 발행할 수 있다', async () => {
    const view = await run();
    expect(view.result.current.apply.canPublish).toBe(false);
    await saveFirstZone(view);
    await waitFor(() => expect(view.result.current.apply.canPublish).toBe(true));

    mock.rejectNextBundle();
    await act(() => view.result.current.apply.publish.mutateAsync());
    await waitFor(() => expect(view.result.current.apply.canPublish).toBe(false));
    await waitFor(
      () =>
        expect(view.result.current.apply.latest).toMatchObject({
          config_version: 'cfg-13',
          status: 'REJECTED',
          last_ack: { active_config_version: 'cfg-12' },
        }),
      { timeout: 5_000 },
    );
    expect(view.result.current.apply.unpublished?.size).toBe(0);
    expect(view.result.current.apply.canPublish).toBe(true);
  });

  it('발행하는 동안 받던 옛 목록이 늦게 와도 새 묶음(적용 대기)을 되돌리지 않는다', async () => {
    const view = await run();
    const before = await (
      await fetch('http://api.test/api/config-bundles', {
        headers: { Authorization: 'Bearer mock-token.admin' },
      })
    ).json();
    let held = false;
    server.use(
      http.get('*/api/config-bundles', async () => {
        if (held) return undefined;
        held = true;
        await delay(150);
        return HttpResponse.json(before);
      }),
    );
    act(() => {
      void client.invalidateQueries({ queryKey: BUNDLES_KEY });
    });
    await act(() => view.result.current.apply.publish.mutateAsync());
    await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
    expect(view.result.current.apply.latest).toMatchObject({
      config_version: 'cfg-13',
      status: 'PENDING',
    });
  });

  it('구역 편집 화면에 들어오면 구역을 다시 받아, 그 전에 다른 관리자가 저장한 구역도 발행 전으로 보인다', async () => {
    const list = renderHook(() => useZones(), { wrapper });
    await waitFor(() => expect(list.result.current.zones).toHaveLength(2));
    const z04 = list.result.current.zones[0]!;
    await fetch('http://api.test/api/zones/z-04', {
      method: 'PUT',
      headers: { Authorization: 'Bearer mock-token.admin', 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyOf(z04, { ...formOf(z04), name: '다른 사람' }, null)),
    });

    const page = renderHook(
      () => {
        const { zones } = useZones();
        return useConfigApply(zones);
      },
      { wrapper },
    );
    await waitFor(() => expect([...(page.result.current.unpublished ?? [])]).toEqual(['z-04']));
    expect(page.result.current.canPublish).toBe(true);
  });
});
