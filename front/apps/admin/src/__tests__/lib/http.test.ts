import { ApiError, createHttp, NETWORK_ERROR } from '@/lib/http';
import { delay } from 'msw';
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

const failure = (promise: Promise<unknown>) =>
  promise.then(
    () => undefined,
    (e: unknown) => e,
  );

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

  it('204 처럼 본문이 없으면 undefined 를 스키마로 검사한다', async () => {
    server.use(http.post(`${BASE}/api/ack`, () => new HttpResponse(null, { status: 204 })));
    const request = createHttp({ baseUrl: BASE });

    await expect(request('/api/ack', { method: 'POST', schema: z.undefined() })).resolves.toBe(
      undefined,
    );
  });

  it('성공 상태인데 본문이 스키마와 다르거나 JSON 이 아니면 INVALID_RESPONSE 다', async () => {
    server.use(
      http.get(`${BASE}/api/shape`, () => HttpResponse.json({ ok: 'yes' })),
      http.get(`${BASE}/api/html`, () => HttpResponse.html('<html>점검 중</html>')),
      http.get(`${BASE}/api/empty`, () => new HttpResponse(null, { status: 200 })),
    );
    const request = createHttp({ baseUrl: BASE });

    for (const path of ['/api/shape', '/api/html', '/api/empty']) {
      const error = await failure(request(path, { schema: okSchema }));
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ status: 200, kind: 'INVALID_RESPONSE' });
      expect((error as ApiError).cause).toBeDefined();
    }
  });

  it('오류 응답은 상태와 서버 문구를 담은 ApiError 이고, 서버 문구는 message 에 넣지 않는다', async () => {
    server.use(
      http.get(`${BASE}/api/message`, () =>
        HttpResponse.json({ message: '이미 있다' }, { status: 409 }),
      ),
      http.get(`${BASE}/api/detail`, () =>
        HttpResponse.json({ detail: '형식이 틀렸다' }, { status: 400 }),
      ),
      http.get(`${BASE}/api/crash`, () =>
        HttpResponse.json({ message: 'NullPointerException at Foo.java:12' }, { status: 500 }),
      ),
      http.get(`${BASE}/api/empty`, () => new HttpResponse(null, { status: 500 })),
    );
    const request = createHttp({ baseUrl: BASE });

    await expect(request('/api/message', { schema: okSchema })).rejects.toMatchObject({
      status: 409,
      kind: 'HTTP',
      serverMessage: '이미 있다',
    });
    await expect(request('/api/detail', { schema: okSchema })).rejects.toMatchObject({
      status: 400,
      serverMessage: '형식이 틀렸다',
    });
    const crash = await failure(request('/api/crash', { schema: okSchema }));
    expect(crash).toBeInstanceOf(ApiError);
    expect((crash as ApiError).message).not.toContain('NullPointerException');
    await expect(request('/api/empty', { schema: okSchema })).rejects.toMatchObject({
      status: 500,
      serverMessage: undefined,
    });
  });

  it('연결이 안 되면 status 0, kind NETWORK 이고 원인을 cause 에 남긴다', async () => {
    server.use(http.get(`${BASE}/api/ping`, () => HttpResponse.error()));
    const request = createHttp({ baseUrl: BASE });

    const error = await failure(request('/api/ping', { schema: okSchema }));
    expect(error).toMatchObject({ status: NETWORK_ERROR, kind: 'NETWORK' });
    expect((error as ApiError).cause).toBeDefined();
  });

  it('정한 시간 안에 답이 없으면 TIMEOUT 이다', async () => {
    // Node 의 msw 가로채기는 시간 초과로 끊긴 요청을 500 응답으로 바꿔 돌려준다(브라우저 fetch 와 다르다).
    // 그래서 신호가 끊기면 실패하는 fetch 로 바꿔 시험한다.
    vi.stubGlobal(
      'fetch',
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason));
        }),
    );
    try {
      const request = createHttp({ baseUrl: BASE, timeoutMs: 30 });

      await expect(request('/api/slow', { schema: okSchema })).rejects.toMatchObject({
        status: NETWORK_ERROR,
        kind: 'TIMEOUT',
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('부르는 쪽이 취소하면 ApiError 로 바꾸지 않고 그대로 던진다', async () => {
    server.use(
      http.get(`${BASE}/api/slow`, async () => {
        await delay(500);
        return HttpResponse.json({ ok: true });
      }),
    );
    const request = createHttp({ baseUrl: BASE });
    const controller = new AbortController();

    const pending = failure(request('/api/slow', { schema: okSchema, signal: controller.signal }));
    controller.abort();
    const error = await pending;
    expect(error).not.toBeInstanceOf(ApiError);
    expect((error as Error).name).toBe('AbortError');
  });

  it('본문을 JSON 으로 만들 수 없으면 연결 실패가 아니라 그 오류를 그대로 던진다', async () => {
    const request = createHttp({ baseUrl: BASE });

    const error = await failure(
      request('/api/echo', { method: 'POST', body: { big: 1n }, schema: okSchema }),
    );
    expect(error).toBeInstanceOf(TypeError);
  });

  it('토큰을 실어 보낸 요청이 401 이면 그 토큰을 넘겨 onUnauthorized 를 부른다', async () => {
    server.use(http.get(`${BASE}/api/me`, () => new HttpResponse(null, { status: 401 })));
    const onUnauthorized = vi.fn();

    const signedIn = createHttp({ baseUrl: BASE, getToken: () => 'expired', onUnauthorized });
    await expect(signedIn('/api/me', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledExactlyOnceWith('expired');

    const signedOut = createHttp({ baseUrl: BASE, getToken: () => undefined, onUnauthorized });
    await expect(signedOut('/api/me', { schema: okSchema })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('/ 로 시작하지 않는 경로는 보내지 않는다(토큰이 다른 서버로 가지 않게)', async () => {
    const request = createHttp({ baseUrl: BASE, getToken: () => 'token-1' });

    await expect(request('@evil.example/x', { schema: okSchema })).rejects.toThrow(/\/ 로 시작/);
    await expect(request('//evil.example/x', { schema: okSchema })).rejects.toThrow(/\/ 로 시작/);
  });
});
