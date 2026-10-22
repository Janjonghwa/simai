import { applyEvents } from '@/features/events/eventsCache';
import { useLiveEvents } from '@/features/events/useLiveEvents';
import { riskEventsBetween } from '@/mocks/realtimeScenario';
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

const BASE = 'http://api.test';
const payloads = riskEventsBetween(0, 40_000).map((m) => m.payload);
const edgeRaised = payloads.find((e) => e.rule_id === 'R2')!;
const zoneRaised = payloads.find((e) => e.rule_id === 'R1' && e.world_ids[0] === 'w-0015')!;

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, wrapper };
}

describe('useLiveEvents', () => {
  it('STOMP 가 먼저 캐시를 채웠어도 목록을 받아 합친다', async () => {
    server.use(http.get(`${BASE}/api/events`, () => HttpResponse.json({ content: [zoneRaised] })));
    const { queryClient, wrapper } = setup();
    applyEvents(queryClient, [edgeRaised]);

    const { result } = renderHook(() => useLiveEvents(), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.entries.map((e) => e.event.rule_id).sort()).toEqual(['R1', 'R2']);
    expect(result.current.failed).toBe(false);
  });

  it('목록을 못 받으면 failed 이고, 그 뒤 STOMP 가 와도 가려지지 않는다', async () => {
    server.use(
      http.get(`${BASE}/api/events`, () => HttpResponse.json({ message: 'down' }, { status: 503 })),
    );
    const { queryClient, wrapper } = setup();
    const { result } = renderHook(() => useLiveEvents(), { wrapper });
    await waitFor(() => expect(result.current.failed).toBe(true));

    act(() => applyEvents(queryClient, [edgeRaised]));
    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    expect(result.current.loaded).toBe(false);
    expect(result.current.failed).toBe(true);
  });
});
