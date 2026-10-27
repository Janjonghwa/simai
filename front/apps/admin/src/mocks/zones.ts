import { zoneBodySchema, type ZoneItem } from '@/features/zones/types';
import { readMockUser } from '@/mocks/mockUser';
import type { MockConfig } from '@/mocks/realtimeScenario';
import { http, HttpResponse } from 'msw/http';

// 서버 구역·구성 묶음 API(BE -120) 대신 쓰는 mock. 서버 형식과 검사 규칙(docs/design/server-config-bundle.md)을 따른다.
// 이벤트 시나리오(realtimeScenario)의 z-04·z-02 와 맞춘다. 위치는 디자인 데모의 기본 구역을 따른다.

/** 처음 구성 묶음(cfg-12)을 발행한 시각(ms). 처음 구역·작업면들은 그 전에 저장했다. */
export const SEEDED_AT = 1_760_342_400_000;

export const MOCK_ZONES = [
  {
    zone_id: 'z-04',
    zone_version: 1,
    name: '개구부 위 작업 공간',
    zone_kind: 'OPENING',
    eval_type: 'VOLUME_3D',
    polygon_xy: [
      [5.8, 1.2],
      [8.6, 1.2],
      [8.6, 3.8],
      [5.8, 3.8],
    ],
    z_min_m: 0,
    z_max_m: 2.4,
    severity: 'CRITICAL',
    requirements: null,
    has_guardrail: null,
    valid_from: SEEDED_AT - 60_000,
    valid_to: null,
  },
  {
    zone_id: 'z-02',
    zone_version: 1,
    name: '단상 40cm',
    zone_kind: 'PLATFORM',
    eval_type: 'VOLUME_3D',
    polygon_xy: [
      [1, 6.2],
      [4, 6.2],
      [4, 8.4],
      [1, 8.4],
    ],
    z_min_m: 0,
    z_max_m: 0.4,
    severity: 'HIGH',
    requirements: null,
    has_guardrail: true,
    valid_from: SEEDED_AT - 60_000,
    valid_to: null,
  },
] satisfies ZoneItem[];

/** Tier 0 가 새 구성 묶음을 받아 ACK 하기까지 걸리는 시간(ms) 흉내. */
const ACK_DELAY_MS = 3_000;
const MAX_VERTICES = 256;

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ status, detail, code }, { status });

/** 바닥 다각형 넓이(신발끈 공식). 닫는 점은 빼고 센다. */
function area(points: readonly (readonly [number, number])[]): number {
  let sum = 0;
  points.forEach(([x0, y0], i) => {
    const [x1, y1] = points[(i + 1) % points.length]!;
    sum += x0 * y1 - x1 * y0;
  });
  return Math.abs(sum) / 2;
}

interface MockBundle {
  config_version: string;
  previous_config_version: string | null;
  issued_at: number;
  /** 이 시각부터 Tier 0 의 config_ack 가 있다. */
  ack_at: number;
  rejected: boolean;
}

export interface ZonesMock {
  handlers: ReturnType<typeof http.get>[];
  /** 상단 바 /topic/health 의 구성 묶음 상태(서버 ViewerHealthService 의 config 를 흉내 낸다). */
  config(): MockConfig;
  /** 다음 발행을 Tier 0 가 거부한다(REJECTED, 이전 묶음을 계속 쓴다). */
  rejectNextBundle(): void;
}

/**
 * 구역 조회·만들기·새 버전과 구성 묶음 발행·조회. 로그인하지 않았거나 실서버 토큰이 만료면 401, 쓰기는 관리자만(아니면 403)이다.
 * 새 버전은 지금 버전을 닫고 다음 버전을 더한다(서버처럼 고치기 시작한 버전은 보지 않는다). 상태는 이 mock 이 살아 있는 동안만 둔다.
 * 처음에는 두 구역 v1 을 담은 cfg-12 가 현장에 적용되어 있다.
 */
export function createZonesMock({
  now = Date.now,
  ackDelayMs = ACK_DELAY_MS,
}: { now?: () => number; ackDelayMs?: number } = {}): ZonesMock {
  // 지금 버전과 닫힌 버전. 지금 버전은 valid_to 가 null 이다.
  let versions: ZoneItem[] = structuredClone(MOCK_ZONES);
  let issued = 0;
  // 새것부터.
  const bundles: MockBundle[] = [
    {
      config_version: 'cfg-12',
      previous_config_version: null,
      issued_at: SEEDED_AT,
      ack_at: SEEDED_AT,
      rejected: false,
    },
  ];
  let rejectNext = false;

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
  const current = () => versions.filter((z) => z.valid_to === null);

  /** 서버(ZoneRepository.check)와 같은 검사. 통과하면 저장할 칸, 아니면 오류 응답. */
  const check = async (request: Request) => {
    const body = zoneBodySchema.safeParse(await request.json().catch(() => undefined));
    if (!body.success || !body.data.name.trim() || !body.data.zone_kind.trim())
      return problem(400, 'INVALID_ZONE', '구역 값이 올바르지 않습니다');
    const { z_min_m: min, z_max_m: max, eval_type, polygon_xy } = body.data;
    if (
      (min !== null && max !== null && !(min < max)) ||
      (eval_type === 'VOLUME_3D' && (min === null || max === null))
    )
      return problem(400, 'INVALID_Z_RANGE', '높이 범위가 올바르지 않습니다');
    const [first, last] = [polygon_xy[0]!, polygon_xy[polygon_xy.length - 1]!];
    const ring =
      first[0] === last[0] && first[1] === last[1] ? polygon_xy.slice(0, -1) : polygon_xy;
    if (ring.length < 3 || ring.length > MAX_VERTICES || area(ring) <= 0)
      return problem(400, 'INVALID_POLYGON', '다각형이 올바르지 않습니다');
    return {
      ...body.data,
      name: body.data.name.trim(),
      zone_kind: body.data.zone_kind.trim(),
      polygon_xy: ring,
    };
  };

  const statusAt = (bundle: MockBundle, t: number) =>
    t < bundle.ack_at ? 'PENDING' : bundle.rejected ? 'REJECTED' : 'APPLIED';
  /** t 에 Tier 0 가 쓰고 있는 묶음(가장 최근에 적용된 것). */
  const activeAt = (t: number) => bundles.find((b) => statusAt(b, t) === 'APPLIED') ?? null;
  const view = (bundle: MockBundle) => {
    const t = now();
    const status = statusAt(bundle, t);
    return {
      config_version: bundle.config_version,
      previous_config_version: bundle.previous_config_version,
      issued_at: bundle.issued_at,
      bundle_hash: `sha256:mock-${bundle.config_version}`,
      calibration_version: 'cal-7',
      rule_profile: 'OPERATION',
      status,
      applied_at: status === 'APPLIED' ? bundle.ack_at : null,
      last_ack:
        status === 'PENDING'
          ? null
          : {
              result: status,
              // 그 묶음의 ACK 때 Tier 0 가 쓰던 묶음이다(뒤에 다른 묶음이 적용돼도 바뀌지 않는다).
              active_config_version: activeAt(bundle.ack_at)?.config_version ?? null,
              reject_reason: status === 'REJECTED' ? 'VALIDATION_FAILED' : null,
            },
    };
  };

  const handlers = [
    http.get('*/api/zones', ({ request }) => deny(request, false) ?? HttpResponse.json(current())),
    http.get('*/api/zones/:zoneId/versions', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const list = versions.filter((z) => z.zone_id === params.zoneId);
      return list.length > 0
        ? HttpResponse.json(list)
        : problem(404, 'ZONE_NOT_FOUND', '구역을 찾을 수 없습니다');
    }),
    http.post('*/api/zones', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await check(request);
      if (fields instanceof Response) return fields;
      // 서버는 z-<8 hex> 를 고른다.
      const zone: ZoneItem = {
        ...fields,
        zone_id: `z-${(++issued).toString(16).padStart(8, '0')}`,
        zone_version: 1,
        valid_from: now(),
        valid_to: null,
      };
      versions = [...versions, zone];
      return HttpResponse.json(zone, { status: 201 });
    }),
    http.put('*/api/zones/:zoneId', async ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const before = current().find((z) => z.zone_id === params.zoneId);
      if (!before) return problem(404, 'ZONE_NOT_FOUND', '구역을 찾을 수 없습니다');
      const fields = await check(request);
      if (fields instanceof Response) return fields;
      const t = now();
      const zone: ZoneItem = {
        ...fields,
        zone_id: before.zone_id,
        zone_version: before.zone_version + 1,
        valid_from: t,
        valid_to: null,
      };
      versions = [...versions.map((z) => (z === before ? { ...z, valid_to: t } : z)), zone];
      return HttpResponse.json(zone);
    }),
    http.post('*/api/config-bundles', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const body: unknown = await request.json().catch(() => null);
      const profile =
        typeof body === 'object' && body !== null ? Reflect.get(body, 'rule_profile') : undefined;
      if (
        profile !== undefined &&
        profile !== null &&
        profile !== 'OPERATION' &&
        profile !== 'CLASSROOM_TEST'
      )
        return problem(400, 'INVALID_RULE_PROFILE', 'rule_profile 이 올바르지 않습니다');
      const t = now();
      const number = Number(bundles[0]!.config_version.split('-')[1]) + 1;
      const bundle: MockBundle = {
        config_version: `cfg-${number}`,
        previous_config_version: bundles[0]!.config_version,
        issued_at: t,
        ack_at: t + ackDelayMs,
        rejected: rejectNext,
      };
      rejectNext = false;
      bundles.unshift(bundle);
      return HttpResponse.json(view(bundle), { status: 201 });
    }),
    http.get('*/api/config-bundles', ({ request }) => {
      return deny(request, false) ?? HttpResponse.json(bundles.slice(0, 20).map(view));
    }),
    http.get('*/api/config-bundles/:version', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const bundle = bundles.find((b) => b.config_version === params.version);
      return bundle
        ? HttpResponse.json(view(bundle))
        : problem(404, 'CONFIG_BUNDLE_NOT_FOUND', '구성 묶음을 찾을 수 없습니다');
    }),
  ];

  return {
    handlers,
    config: () => {
      const t = now();
      const latest = bundles[0]!;
      const status = statusAt(latest, t);
      return {
        published_version: latest.config_version,
        applied_version: activeAt(t)?.config_version ?? null,
        sync: status === 'APPLIED' ? 'CURRENT' : status === 'PENDING' ? 'PENDING' : 'STALE',
      };
    },
    rejectNextBundle: () => {
      rejectNext = true;
    },
  };
}
