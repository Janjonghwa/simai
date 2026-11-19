import { parseEnv } from '@/lib/parseEnv';
import { handlers } from '@/mocks/handlers';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, expect, it } from 'vitest';

const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

it('목업 빌드의 가상 주소는 운영 환경 검사와 REST 매칭을 통과한다', async () => {
  const env = parseEnv(
    {
      VITE_API_BASE_URL: 'https://mock.simai.invalid',
      VITE_STOMP_URL: 'wss://mock.simai.invalid/ws',
      VITE_API_MOCK: 'true',
      VITE_API_REAL: '',
    },
    { production: true },
  );
  expect(env.VITE_API_REAL).toEqual([]);
  const response = await fetch(`${env.VITE_API_BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'demo', password: 'demo' }),
  });
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({ user: { id: 'demo' } });
});
