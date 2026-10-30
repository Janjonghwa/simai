import { RulesPanel } from '@/features/rules/RulesPanel';
import { createRulesMock } from '@/mocks/rules';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.viewer' }) };
});

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RulesPanel />
    </QueryClientProvider>,
  );
}

describe('규칙 현황 칸', () => {
  it('운영 프로파일: 구성 판과 R1~R10 을 보이고 시험 표시는 없다', async () => {
    server.use(...createRulesMock().handlers);
    show();
    const table = await screen.findByRole('table');
    expect(screen.getByText('OPERATION')).toBeTruthy();
    expect(screen.getByText('cfg-3')).toBeTruthy();
    expect(screen.queryByText('시험 프로파일')).toBeNull();
    expect(screen.queryByText('발행 대기 판')).toBeNull();
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(10);
    // R2: 활성, 기본 등급 위험(CRITICAL).
    const r2 = within(rows[1]!);
    expect(r2.getByText('활성')).toBeTruthy();
    expect(r2.getByText('위험')).toBeTruthy();
    // 확장 규칙 R8 은 비활성.
    expect(within(rows[7]!).getByText('비활성')).toBeTruthy();
    // R6 은 자기 판이 없고 R1 에 포함된다.
    expect(within(rows[5]!).getByText('R1 에 포함')).toBeTruthy();
  });

  it('교실 시험 프로파일이면 시험 프로파일 표시를 한다', async () => {
    server.use(...createRulesMock({ profile: 'CLASSROOM_TEST' }).handlers);
    show();
    await screen.findByRole('table');
    expect(screen.getByText('시험 프로파일')).toBeTruthy();
    expect(screen.getByText('CLASSROOM_TEST')).toBeTruthy();
  });

  it('발행만 하고 적용 전인 구성 판은 발행 대기 판으로 보인다', async () => {
    server.use(...createRulesMock({ pendingConfigVersion: 'cfg-4' }).handlers);
    show();
    await screen.findByRole('table');
    expect(screen.getByText('발행 대기 판')).toBeTruthy();
    expect(screen.getByText('cfg-4')).toBeTruthy();
  });

  it('받지 못하면 알리고 표는 그리지 않는다', async () => {
    server.use(http.get('*/api/rules', () => HttpResponse.json({ code: 'X' }, { status: 500 })));
    show();
    expect((await screen.findByRole('alert')).textContent).toBe('규칙을 받지 못했습니다');
    expect(screen.queryByRole('table')).toBeNull();
  });
});
