import { METRICS_KEY, useMetrics } from '@/features/system/useMetrics';
import { type Role, useSessionStore } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// 앱의 api 인스턴스는 import.meta.env 를 읽는다(CONVENTION 테스트). 같은 createHttp 로 시험 주소를 쓴다.
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  vi.useRealTimers();
  cleanup();
  server.resetHandlers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
});
afterAll(() => server.close());

const body = (contract = 'DEMO_UNCONFIRMED') => ({
  contract,
  as_of: 1,
  stages: {
    capture_to_site_alert_ms: { p50: 340, p95: 520 },
    capture_to_remote_delivery_ms: { viewer: null, pwa: { p50: 1400, p95: 2300 } },
  },
});

const signIn = (role: Role) =>
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role } });

function setup(role: Role, respond: () => Response = () => HttpResponse.json(body())) {
  let calls = 0;
  server.use(
    http.get('http://api.test/api/metrics', () => {
      calls += 1;
      return respond();
    }),
  );
  signIn(role);
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const disabled = () => queryClient.getQueryCache().find({ queryKey: METRICS_KEY })?.isDisabled();
  return { wrapper, queryClient, disabled, calls: () => calls };
}

describe('useMetrics', () => {
  it('관리자면 지표를 받는다', async () => {
    const { wrapper, calls } = setup('ADMIN');
    const { result } = renderHook(() => useMetrics(), { wrapper });

    await waitFor(() => expect(result.current?.contract).toBe('DEMO_UNCONFIRMED'));
    expect(result.current?.stages.capture_to_site_alert_ms).toEqual({ p50: 340, p95: 520 });
    expect(result.current?.stages.capture_to_remote_delivery_ms?.pwa).toEqual({
      p50: 1400,
      p95: 2300,
    });
    expect(calls()).toBe(1);
  });

  it.each(['FOREMAN', 'VIEWER'] as const)(
    '%s 는 부르지 않고, 관리자가 되면 한 번 부른다(명세 12.6 관리자 전용)',
    async (role) => {
      const { wrapper, calls, disabled } = setup(role);
      const { result } = renderHook(() => useMetrics(), { wrapper });

      // 꺼 둔 조회는 시간이 지나도 요청하지 않는다. 기다리지 않고 꺼져 있는지를 본다.
      expect(disabled()).toBe(true);
      expect(result.current).toBeUndefined();
      expect(calls()).toBe(0);

      act(() => signIn('ADMIN'));
      await waitFor(() => expect(result.current).toBeDefined());
      expect(calls()).toBe(1);
    },
  );

  it('관리자가 아니게 되면 받아 둔 값을 돌려주지 않는다', async () => {
    const { wrapper } = setup('ADMIN');
    const { result } = renderHook(() => useMetrics(), { wrapper });
    await waitFor(() => expect(result.current).toBeDefined());

    act(() => signIn('VIEWER'));
    expect(result.current).toBeUndefined();
  });

  it('5초마다 다시 받는다', async () => {
    const { wrapper, calls } = setup('ADMIN');
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    renderHook(() => useMetrics(), { wrapper });
    await vi.waitFor(() => expect(calls()).toBe(1));

    await vi.advanceTimersByTimeAsync(5_000);
    await vi.waitFor(() => expect(calls()).toBe(2));
  });

  it('구간 키가 빠진 응답도 받는다', async () => {
    const { wrapper } = setup('ADMIN', () =>
      HttpResponse.json({ contract: 'DEMO_UNCONFIRMED', as_of: 1, stages: {} }),
    );
    const { result } = renderHook(() => useMetrics(), { wrapper });

    await waitFor(() => expect(result.current?.stages).toEqual({}));
  });

  it('가정한 형식 표시(contract)가 없는 응답은 받지 않고 비운다', async () => {
    const { wrapper, queryClient, calls } = setup('ADMIN', () => HttpResponse.json(body('V1')));
    const { result } = renderHook(() => useMetrics(), { wrapper });

    await waitFor(() => expect(queryClient.getQueryState(METRICS_KEY)?.status).toBe('error'));
    expect(calls()).toBe(1);
    expect(result.current).toBeUndefined();
  });

  it.each([401, 403, 500])('HTTP %i 이면 다시 보내지 않고 비운다', async (status) => {
    const { wrapper, queryClient, calls } = setup('ADMIN', () =>
      HttpResponse.json({ status }, { status }),
    );
    const { result } = renderHook(() => useMetrics(), { wrapper });

    await waitFor(() => expect(queryClient.getQueryState(METRICS_KEY)?.status).toBe('error'));
    expect(calls()).toBe(1);
    expect(result.current).toBeUndefined();
  });

  it('갱신이 실패하면 받아 둔 값을 지금 값처럼 돌려주지 않는다', async () => {
    let fail = false;
    const { wrapper, queryClient } = setup('ADMIN', () =>
      fail ? HttpResponse.json({}, { status: 500 }) : HttpResponse.json(body()),
    );
    const { result } = renderHook(() => useMetrics(), { wrapper });
    await waitFor(() => expect(result.current).toBeDefined());

    fail = true;
    await act(() => queryClient.refetchQueries({ queryKey: METRICS_KEY }));
    await waitFor(() => expect(result.current).toBeUndefined());

    fail = false;
    await act(() => queryClient.refetchQueries({ queryKey: METRICS_KEY }));
    await waitFor(() => expect(result.current).toBeDefined());
  });
});
