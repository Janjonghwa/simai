import { bodyOf, formOf } from '@/features/zones/zoneForm';
import { readZones } from '@/features/zones/zones';
import { createZonesMock, MOCK_ZONES, type ZonesMock } from '@/mocks/zones';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const T0 = 1_760_342_400_000;
let t = T0;
let mock: ZonesMock;
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  t = T0;
  mock = createZonesMock({ now: () => t, ackDelayMs: 3_000 });
  server.resetHandlers(...mock.handlers);
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
const z04 = readZones(MOCK_ZONES)[0]!;
const body = (name: string) => bodyOf(z04, { ...formOf(z04), name }, null);

describe('구역 mock(BE -120 형식)', () => {
  it('로그인한 계정이면 역할과 상관없이 지금 구역 배열을 주고, 로그인하지 않았으면 401 이다', async () => {
    for (const account of ['admin', 'foreman', 'viewer']) {
      const response = await call('GET', '/api/zones', account);
      expect(await response.json()).toEqual(MOCK_ZONES);
    }
    expect((await call('GET', '/api/zones', null)).status).toBe(401);
  });

  it('고치기·만들기는 관리자만 한다', async () => {
    for (const account of ['foreman', 'viewer']) {
      expect((await call('PUT', '/api/zones/z-04', account, body('x'))).status).toBe(403);
      expect((await call('POST', '/api/zones', account, body('x'))).status).toBe(403);
    }
  });

  it('고치면 지금 버전을 닫고 다음 버전을 더한다(서버처럼 고치기 시작한 버전은 보지 않는다)', async () => {
    t = T0 + 5_000;
    const first = await call('PUT', '/api/zones/z-04', 'admin', body('A'));
    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({
      zone_id: 'z-04',
      zone_version: 2,
      name: 'A',
      valid_from: T0 + 5_000,
      valid_to: null,
    });
    const second = await call('PUT', '/api/zones/z-04', 'admin', body('B'));
    expect((await second.json()).zone_version).toBe(3);

    const list = await (await call('GET', '/api/zones', 'viewer')).json();
    expect(readZones(list).find((z) => z.zoneId === 'z-04')).toMatchObject({
      version: 3,
      name: 'B',
    });
    const history: { zone_version: number; valid_to: number | null }[] = await (
      await call('GET', '/api/zones/z-04/versions', 'viewer')
    ).json();
    expect(history.map((v) => [v.zone_version, v.valid_to])).toEqual([
      [1, T0 + 5_000],
      [2, T0 + 5_000],
      [3, null],
    ]);
  });

  it('새 구역은 서버가 정한 z-<8 hex> ID 의 v1 이다', async () => {
    const created = await call('POST', '/api/zones', 'admin', body('새 구역'));
    expect(created.status).toBe(201);
    const zone = await created.json();
    expect(zone).toMatchObject({ zone_version: 1, valid_to: null });
    expect(zone.zone_id).toMatch(/^z-[0-9a-f]{8}$/);
  });

  it('서버와 같이 검사한다: 이름·유형이 비면 INVALID_ZONE, 높이가 맞지 않으면 INVALID_Z_RANGE, 넓이 없는 다각형은 INVALID_POLYGON, 없는 구역은 404', async () => {
    const code = async (response: Response) => [response.status, (await response.json()).code];
    const post = (value: unknown) => call('POST', '/api/zones', 'admin', value);
    expect(await code(await post({ ...body('x'), zone_kind: ' ' }))).toEqual([400, 'INVALID_ZONE']);
    expect(await code(await post({ ...body('x'), z_max_m: null }))).toEqual([
      400,
      'INVALID_Z_RANGE',
    ]);
    expect(await code(await post({ ...body('x'), z_min_m: 3, z_max_m: 1 }))).toEqual([
      400,
      'INVALID_Z_RANGE',
    ]);
    const line = {
      ...body('x'),
      polygon_xy: [
        [0, 0],
        [1, 0],
        [2, 0],
      ],
    };
    expect(await code(await post(line))).toEqual([400, 'INVALID_POLYGON']);
    expect(await code(await call('PUT', '/api/zones/z-99', 'admin', body('x')))).toEqual([
      404,
      'ZONE_NOT_FOUND',
    ]);
  });
});

describe('구성 묶음 mock(BE -120 형식)', () => {
  const latest = async () => (await (await call('GET', '/api/config-bundles', 'viewer')).json())[0];

  it('처음에는 cfg-12 가 현장에 적용되어 있다', async () => {
    expect(await latest()).toMatchObject({ config_version: 'cfg-12', status: 'APPLIED' });
    expect(mock.config()).toEqual({
      published_version: 'cfg-12',
      applied_version: 'cfg-12',
      sync: 'CURRENT',
    });
    expect((await call('GET', '/api/config-bundles/cfg-99', 'viewer')).status).toBe(404);
  });

  it('발행하면 적용 대기(PENDING)인 다음 묶음을 주고, Tier 0 ACK 뒤에는 현장 적용됨(APPLIED)이다', async () => {
    t = T0 + 1_000;
    const published = await call('POST', '/api/config-bundles', 'admin', {});
    expect(published.status).toBe(201);
    expect(await published.json()).toMatchObject({
      config_version: 'cfg-13',
      previous_config_version: 'cfg-12',
      issued_at: T0 + 1_000,
      status: 'PENDING',
      applied_at: null,
      last_ack: null,
    });
    expect(mock.config()).toMatchObject({
      published_version: 'cfg-13',
      applied_version: 'cfg-12',
      sync: 'PENDING',
    });
    t = T0 + 4_000;
    expect(await latest()).toMatchObject({
      config_version: 'cfg-13',
      status: 'APPLIED',
      applied_at: T0 + 4_000,
      last_ack: { active_config_version: 'cfg-13' },
    });
    expect(mock.config()).toMatchObject({ applied_version: 'cfg-13', sync: 'CURRENT' });
    const [, older] = await (await call('GET', '/api/config-bundles', 'viewer')).json();
    expect(older).toMatchObject({
      config_version: 'cfg-12',
      last_ack: { active_config_version: 'cfg-12' },
    });
  });

  it('Tier 0 가 거부하면 REJECTED 이고 이전 묶음을 계속 쓴다. 다음 발행은 다시 받는다', async () => {
    mock.rejectNextBundle();
    await call('POST', '/api/config-bundles', 'admin', {});
    t = T0 + 3_000;
    expect(await latest()).toMatchObject({
      config_version: 'cfg-13',
      status: 'REJECTED',
      last_ack: { active_config_version: 'cfg-12', reject_reason: 'VALIDATION_FAILED' },
    });
    expect(mock.config()).toEqual({
      published_version: 'cfg-13',
      applied_version: 'cfg-12',
      sync: 'STALE',
    });
    await call('POST', '/api/config-bundles', 'admin', {});
    t = T0 + 6_000;
    expect(await latest()).toMatchObject({ config_version: 'cfg-14', status: 'APPLIED' });
  });

  it('발행은 관리자만 하고, 규칙 프로필이 틀리면 400 이다', async () => {
    for (const account of ['foreman', 'viewer']) {
      expect((await call('POST', '/api/config-bundles', account, {})).status).toBe(403);
    }
    const bad = await call('POST', '/api/config-bundles', 'admin', { rule_profile: 'X' });
    expect([bad.status, (await bad.json()).code]).toEqual([400, 'INVALID_RULE_PROFILE']);
    expect(mock.config().published_version).toBe('cfg-12');
  });
});
