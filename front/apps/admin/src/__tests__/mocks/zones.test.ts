import { deleteZone } from '@/features/zones/api';
import { bodyOf, formOf } from '@/features/zones/zoneForm';
import { readZones } from '@/features/zones/zones';
import { ApiError, createHttp } from '@/lib/http';
import { MOCK_STATIC_OBJECTS } from '@/mocks/staticObjects';
import {
  createZonesMock,
  MOCK_ZONES,
  PUBLISH_FAILURES,
  SEEDED_AT,
  type PublishFailureCode,
  type ZonesMock,
} from '@/mocks/zones';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const T0 = SEEDED_AT;
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

describe('구역 보관(삭제) mock', () => {
  const zoneIds = async () =>
    ((await (await call('GET', '/api/zones', 'viewer')).json()) as { zone_id: string }[]).map(
      (z) => z.zone_id,
    );

  it('보관은 관리자만 한다(로그인하지 않았으면 401, 그 밖의 역할은 403)', async () => {
    expect((await call('DELETE', '/api/zones/z-04', null)).status).toBe(401);
    for (const account of ['foreman', 'viewer']) {
      expect((await call('DELETE', '/api/zones/z-04', account)).status).toBe(403);
    }
    expect(await zoneIds()).toEqual(['z-04', 'z-02']);
  });

  it('보관하면 204(본문 없음)이고 목록에서 빠지지만 이력은 남고 지금 버전만 닫힌다', async () => {
    t = T0 + 5_000;
    const response = await call('DELETE', '/api/zones/z-04', 'admin');
    expect(response.status).toBe(204);
    expect(await response.text()).toBe('');
    expect(await zoneIds()).toEqual(['z-02']);
    const history: { zone_version: number; valid_to: number | null }[] = await (
      await call('GET', '/api/zones/z-04/versions', 'viewer')
    ).json();
    expect(history.map((v) => [v.zone_version, v.valid_to])).toEqual([[1, T0 + 5_000]]);
  });

  it('다시 보관해도 204 이고 닫은 시각은 그대로이며, 없는 구역은 404 이다', async () => {
    t = T0 + 5_000;
    await call('DELETE', '/api/zones/z-04', 'admin');
    t = T0 + 9_000;
    expect((await call('DELETE', '/api/zones/z-04', 'admin')).status).toBe(204);
    const history: { valid_to: number | null }[] = await (
      await call('GET', '/api/zones/z-04/versions', 'viewer')
    ).json();
    expect(history.map((v) => v.valid_to)).toEqual([T0 + 5_000]);
    const missing = await call('DELETE', '/api/zones/z-99', 'admin');
    expect([missing.status, (await missing.json()).code]).toEqual([404, 'ZONE_NOT_FOUND']);
  });

  it('보관한 구역은 고칠 수 없다(404). 고쳐 가며 쌓은 버전도 모두 이력에 남고, 같은 구역은 되살리지 않는다', async () => {
    await call('PUT', '/api/zones/z-04', 'admin', body('A'));
    await call('DELETE', '/api/zones/z-04', 'admin');
    const put = await call('PUT', '/api/zones/z-04', 'admin', body('B'));
    expect([put.status, (await put.json()).code]).toEqual([404, 'ZONE_NOT_FOUND']);
    expect(await zoneIds()).toEqual(['z-02']);
    const history: { zone_version: number }[] = await (
      await call('GET', '/api/zones/z-04/versions', 'viewer')
    ).json();
    expect(history.map((v) => v.zone_version)).toEqual([1, 2]);
    // 새로 만들면 다른 ID 의 v1 이다.
    const created = await (await call('POST', '/api/zones', 'admin', body('새'))).json();
    expect(created.zone_id).not.toBe('z-04');
    expect(created.zone_version).toBe(1);
  });

  it('고치기도 서버처럼 검사한다: 넓이 없는 다각형 INVALID_POLYGON, 높이가 없는 VOLUME_3D INVALID_Z_RANGE', async () => {
    const code = async (response: Response) => [response.status, (await response.json()).code];
    const line = {
      ...body('x'),
      polygon_xy: [
        [0, 0],
        [1, 0],
        [2, 0],
      ],
    };
    expect(await code(await call('PUT', '/api/zones/z-04', 'admin', line))).toEqual([
      400,
      'INVALID_POLYGON',
    ]);
    expect(
      await code(await call('PUT', '/api/zones/z-04', 'admin', { ...body('x'), z_min_m: null })),
    ).toEqual([400, 'INVALID_Z_RANGE']);
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

describe('구성 묶음 항목(items)', () => {
  interface Item {
    item_type: string;
    item_id: string;
    item_version: number;
    kind?: string;
  }
  const itemsOf = async (version?: string): Promise<Item[]> => {
    const response = await call(
      'GET',
      version ? `/api/config-bundles/${version}` : '/api/config-bundles',
      'viewer',
    );
    const json = await response.json();
    return (version ? json : json[0]).items;
  };
  const publish = async () => (await call('POST', '/api/config-bundles', 'admin', {})).json();

  it('처음 묶음(cfg-12)은 구역 둘·규칙·작업면 둘을 싣고 MOCK_ZONES·MOCK_STATIC_OBJECTS 와 같다', async () => {
    const items = await itemsOf();
    expect(items).toEqual([
      { item_type: 'ZONE', item_id: 'z-04', item_version: 1 },
      { item_type: 'ZONE', item_id: 'z-02', item_version: 1 },
      { item_type: 'RULE', item_id: 'R2', item_version: 1 },
      { item_type: 'STATIC_OBJECT', item_id: 'ws-0a0c0001', item_version: 1, kind: 'WORK_SURFACE' },
      { item_type: 'STATIC_OBJECT', item_id: 'ws-0a0c0002', item_version: 1, kind: 'WORK_SURFACE' },
    ]);
    expect(items.filter((i) => i.item_type === 'ZONE').map((i) => i.item_id)).toEqual(
      MOCK_ZONES.map((z) => z.zone_id),
    );
    expect(
      items
        .filter((i) => i.item_type === 'STATIC_OBJECT')
        .map((i) => [i.item_id, i.item_version, i.kind]),
    ).toEqual(MOCK_STATIC_OBJECTS.map((o) => [o.object_id, o.object_version, o.kind]));
    // 단건 조회에도 같은 항목이 온다.
    expect(await itemsOf('cfg-12')).toEqual(items);
  });

  it('발행 응답·단건·목록 모두에 items 가 있고, 발행하는 묶음은 그때 유효한 구역(새 버전·새 구역·보관 반영)을 싣는다', async () => {
    await call('PUT', '/api/zones/z-04', 'admin', body('A'));
    const created = await (await call('POST', '/api/zones', 'admin', body('새'))).json();
    await call('DELETE', '/api/zones/z-02', 'admin');
    const published = await publish();
    const expected = [
      { item_type: 'ZONE', item_id: 'z-04', item_version: 2 },
      { item_type: 'ZONE', item_id: created.zone_id, item_version: 1 },
      { item_type: 'RULE', item_id: 'R2', item_version: 1 },
    ];
    expect(published.items).toEqual(expected);
    expect(await itemsOf('cfg-13')).toEqual(expected);
    expect(await itemsOf()).toEqual(expected);
  });

  it('이미 발행한 묶음의 항목은 구역을 고치거나 보관해도 바뀌지 않는다', async () => {
    await call('PUT', '/api/zones/z-04', 'admin', body('A'));
    await call('DELETE', '/api/zones/z-02', 'admin');
    const before = await itemsOf('cfg-12');
    expect(before).toContainEqual({ item_type: 'ZONE', item_id: 'z-02', item_version: 1 });
    await publish();
    expect(await itemsOf('cfg-12')).toEqual(before);
    const [, older] = await (await call('GET', '/api/config-bundles', 'viewer')).json();
    expect(older.items).toEqual(before);
  });

  it('작업면은 mock 이 들고 있지 않아 staticObjects 로 받은 지금 작업면을 싣는다(종류와 버전은 그 객체의 것이다). 주지 않으면 싣지 않는다', async () => {
    const obstacle = {
      ...MOCK_STATIC_OBJECTS[0],
      object_id: 'so-00000001',
      object_version: 3,
      kind: 'OBSTACLE' as const,
    };
    mock = createZonesMock({
      now: () => t,
      ackDelayMs: 3_000,
      staticObjects: () => [MOCK_STATIC_OBJECTS[1], obstacle],
    });
    server.resetHandlers(...mock.handlers);
    expect((await publish()).items).toEqual([
      { item_type: 'ZONE', item_id: 'z-04', item_version: 1 },
      { item_type: 'ZONE', item_id: 'z-02', item_version: 1 },
      { item_type: 'RULE', item_id: 'R2', item_version: 1 },
      { item_type: 'STATIC_OBJECT', item_id: 'ws-0a0c0002', item_version: 1, kind: 'WORK_SURFACE' },
      { item_type: 'STATIC_OBJECT', item_id: 'so-00000001', item_version: 3, kind: 'OBSTACLE' },
    ]);

    mock = createZonesMock({ now: () => t, ackDelayMs: 3_000 });
    server.resetHandlers(...mock.handlers);
    const bare = (await publish()).items as Item[];
    expect(bare.filter((i) => i.item_type === 'STATIC_OBJECT')).toEqual([]);
  });
});

describe('발행 실패 흉내(failNextPublish)', () => {
  const publish = (account = 'admin') => call('POST', '/api/config-bundles', account, {});
  const codes = Object.keys(PUBLISH_FAILURES) as PublishFailureCode[];

  it('기본은 다음 발행을 409 CALIBRATION_MISSING 으로 거절하고, 묶음은 만들지 않으며 그 다음 발행은 된다', async () => {
    mock.failNextPublish();
    const failed = await publish();
    expect([failed.status, (await failed.json()).code]).toEqual([409, 'CALIBRATION_MISSING']);
    expect(mock.config().published_version).toBe('cfg-12');
    expect((await publish()).status).toBe(201);
    expect(mock.config().published_version).toBe('cfg-13');
  });

  it.each(codes)('%s 는 서버의 오류 상태와 code 로 거절한다', async (code) => {
    mock.failNextPublish(code);
    const failed = await publish();
    expect(failed.status).toBe(code === 'PUBLIC_URL_NOT_SET' ? 503 : 409);
    expect(await failed.json()).toMatchObject({ status: failed.status, code });
  });

  it('로그인하지 않았거나 권한이 없는 요청은 거절을 쓰지 않는다. 발행 요청 하나만 거절한다', async () => {
    mock.failNextPublish('RULES_MISSING');
    expect((await call('POST', '/api/config-bundles', null, {})).status).toBe(401);
    expect((await publish('viewer')).status).toBe(403);
    const failed = await publish();
    expect([failed.status, (await failed.json()).code]).toEqual([409, 'RULES_MISSING']);
    expect((await publish()).status).toBe(201);
  });
});

describe('deleteZone(REST 도우미)', () => {
  const http = (account: string) =>
    createHttp({ baseUrl: 'http://api.test', getToken: () => `mock-token.${account}` });

  it('본문 없는 204 를 성공으로 읽고 구역을 보관한다. 다시 불러도 성공이다', async () => {
    await expect(deleteZone(http('admin'), 'z-04')).resolves.toBeUndefined();
    await expect(deleteZone(http('admin'), 'z-04')).resolves.toBeUndefined();
    const list = await (await call('GET', '/api/zones', 'viewer')).json();
    expect(readZones(list).map((z) => z.zoneId)).toEqual(['z-02']);
  });

  it('권한이 없으면 403, 없는 구역은 404(code)로 던진다', async () => {
    await expect(deleteZone(http('foreman'), 'z-04')).rejects.toMatchObject({ status: 403 });
    const error: unknown = await deleteZone(http('admin'), 'z-99').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: 'ZONE_NOT_FOUND' });
  });
});
