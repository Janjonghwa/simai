import { startRealtimeMock } from '@/mocks/realtime';
import type { StompBroker } from '@/mocks/stompBroker';
import { validateMessage, type ViewerHealthPayload } from '@simai/contracts';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

const T0 = 1_760_342_400_000;

let t = T0;
// 연결이 없는 broker 라 STOMP 송출은 하지 않는다. GET /api/health 만 본다.
const broker = {
  connections: 0,
  publish: () => undefined,
  dropAll: () => undefined,
} as unknown as StompBroker;
const mock = startRealtimeMock(broker, { record: (message) => message }, () => t);
const server = setupServer(...mock.handlers);

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  t = T0;
  mock.outage(null);
  mock.sceneMismatch(false);
});
afterAll(() => {
  server.close();
  mock.stop();
});

const get = (authorization: string | null = 'Bearer mock-token.admin') =>
  fetch('http://api.test/api/health', {
    headers: authorization ? { Authorization: authorization } : {},
  });
const payload = async (response: Response) =>
  ((await response.json()) as { payload: ViewerHealthPayload }).payload;

describe('GET /api/health mock', () => {
  it('로그인한 계정이면 역할과 상관없이 viewer_health 메시지를 그대로 준다(계약 통과)', async () => {
    for (const account of ['admin', 'foreman', 'viewer']) {
      const response = await get(`Bearer mock-token.${account}`);
      expect(response.status).toBe(200);
      const checked = validateMessage('viewer_health', await response.json());
      expect(checked.ok ? [] : checked.errors).toEqual([]);
    }
  });

  it('로그인하지 않았거나 실서버 토큰이 만료됐으면 401 이다', async () => {
    expect((await get(null)).status).toBe(401);
    const jwt = (exp: number) =>
      `Bearer h.${btoa(JSON.stringify({ sub: 'u-1', role: 'ADMIN', exp })).replace(/=+$/, '')}.s`;
    expect((await get(jwt(Math.floor(t / 1000) - 1))).status).toBe(401);
    expect((await get(jwt(Math.floor(t / 1000) + 60))).status).toBe(200);
  });

  it('STOMP 흉내와 같은 지금 상태(장애·Scene 불일치)를 준다', async () => {
    mock.outage('tier0');
    t = T0 + 5_000;
    mock.sceneMismatch(true);

    const now = await payload(await get());
    expect(now.remote_state).toBe('CONTROL_HALTED');
    expect(now.scene.mismatch).toBe(true);
  });
});
