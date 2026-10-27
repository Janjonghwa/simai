import { staticObjectBodySchema, type StaticObjectItem } from '@/features/surfaces/types';
import { readMockUser } from '@/mocks/mockUser';
import { SEEDED_AT } from '@/mocks/zones';
import { http, HttpResponse } from 'msw/http';

// 서버 작업면·정적 객체 API(BE -120 geometry 도메인) 대신 쓰는 mock. 서버 검사 규칙(StaticObjectRepository.check)을 따른다.
// 단상은 이벤트 시나리오의 z-02(단상 40cm)와 같은 자리, 책상은 3D 뷰어 하네스의 데모 책상 자리다.

export const MOCK_STATIC_OBJECTS = [
  {
    object_id: 'ws-0a0c0001',
    object_version: 1,
    kind: 'WORK_SURFACE',
    name: '단상',
    obb: { center: [2.5, 7.3, 0.2], size: [3, 2.2, 0.4], yaw_rad: 0 },
    top_height_m: Math.fround(0.4),
    valid_from: SEEDED_AT - 60_000,
    valid_to: null,
  },
  {
    object_id: 'ws-0a0c0002',
    object_version: 1,
    kind: 'WORK_SURFACE',
    name: '책상',
    obb: { center: [5, 4.6, 0.37], size: [1.2, 0.6, 0.74], yaw_rad: -0.2618 },
    top_height_m: Math.fround(0.74),
    valid_from: SEEDED_AT - 60_000,
    valid_to: null,
  },
] satisfies StaticObjectItem[];

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ status, detail, code }, { status });

export interface StaticObjectsMock {
  handlers: ReturnType<typeof http.get>[];
}

/**
 * 작업면·정적 객체 조회·만들기·새 버전. 로그인하지 않았거나 실서버 토큰이 만료면 401, 쓰기는 관리자만(아니면 403)이다.
 * 새 버전은 지금 버전을 닫고 다음 버전을 더한다(서버처럼 고치기 시작한 버전은 보지 않는다). 상태는 이 mock 이 살아 있는 동안만 둔다.
 */
export function createStaticObjectsMock({
  now = Date.now,
}: { now?: () => number } = {}): StaticObjectsMock {
  // 지금 버전과 닫힌 버전. 지금 버전은 valid_to 가 null 이다.
  let versions: StaticObjectItem[] = structuredClone(MOCK_STATIC_OBJECTS);
  let issued = 0;

  const userOf = (request: Request) => {
    const user = readMockUser(request.headers.get('Authorization'));
    return user && (user.expiresAt === undefined || user.expiresAt > now()) ? user : undefined;
  };
  const deny = (request: Request, write: boolean) => {
    const user = userOf(request);
    if (!user) return problem(401, 'UNAUTHORIZED', '로그인이 필요합니다');
    if (write && user.role !== 'ADMIN') return problem(403, 'FORBIDDEN', '권한이 없습니다');
    return undefined;
  };
  const current = () => versions.filter((o) => o.valid_to === null);
  const invalid = () =>
    problem(400, 'INVALID_STATIC_OBJECT', '작업면·정적 객체 값이 올바르지 않습니다');

  /** 서버(StaticObjectRepository.check)와 같은 검사. 통과하면 저장할 칸, 아니면 오류 응답. */
  const check = async (request: Request) => {
    const body = staticObjectBodySchema.safeParse(await request.json().catch(() => undefined));
    if (!body.success) return invalid();
    const { kind, obb, top_height_m: top, top_height_sigma_m: sigma } = body.data;
    if (![...obb.center, ...obb.size, obb.yaw_rad].every(Number.isFinite)) return invalid();
    if (obb.size.some((v) => v <= 0)) return invalid();
    // 서버는 real(float4) 열에 들어간 값으로 상판을 검사하고 그 값을 돌려준다.
    const stored = top === null ? null : Math.fround(top);
    if (stored !== null && !Number.isFinite(stored)) return invalid();
    if (kind === 'WORK_SURFACE' && (stored === null || !(stored > 0))) return invalid();
    if (sigma !== null && (!Number.isFinite(sigma) || sigma < 0)) return invalid();
    const name = body.data.name?.trim() || null;
    return { kind, name, obb, top_height_m: stored, sigma };
  };
  type Fields = Exclude<Awaited<ReturnType<typeof check>>, Response>;
  const itemOf = (id: string, version: number, f: Fields, t: number): StaticObjectItem => ({
    object_id: id,
    object_version: version,
    kind: f.kind,
    name: f.name,
    obb: f.obb,
    top_height_m: f.top_height_m,
    ...(f.sigma === null ? {} : { top_height_sigma_m: f.sigma }),
    valid_from: t,
    valid_to: null,
  });

  const handlers = [
    http.get(
      '*/api/static-objects',
      ({ request }) => deny(request, false) ?? HttpResponse.json(current()),
    ),
    http.get('*/api/static-objects/:objectId/versions', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const list = versions.filter((o) => o.object_id === params.objectId);
      return list.length > 0
        ? HttpResponse.json(list)
        : problem(404, 'STATIC_OBJECT_NOT_FOUND', '작업면·정적 객체를 찾을 수 없습니다');
    }),
    http.post('*/api/static-objects', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await check(request);
      if (fields instanceof Response) return fields;
      // 서버는 작업면이면 ws-, 아니면 so- 뒤에 8 hex 를 고른다.
      const prefix = fields.kind === 'WORK_SURFACE' ? 'ws' : 'so';
      const id = `${prefix}-${(++issued).toString(16).padStart(8, '0')}`;
      const item = itemOf(id, 1, fields, now());
      versions = [...versions, item];
      return HttpResponse.json(item, { status: 201 });
    }),
    http.put('*/api/static-objects/:objectId', async ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await check(request);
      if (fields instanceof Response) return fields;
      const before = current().find((o) => o.object_id === params.objectId);
      if (!before) {
        return versions.some((o) => o.object_id === params.objectId)
          ? problem(409, 'CONCURRENT_EDIT', '다른 요청이 먼저 새 버전을 만들었습니다')
          : problem(404, 'STATIC_OBJECT_NOT_FOUND', '작업면·정적 객체를 찾을 수 없습니다');
      }
      const t = now();
      const item = itemOf(before.object_id, before.object_version + 1, fields, t);
      versions = [...versions.map((o) => (o === before ? { ...o, valid_to: t } : o)), item];
      return HttpResponse.json(item);
    }),
  ];

  return { handlers };
}
