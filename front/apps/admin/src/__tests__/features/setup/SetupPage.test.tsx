import { SetupPage } from '@/features/setup/SetupPage';
import { createRulesMock } from '@/mocks/rules';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.foreman' }) };
});
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => vi.fn() }));

const server = setupServer(...createRulesMock({ profile: 'CLASSROOM_TEST' }).handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(cleanup);
afterAll(() => server.close());

describe('설치 · 보정 화면의 규칙 칸', () => {
  it('보정 단계 아래에 규칙 현황을 보인다(관리자가 아닌 역할도)', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SetupPage />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole('table')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '규칙' })).toBeTruthy();
    expect(screen.getByText('시험 프로파일')).toBeTruthy();
    // 보정 화면 자체도 그대로 있다.
    expect(screen.getByRole('list', { name: '보정 단계' })).toBeTruthy();
  });
});
