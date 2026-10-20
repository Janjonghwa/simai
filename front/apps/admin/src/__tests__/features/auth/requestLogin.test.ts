import { requestLogin } from '@/features/auth/api';
import { createHttp } from '@/lib/http';
import { MOCK_ACCOUNTS } from '@/mocks/accounts';
import { handlers } from '@/mocks/handlers';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

// 개발 서버 mock 응답이 로그인 응답 스키마를 통과하는지 본다.
const server = setupServer(...handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

const client = createHttp({ baseUrl: 'http://api.test' });

describe('requestLogin', () => {
  it('mock 계정으로 로그인하면 토큰과 역할을 받는다', async () => {
    for (const account of MOCK_ACCOUNTS) {
      const session = await requestLogin(client, {
        username: account.username,
        password: account.password,
      });
      expect(session.accessToken).not.toBe('');
      expect(session.user).toEqual({ id: account.id, role: account.role });
    }
  });

  it('비밀번호가 틀리면 401 이다', async () => {
    const [account] = MOCK_ACCOUNTS;
    await expect(
      requestLogin(client, { username: account.username, password: 'wrong' }),
    ).rejects.toMatchObject({ status: 401 });
  });
});
