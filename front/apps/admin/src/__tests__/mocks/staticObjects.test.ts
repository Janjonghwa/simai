import { createStaticObjectsMock, MOCK_STATIC_OBJECTS } from '@/mocks/staticObjects';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const T0 = 1_760_342_400_000;
let t = T0;
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  t = T0;
  server.resetHandlers(...createStaticObjectsMock({ now: () => t }).handlers);
});

const call = (method: string, path: string, account: string | null, body?: unknown) =>
  fetch(`http://api.test${path}`, {
    method,
    headers: {
      ...(account ? { Authorization: `Bearer mock-token.${account}` } : {}),
      'Content-Type': 'application/json',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
const surface = (overrides: Record<string, unknown> = {}) => ({
  kind: 'WORK_SURFACE',
  name: '책상',
  obb: { center: [5, 4.6, 0.37], size: [1.2, 0.6, 0.74], yaw_rad: 0 },
  top_height_m: 0.74,
  top_height_sigma_m: null,
  ...overrides,
});
const code = async (response: Response) => [response.status, (await response.json()).code];

describe('작업면·정적 객체 mock(BE -120 geometry 형식)', () => {
  it('로그인한 계정이면 역할과 상관없이 지금 작업면 배열을 주고, 로그인하지 않았으면 401 이다', async () => {
    for (const account of ['admin', 'foreman', 'viewer']) {
      const response = await call('GET', '/api/static-objects', account);
      expect(await response.json()).toEqual(MOCK_STATIC_OBJECTS);
    }
    expect((await call('GET', '/api/static-objects', null)).status).toBe(401);
  });

  it('만들기·고치기는 관리자만 한다', async () => {
    for (const account of ['foreman', 'viewer']) {
      expect((await call('POST', '/api/static-objects', account, surface())).status).toBe(403);
      expect(
        (await call('PUT', '/api/static-objects/ws-0a0c0002', account, surface())).status,
      ).toBe(403);
    }
  });

  it('새 작업면은 ws-, 장애물은 so- 로 시작하는 8 hex ID 의 v1 이고, 상판 높이는 float4 로 돌려준다', async () => {
    const created = await call('POST', '/api/static-objects', 'admin', surface());
    expect(created.status).toBe(201);
    const item = await created.json();
    expect(item).toMatchObject({ object_version: 1, valid_from: T0, valid_to: null });
    expect(item.object_id).toMatch(/^ws-[0-9a-f]{8}$/);
    expect(item.top_height_m).toBe(Math.fround(0.74));
    const obstacle = await (
      await call('POST', '/api/static-objects', 'admin', surface({ kind: 'OBSTACLE' }))
    ).json();
    expect(obstacle.object_id).toMatch(/^so-[0-9a-f]{8}$/);
  });

  it('고치면 지금 버전을 닫고 다음 버전을 더한다(고치기 시작한 버전은 보지 않는다)', async () => {
    t = T0 + 5_000;
    const saved = await call(
      'PUT',
      '/api/static-objects/ws-0a0c0002',
      'admin',
      surface({ name: '교탁', top_height_sigma_m: 0.03 }),
    );
    expect(await saved.json()).toMatchObject({
      object_id: 'ws-0a0c0002',
      object_version: 2,
      name: '교탁',
      top_height_sigma_m: 0.03,
      valid_from: T0 + 5_000,
    });
    const history: { object_version: number; valid_to: number | null }[] = await (
      await call('GET', '/api/static-objects/ws-0a0c0002/versions', 'viewer')
    ).json();
    expect(history.map((v) => [v.object_version, v.valid_to])).toEqual([
      [1, T0 + 5_000],
      [2, null],
    ]);
  });

  it('서버와 같이 검사한다: 종류·크기·상판·표준편차가 틀리면 INVALID_STATIC_OBJECT, 없는 객체는 404', async () => {
    const post = (value: unknown) => call('POST', '/api/static-objects', 'admin', value);
    expect(await code(await post(surface({ kind: 'TABLE' })))).toEqual([
      400,
      'INVALID_STATIC_OBJECT',
    ]);
    expect(
      await code(
        await post(surface({ obb: { center: [1, 1, 0.2], size: [0, 1, 0.4], yaw_rad: 0 } })),
      ),
    ).toEqual([400, 'INVALID_STATIC_OBJECT']);
    expect(await code(await post(surface({ top_height_m: 0 })))).toEqual([
      400,
      'INVALID_STATIC_OBJECT',
    ]);
    expect(await code(await post(surface({ top_height_m: 1e-46 })))).toEqual([
      400,
      'INVALID_STATIC_OBJECT',
    ]);
    expect(await code(await post(surface({ top_height_sigma_m: -0.1 })))).toEqual([
      400,
      'INVALID_STATIC_OBJECT',
    ]);
    // 장애물은 상판 높이가 없어도 된다.
    expect((await post(surface({ kind: 'OBSTACLE', top_height_m: null }))).status).toBe(201);
    expect(
      await code(await call('PUT', '/api/static-objects/ws-none', 'admin', surface())),
    ).toEqual([404, 'STATIC_OBJECT_NOT_FOUND']);
  });
});
