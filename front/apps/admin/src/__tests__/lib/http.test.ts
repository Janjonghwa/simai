import { ApiError, createHttp, NETWORK_ERROR } from '@/lib/http';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';

const BASE = 'http://api.test';
const okSchema = z.object({ ok: z.literal(true) });

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('createHttp', () => {
  it('토큰이 있으면 Bearer 헤더를 붙이고 응답을 스키마로 검사해 돌려준다', async () => {
    let authorization: string | null = null;
    server.use(
      http.get(`${BASE}/api/ping`, ({ request }) => {
        authorization = request.headers.get('Authorization');
        return HttpResponse.json({ ok: true });
      }),
    );
    const request = createHttp({ baseUrl: `${BASE}/`, getToken: () => 'token-1' });

    await expect(request('/api/ping', { schema: okSchema })).resolves.toEqual({ ok: true });
    expect(authorization).toBe('Bearer token-1');
  });

  it('토큰이 없으면 Authorization 을 붙이지 않고, 본문은 JSON 으로 보낸다', async () => {
    let received: { authorization: string | null; body: unknown } | undefined;
    server.use(
      http.post(`${BASE}/api/echo`, async ({ request }) => {
        received = {
          authorization: request.headers.get('Authorization'),
          body: await request.json(),
        };
        return HttpResponse.json({ ok: true });
      }),
    );
    const request = createHttp({ baseUrl: BASE });

    await request('/api/echo', { method: 'POST', body: { a: 1 }, schema: okSchema });
    expect(received).toEqual({ authorization: null, body: { a: 1 } });
  });

  it('응답이 스키마와 다르면 실패한다', async () => {
    server.use(http.get(`${BASE}/api/ping`, () => HttpResponse.json({ ok: 'yes' })));
    const request = createHttp({ baseUrl: BASE });

    await expect(request('/api/ping', { schema: okSchema })).rejects.toThrow();
  });

  it('오류 응답은 상태와 서버 문구를 담은 ApiError 다', async () => {
    server.use(
      http.get(`${BASE}/api/message`, () =>
        HttpResponse.json({ message: '이미 있다' }, { status: 409 }),
      ),
      http.get(`${BASE}/api/detail`, () =>
        HttpResponse.json({ detail: '형식이 틀렸다' }, { status: 400 }),
      ),
      http.get(`${BASE}/api/empty`, () => new HttpResponse(null, { status: 500 })),
    );
    const request = createHttp({ baseUrl: BASE });

    await expect(request('/api/message', { schema: okSchema })).rejects.toMatchObject({
      status: 409,
      serverMessage: '이미 있다',
    });
    await expect(request('/api/detail', { schema: okSchema })).rejects.toMatchObject({
      status: 400,
      serverMessage: '형식이 틀렸다',
    });
    const error: unknown = await request('/api/empty', { schema: okSchema }).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 500, serverMessage: undefined });
  });

  it('연결이 안 되면 status 0 인 ApiError 다', async () => {
    server.use(http.get(`${BASE}/api/ping`, () => HttpResponse.error()));
    const request = createHttp({ baseUrl: BASE });

    await expect(request('/api/ping', { schema: okSchema })).rejects.toMatchObject({
      status: NETWORK_ERROR,
    });
  });

  it('토큰을 실어 보낸 요청이 401 일 때만 onUnauthorized 를 부른다', async () => {
    server.use(http.get(`${BASE}/api/me`, () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();

    const signedIn = createHttp({ baseUrl: BASE, getToken: () => 'expired', onUnauthorized });
    await expect(signedIn('/api/me', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    const signedOut = createHttp({ baseUrl: BASE, getToken: () => undefined, onUnauthorized });
    await expect(signedOut('/api/me', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('응답을 기다리는 사이 토큰이 바뀌었으면 401 이어도 새 세션을 끝내지 않는다', async () => {
    let token = 'old';
    server.use(
      http.get(`${BASE}/api/me`, () => {
        token = 'new';
        return new HttpResponse(null, { status: 401 });
      }),
    );
    const onUnauthorized = vi.fn();
    const request = createHttp({ baseUrl: BASE, getToken: () => token, onUnauthorized });

    await expect(request('/api/me', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('/ 로 시작하지 않는 경로는 보내지 않는다(토큰이 다른 서버로 가지 않게)', async () => {
    const request = createHttp({ baseUrl: BASE, getToken: () => 'token-1' });

    await expect(request('@evil.example/x', { schema: okSchema })).rejects.toThrow(/\/ 로 시작/);
    await expect(request('//evil.example/x', { schema: okSchema })).rejects.toThrow(/\/ 로 시작/);
  });
});
