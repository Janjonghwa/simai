import { metricsResponseSchema } from '@/features/system/types';
import { handlers } from '@/mocks/handlers';
import { metricsHandlers } from '@/mocks/metrics';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const server = setupServer(...metricsHandlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());

const get = (authorization?: string) =>
  fetch('http://api.test/api/metrics', {
    headers: authorization ? { Authorization: authorization } : {},
  });

describe('지표 mock', () => {
  it('관리자에게 가정한 형식으로 세 구간(현장 경보·관제 화면·반장 PWA)을 명세 6.6 목표 안의 값으로 답한다', async () => {
    const response = await get('Bearer mock-token.admin');
    expect(response.status).toBe(200);
    const { stages } = metricsResponseSchema.parse(await response.json());
    const remote = stages.capture_to_remote_delivery_ms;
    const p95 = [stages.capture_to_site_alert_ms?.p95, remote?.viewer?.p95, remote?.pwa?.p95];
    const target = [900, 1200, 2500];
    p95.forEach((value, i) => {
      expect(value).toBeGreaterThan(0);
      expect(value).toBeLessThan(target[i]!);
    });
  });

  it('관리자가 아니면 403, 로그인하지 않았으면 401 이다', async () => {
    expect((await get('Bearer mock-token.viewer')).status).toBe(403);
    expect((await get('Bearer mock-token.foreman')).status).toBe(403);
    expect((await get()).status).toBe(401);
  });

  it('개발 서버 mock 목록에 들어 있다', () => {
    expect(handlers).toEqual(expect.arrayContaining(metricsHandlers));
  });
});
