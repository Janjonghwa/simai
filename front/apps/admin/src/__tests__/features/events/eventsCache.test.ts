import { ackErrorMessage } from '@/features/events/ack';
import { requestAck, requestEvents } from '@/features/events/api';
import { bindEventsCache, LIVE_EVENTS_KEY, type LiveEvents } from '@/features/events/eventsCache';
import { ApiError, createHttp, NETWORK_ERROR } from '@/lib/http';
import type { RealtimeListeners } from '@/lib/stomp';
import { riskEventsBetween } from '@/mocks/realtimeScenario';
import { QueryClient } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

// 시나리오의 R2(개구부 근접) 발생·갱신.
const [raised, updated] = riskEventsBetween(0, 40_000)
  .map((m) => m.payload)
  .filter((e) => e.rule_id === 'R2');

function fakeRealtime() {
  const listeners: { [K in keyof RealtimeListeners]: RealtimeListeners[K][] } = {
    riskEvent: [],
    open: [],
  };
  return {
    on<K extends keyof RealtimeListeners>(type: K, listener: RealtimeListeners[K]) {
      listeners[type].push(listener);
      return () => {
        listeners[type] = listeners[type].filter((l) => l !== listener) as never;
      };
    },
    listeners,
  };
}

describe('bindEventsCache', () => {
  it('/topic/events 를 순서대로 캐시에 합친다. 목록을 받기 전이라 loaded 는 false 다', () => {
    const queryClient = new QueryClient();
    const realtime = fakeRealtime();
    const unbind = bindEventsCache(realtime, queryClient);

    realtime.listeners.riskEvent.forEach((l) => l(updated!, 0));
    realtime.listeners.riskEvent.forEach((l) => l(raised!, 0));
    const data = queryClient.getQueryData<LiveEvents>(LIVE_EVENTS_KEY);
    expect(data?.entries.map((e) => e.event.lifecycle)).toEqual(['UPDATED']);
    expect(data?.loaded).toBe(false);

    unbind();
    expect(realtime.listeners.riskEvent).toHaveLength(0);
    expect(realtime.listeners.open).toHaveLength(0);
  });

  it('연결이 다시 열리면 받은 목록도 다시 받을 때까지 loaded 를 내리고 다시 받게 한다', () => {
    const queryClient = new QueryClient();
    const realtime = fakeRealtime();
    bindEventsCache(realtime, queryClient);
    queryClient.setQueryData<LiveEvents>(LIVE_EVENTS_KEY, { entries: [], loaded: true });

    realtime.listeners.open.forEach((l) => l());
    expect(queryClient.getQueryData<LiveEvents>(LIVE_EVENTS_KEY)?.loaded).toBe(false);
    expect(queryClient.getQueryState(LIVE_EVENTS_KEY)?.isInvalidated).toBe(true);
  });

  it('목록을 받기 전에 연결이 열려도 캐시를 만들지 않는다', () => {
    const queryClient = new QueryClient();
    const realtime = fakeRealtime();
    bindEventsCache(realtime, queryClient);
    realtime.listeners.open.forEach((l) => l());
    expect(queryClient.getQueryState(LIVE_EVENTS_KEY)).toBeUndefined();
  });
});

const BASE = 'http://api.test';
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('이벤트 REST', () => {
  it('목록을 받아 검사하고, ACK 는 보던 event_sequence 를 실어 보낸다', async () => {
    let ackBody: unknown;
    server.use(
      http.get(`${BASE}/api/events`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get('size')).toBe('100');
        return HttpResponse.json({ content: [raised], totalElements: 1 });
      }),
      http.post(`${BASE}/api/events/:id/ack`, async ({ request, params }) => {
        expect(params.id).toBe(raised!.event_id);
        ackBody = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const api = createHttp({ baseUrl: BASE });

    const page = await requestEvents(api);
    expect(page.content[0]?.event_id).toBe(raised!.event_id);
    await requestAck(api, raised!.event_id, 2);
    expect(ackBody).toEqual({ event_sequence: 2 });
  });

  it('목록 형식이 다르면 INVALID_RESPONSE 다', async () => {
    server.use(http.get(`${BASE}/api/events`, () => HttpResponse.json([raised])));
    await expect(requestEvents(createHttp({ baseUrl: BASE }))).rejects.toMatchObject({
      kind: 'INVALID_RESPONSE',
    });
  });

  it('ACK 실패 문구: 4xx 서버 문구만, 연결 실패는 따로, 그 밖은 기본 문구', () => {
    expect(ackErrorMessage(new ApiError(409, { serverMessage: '이미 해소된 이벤트입니다' }))).toBe(
      '이미 해소된 이벤트입니다',
    );
    expect(ackErrorMessage(new ApiError(NETWORK_ERROR))).toBe('서버에 연결할 수 없습니다');
    expect(ackErrorMessage(new ApiError(500, { serverMessage: 'NPE' }))).toBe(
      '확인하지 못했습니다',
    );
  });
});
