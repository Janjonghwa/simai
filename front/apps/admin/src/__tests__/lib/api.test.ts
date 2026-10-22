import { useSessionStore } from '@/store/session.store';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';

// lib/api.ts 의 실제 배선을 본다. env 만 바꾼다(import.meta.env 를 읽지 않게).
vi.mock('@/lib/env', () => ({ env: { VITE_API_BASE_URL: 'http://api.test' } }));
const { api } = await import('@/lib/api');

const BASE = 'http://api.test';
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const okSchema = z.object({ ok: z.literal(true) });
const refreshed = () =>
  HttpResponse.json({ accessToken: 't1', user: { id: 'u-1', role: 'ADMIN' } });

describe('lib/api 배선', () => {
  beforeEach(() => {
    useSessionStore.setState(useSessionStore.getInitialState(), true);
    useSessionStore.getState().signIn({ accessToken: 't0', user: { id: 'u-1', role: 'ADMIN' } });
  });

  it('앱 요청이 401 이면 토큰 없이 쿠키로 갱신하고 새 토큰으로 다시 보낸다', async () => {
    const refreshAuthorization: (string | null)[] = [];
    const dataAuthorization: (string | null)[] = [];
    server.use(
      http.post(`${BASE}/api/auth/refresh`, ({ request }) => {
        refreshAuthorization.push(request.headers.get('Authorization'));
        return refreshed();
      }),
      http.get(`${BASE}/api/data`, ({ request }) => {
        const authorization = request.headers.get('Authorization');
        dataAuthorization.push(authorization);
        return authorization === 'Bearer t1'
          ? HttpResponse.json({ ok: true })
          : new HttpResponse(null, { status: 401 });
      }),
    );

    await expect(api('/api/data', { schema: okSchema })).resolves.toEqual({ ok: true });
    // 만료된 Bearer 를 갱신에 실으면 서버가 쿠키를 보기 전에 401 로 거절한다.
    expect(refreshAuthorization).toEqual([null]);
    expect(dataAuthorization).toEqual(['Bearer t0', 'Bearer t1']);
    expect(useSessionStore.getState().session?.accessToken).toBe('t1');
  });

  it('갱신한 토큰으로 다시 보낸 요청도 401 이면 세션을 끝낸다', async () => {
    server.use(
      http.post(`${BASE}/api/auth/refresh`, refreshed),
      http.get(`${BASE}/api/data`, () => new HttpResponse(null, { status: 401 })),
    );

    await expect(api('/api/data', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(useSessionStore.getState().session).toBeUndefined();
  });
});
