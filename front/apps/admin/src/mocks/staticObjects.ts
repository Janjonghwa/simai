import {
  staticObjectBodySchema,
  type StaticObjectBody,
  type StaticObjectItem,
} from '@/features/surfaces/types';
import {
  AUTH_ERRORS,
  created,
  createDeny,
  errorsOf,
  hexId,
  noContent,
  SEEDED_VALID_FROM,
} from '@/mocks/zones';
import { http, HttpResponse } from 'msw/http';

// 서버 작업면·정적 객체 API(BE -120 geometry 도메인) 대신 쓰는 mock. 서버 검사 규칙(StaticObjectRepository.check)을 따른다.
// 단상은 이벤트 시나리오의 z-02(단상 구역)와 같은 자리, 책상은 3D 뷰어 하네스의 데모 책상 자리다.
// 오류 응답·인증 검사·ID 도우미는 구역 mock(mocks/zones)과 함께 쓴다.

/** 작업면 ID 접두. 그 밖의 정적 객체(장애물)는 so- 다. */
const WORK_SURFACE_ID_PREFIX = 'ws';
const OBSTACLE_ID_PREFIX = 'so';

const ERRORS = {
  ...AUTH_ERRORS,
  INVALID_STATIC_OBJECT: { status: 400, detail: '작업면·정적 객체 값이 올바르지 않습니다' },
  STATIC_OBJECT_NOT_FOUND: { status: 404, detail: '작업면·정적 객체를 찾을 수 없습니다' },
};
const fail = errorsOf(ERRORS);

export const MOCK_STATIC_OBJECTS = [
  {
    object_id: 'ws-0a0c0001',
    object_version: 1,
    kind: 'WORK_SURFACE',
    name: '단상 작업면',
    obb: { center: [2.5, 7.3, 0.2], size: [3, 2.2, 0.4], yaw_rad: 0 },
    top_height_m: Math.fround(0.4),
    valid_from: SEEDED_VALID_FROM,
    valid_to: null,
  },
  {
    object_id: 'ws-0a0c0002',
    object_version: 1,
    kind: 'WORK_SURFACE',
    name: '책상',
    obb: { center: [5, 4.6, 0.37], size: [1.2, 0.6, 0.74], yaw_rad: -0.2618 },
    top_height_m: Math.fround(0.74),
    valid_from: SEEDED_VALID_FROM,
    valid_to: null,
  },
] satisfies StaticObjectItem[];

/** 중심·크기·회전이 모두 유한한 수이고 크기가 양수다. */
const isValidObb = ({ center, size, yaw_rad }: StaticObjectBody['obb']) =>
  [...center, ...size, yaw_rad].every(Number.isFinite) && size.every((v) => v > 0);

/** 상판 높이(float4 로 저장한 값): 있으면 유한해야 하고, 작업면은 반드시 있고 양수여야 한다. */
function isValidTop(kind: StaticObjectBody['kind'], stored: number | null): boolean {
  if (stored === null) return kind !== 'WORK_SURFACE';
  return Number.isFinite(stored) && (kind !== 'WORK_SURFACE' || stored > 0);
}

const isValidSigma = (sigma: number | null) =>
  sigma === null || (Number.isFinite(sigma) && sigma >= 0);

/** 서버(StaticObjectRepository.check)와 같은 검사. 통과하면 저장할 칸, 아니면 오류 응답. */
async function checkStaticObjectBody(request: Request) {
  const body = staticObjectBodySchema.safeParse(await request.json().catch(() => undefined));
  if (!body.success) return fail('INVALID_STATIC_OBJECT');
  const { kind, obb, top_height_m: top, top_height_sigma_m: sigma } = body.data;
  // 서버는 real(float4) 열에 들어간 값으로 상판을 검사하고 그 값을 돌려준다.
  const stored = top === null ? null : Math.fround(top);
  if (!isValidObb(obb) || !isValidTop(kind, stored) || !isValidSigma(sigma))
    return fail('INVALID_STATIC_OBJECT');
  const name = body.data.name?.trim() || null;
  return { kind, name, obb, top_height_m: stored, sigma };
}
type Fields = Exclude<Awaited<ReturnType<typeof checkStaticObjectBody>>, Response>;

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

export interface StaticObjectsMock {
  handlers: ReturnType<typeof http.get>[];
  /** 지금 버전(보관하지 않은 작업면·정적 객체). 구성 묶음 mock 이 발행 때 싣는다. */
  current(): StaticObjectItem[];
}

/**
 * 작업면·정적 객체 조회·만들기·새 버전·보관(삭제). 로그인하지 않았거나 실서버 토큰이 만료면 401, 쓰기는 관리자만(아니면 403)이다.
 * 새 버전은 지금 버전을 닫고 다음 버전을 더한다(서버처럼 고치기 시작한 버전은 보지 않는다). 보관은 지금 버전의 valid_to 만 닫는다
 * (이력은 남고, 다시 보관해도 204, 보관한 객체를 고치면 404). 상태는 이 mock 이 살아 있는 동안만 둔다.
 */
export function createStaticObjectsMock({
  now = Date.now,
}: { now?: () => number } = {}): StaticObjectsMock {
  // 지금 버전과 닫힌 버전. 지금 버전은 valid_to 가 null 이다.
  let versions: StaticObjectItem[] = structuredClone(MOCK_STATIC_OBJECTS);
  let issued = 0;

  const deny = createDeny(now);
  const current = () => versions.filter((o) => o.valid_to === null);
  /** target 의 valid_to 를 t 로 닫은 전체 버전 목록. */
  const closed = (target: StaticObjectItem, t: number) =>
    versions.map((o) => (o === target ? { ...o, valid_to: t } : o));

  const handlers = [
    http.get(
      '*/api/static-objects',
      ({ request }) => deny(request, false) ?? HttpResponse.json(current()),
    ),
    http.get('*/api/static-objects/:objectId/versions', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const list = versions.filter((o) => o.object_id === params.objectId);
      return list.length > 0 ? HttpResponse.json(list) : fail('STATIC_OBJECT_NOT_FOUND');
    }),
    http.post('*/api/static-objects', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await checkStaticObjectBody(request);
      if (fields instanceof Response) return fields;
      // 서버는 작업면이면 ws-, 아니면 so- 뒤에 8 hex 를 고른다.
      const prefix = fields.kind === 'WORK_SURFACE' ? WORK_SURFACE_ID_PREFIX : OBSTACLE_ID_PREFIX;
      const item = itemOf(hexId(prefix, ++issued), 1, fields, now());
      versions = [...versions, item];
      return created(item);
    }),
    http.put('*/api/static-objects/:objectId', async ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await checkStaticObjectBody(request);
      if (fields instanceof Response) return fields;
      // 보관한 객체는 지금 버전이 없어 없는 객체와 같이 404 다(서버도 보관한 ID 는 고칠 수 없다).
      const before = current().find((o) => o.object_id === params.objectId);
      if (!before) return fail('STATIC_OBJECT_NOT_FOUND');
      const t = now();
      const item = itemOf(before.object_id, before.object_version + 1, fields, t);
      versions = [...closed(before, t), item];
      return HttpResponse.json(item);
    }),
    http.delete('*/api/static-objects/:objectId', ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      if (!versions.some((o) => o.object_id === params.objectId))
        return fail('STATIC_OBJECT_NOT_FOUND');
      const before = current().find((o) => o.object_id === params.objectId);
      // 이미 보관했으면(두 번 누름·재시도) 그대로 204 다.
      if (before) versions = closed(before, now());
      return noContent();
    }),
  ];

  return { handlers, current };
}
