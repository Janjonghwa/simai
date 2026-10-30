import type { StaticObjectItem } from '@/features/surfaces/types';
import {
  zoneBodySchema,
  type BundleItem,
  type BundleStatus,
  type ZoneBody,
  type ZoneItem,
} from '@/features/zones/types';
import { readLiveUser } from '@/mocks/problem';
import type { MockConfig } from '@/mocks/realtimeScenario';
import { http, HttpResponse } from 'msw/http';

// 서버 구역·구성 묶음 API(BE -120) 대신 쓰는 mock. 서버 형식과 검사 규칙(docs/design/server-config-bundle.md)을 따른다.
// 이벤트 시나리오(realtimeScenario)의 z-04·z-02 와 맞춘다. 위치는 디자인 데모의 기본 구역을 따른다.
// 작업면 mock(mocks/staticObjects)도 이 파일의 오류 응답·인증 검사·ID 도우미를 함께 쓴다.

/** 처음 구성 묶음(cfg-12)을 발행한 시각(ms). 처음 구역·작업면들은 그 전에 저장했다. */
export const SEEDED_AT = 1_760_342_400_000;
/** 처음 구역·작업면을 저장한 시각(처음 묶음 발행 1분 전). */
export const SEEDED_VALID_FROM = SEEDED_AT - 60_000;
/** 처음 적용된 묶음의 번호(cfg-12). 발행마다 하나씩 늘린다. */
const SEEDED_CONFIG_NUMBER = 12;
const CONFIG_VERSION_PREFIX = 'cfg-';
/** 목록 조회가 주는 최근 묶음 수. */
const BUNDLE_LIST_LIMIT = 20;
/** 묶음이 가리키는 보정 문서 버전·거부 사유(고정 흉내). */
const CALIBRATION_VERSION = 'cal-7';
const REJECT_REASON = 'VALIDATION_FAILED';
/** 발행 요청이 고를 수 있는 규칙 프로필. 안 고르면 앞의 것(운영)이다. */
const RULE_PROFILES = ['OPERATION', 'CLASSROOM_TEST'] as const;
const DEFAULT_RULE_PROFILE = RULE_PROFILES[0];
/** Tier 0 가 새 구성 묶음을 받아 ACK 하기까지 걸리는 시간(ms) 흉내. */
const ACK_DELAY_MS = 3_000;
/** 구역 다각형 꼭짓점 수의 범위(서버 검사). */
const MIN_VERTICES = 3;
const MAX_VERTICES = 256;
/** 서버가 고르는 ID 는 접두 뒤에 8자리 hex 다. */
const ID_HEX_LENGTH = 8;
const HEX_RADIX = 16;

interface ErrorSpec {
  status: number;
  detail: string;
}

/** 로그인·권한 오류. 작업면 mock 도 같은 표를 쓴다. */
export const AUTH_ERRORS = {
  UNAUTHORIZED: { status: 401, detail: '로그인이 필요합니다' },
  FORBIDDEN: { status: 403, detail: '권한이 없습니다' },
};

/** 구성 묶음 발행이 실패하는 이유(서버 ConfigBundleService 의 오류 코드). */
export const PUBLISH_FAILURES = {
  CALIBRATION_MISSING: { status: 409, detail: '보정 문서가 없습니다' },
  CURRENT_CALIBRATION_MISSING: { status: 409, detail: '유효한 보정 문서가 없습니다' },
  RULES_MISSING: { status: 409, detail: '유효한 규칙 버전이 없습니다' },
  SITE_NOT_SINGLE: { status: 409, detail: '현장이 하나로 정해지지 않았습니다' },
  PUBLIC_URL_NOT_SET: { status: 503, detail: '서버 공개 주소가 설정되지 않았습니다' },
} as const;
export type PublishFailureCode = keyof typeof PUBLISH_FAILURES;

const ERRORS = {
  INVALID_ZONE: { status: 400, detail: '구역 값이 올바르지 않습니다' },
  INVALID_Z_RANGE: { status: 400, detail: '높이 범위가 올바르지 않습니다' },
  INVALID_POLYGON: { status: 400, detail: '다각형이 올바르지 않습니다' },
  INVALID_RULE_PROFILE: { status: 400, detail: 'rule_profile 이 올바르지 않습니다' },
  ZONE_NOT_FOUND: { status: 404, detail: '구역을 찾을 수 없습니다' },
  CONFIG_BUNDLE_NOT_FOUND: { status: 404, detail: '구성 묶음을 찾을 수 없습니다' },
  ...PUBLISH_FAILURES,
};

/** 오류 표(코드 → 상태·메시지)로 서버 오류 응답(status·detail·code)을 만드는 함수를 만든다. */
export function errorsOf<T extends Record<string, ErrorSpec>>(table: T) {
  return (code: keyof T & string) => {
    const { status, detail } = table[code];
    return HttpResponse.json({ status, detail, code }, { status });
  };
}

const fail = errorsOf(ERRORS);

export const created = (body: Parameters<typeof HttpResponse.json>[0]) =>
  HttpResponse.json(body, { status: 201 });
export const noContent = () => new HttpResponse(null, { status: 204 });

/** 서버가 고르는 `<접두>-<8 hex>` ID. */
export const hexId = (prefix: string, serial: number) =>
  `${prefix}-${serial.toString(HEX_RADIX).padStart(ID_HEX_LENGTH, '0')}`;

/** 로그인하지 않았거나 토큰이 만료면 401, 쓰기(write)인데 관리자가 아니면 403 응답을 주고 통과하면 undefined 를 주는 검사. */
export function createDeny(now: () => number) {
  const authFail = errorsOf(AUTH_ERRORS);
  return (request: Request, write: boolean) => {
    const user = readLiveUser(request, now());
    if (!user) return authFail('UNAUTHORIZED');
    if (write && user.role !== 'ADMIN') return authFail('FORBIDDEN');
    return undefined;
  };
}

export const MOCK_ZONES = [
  {
    zone_id: 'z-04',
    zone_version: 1,
    name: '개구부 구역',
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
    valid_from: SEEDED_VALID_FROM,
    valid_to: null,
  },
  {
    zone_id: 'z-02',
    zone_version: 1,
    name: '단상 구역',
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
    valid_from: SEEDED_VALID_FROM,
    valid_to: null,
  },
] satisfies ZoneItem[];

/** 묶음에 싣는 규칙 항목. 규칙 mock 은 따로 없어 발행마다 같은 규칙 버전을 싣는다. */
const RULE_ITEM = {
  item_type: 'RULE',
  item_id: 'R2',
  item_version: 1,
} satisfies BundleItem;

// 처음 묶음(cfg-12)에 실린 작업면. MOCK_STATIC_OBJECTS(mocks/staticObjects.ts)와 같다(그쪽이 이 파일을 가져와 순환하지 않게 적는다).
const SEEDED_SURFACE_ITEMS = [
  { item_type: 'STATIC_OBJECT', item_id: 'ws-0a0c0001', item_version: 1, kind: 'WORK_SURFACE' },
  { item_type: 'STATIC_OBJECT', item_id: 'ws-0a0c0002', item_version: 1, kind: 'WORK_SURFACE' },
] satisfies BundleItem[];

const zoneItem = (zone: Pick<ZoneItem, 'zone_id' | 'zone_version'>) =>
  ({
    item_type: 'ZONE',
    item_id: zone.zone_id,
    item_version: zone.zone_version,
  }) satisfies BundleItem;

const staticObjectItem = (o: StaticObjectItem): BundleItem => ({
  item_type: 'STATIC_OBJECT',
  item_id: o.object_id,
  item_version: o.object_version,
  kind: o.kind,
});

/** Tier 0 의 ACK 로 정해지는 묶음 상태(MISMATCH 는 흉내 내지 않는다). */
type AckStatus = Exclude<BundleStatus, 'MISMATCH'>;

/** 상단 바에 보이는 동기화 상태. 거부된 묶음이 최신이면 현장은 이전 묶음을 쓰고 있어 STALE 이다. */
const SYNC_BY_STATUS: Record<AckStatus, MockConfig['sync']> = {
  APPLIED: 'CURRENT',
  PENDING: 'PENDING',
  REJECTED: 'STALE',
};

/** 바닥 다각형 넓이(신발끈 공식). 닫는 점은 빼고 센다. */
function area(points: readonly (readonly [number, number])[]): number {
  let sum = 0;
  points.forEach(([x0, y0], i) => {
    const [x1, y1] = points[(i + 1) % points.length]!;
    sum += x0 * y1 - x1 * y0;
  });
  return Math.abs(sum) / 2;
}

/** 닫는 점(첫 점과 같은 끝 점)은 빼고 돌려준다. 서버는 열린 고리로 저장한다. */
function openRing(points: ZoneBody['polygon_xy']): ZoneBody['polygon_xy'] {
  const first = points[0]!;
  const last = points[points.length - 1]!;
  return first[0] === last[0] && first[1] === last[1] ? points.slice(0, -1) : points;
}

const isValidRing = (ring: ZoneBody['polygon_xy']) =>
  ring.length >= MIN_VERTICES && ring.length <= MAX_VERTICES && area(ring) > 0;

/** 높이 범위: 있으면 최저 < 최고, 3D 부피 판정은 둘 다 있어야 한다. */
function isValidZRange({ z_min_m: min, z_max_m: max, eval_type }: ZoneBody): boolean {
  if (eval_type === 'VOLUME_3D' && (min === null || max === null)) return false;
  return min === null || max === null || min < max;
}

/** 서버(ZoneRepository.check)와 같은 검사. 통과하면 저장할 칸, 아니면 오류 응답. */
async function checkZoneBody(request: Request) {
  const body = zoneBodySchema.safeParse(await request.json().catch(() => undefined));
  if (!body.success || !body.data.name.trim() || !body.data.zone_kind.trim())
    return fail('INVALID_ZONE');
  if (!isValidZRange(body.data)) return fail('INVALID_Z_RANGE');
  const ring = openRing(body.data.polygon_xy);
  if (!isValidRing(ring)) return fail('INVALID_POLYGON');
  return {
    ...body.data,
    name: body.data.name.trim(),
    zone_kind: body.data.zone_kind.trim(),
    polygon_xy: ring,
  };
}

/** 요청 본문의 rule_profile. 본문이 객체가 아니면 없는 것으로 본다. */
function profileOf(body: unknown): unknown {
  return typeof body === 'object' && body !== null ? Reflect.get(body, 'rule_profile') : undefined;
}

/** 없거나 null 이면 기본 프로필을 쓰므로 통과다. */
const isValidProfile = (profile: unknown) =>
  profile === undefined || profile === null || RULE_PROFILES.some((p) => p === profile);

interface MockBundle {
  config_version: string;
  previous_config_version: string | null;
  issued_at: number;
  /** 이 시각부터 Tier 0 의 config_ack 가 있다. */
  ack_at: number;
  rejected: boolean;
  /** 발행 때 실린 구역·규칙·작업면(서버 config_bundle_item). */
  items: BundleItem[];
}

export interface ZonesMock {
  handlers: ReturnType<typeof http.get>[];
  /** 상단 바 /topic/health 의 구성 묶음 상태(서버 ViewerHealthService 의 config 를 흉내 낸다). */
  config(): MockConfig;
  /** 다음 발행을 Tier 0 가 거부한다(REJECTED, 이전 묶음을 계속 쓴다). */
  rejectNextBundle(): void;
  /** 다음 발행 요청 하나를 서버 오류로 거절한다(묶음은 만들지 않는다). 409(보정·규칙·현장)·503(공개 주소). 기본은 보정 문서 없음이다. */
  failNextPublish(code?: PublishFailureCode): void;
}

/**
 * 구역 조회·만들기·새 버전·보관(삭제)과 구성 묶음 발행·조회. 로그인하지 않았거나 실서버 토큰이 만료면 401, 쓰기는 관리자만(아니면 403)이다.
 * 새 버전은 지금 버전을 닫고 다음 버전을 더한다(서버처럼 고치기 시작한 버전은 보지 않는다). 보관은 지금 버전의 valid_to 만 닫는다
 * (이력은 남고, 다시 보관해도 204, 보관한 구역을 고치면 404). 상태는 이 mock 이 살아 있는 동안만 둔다.
 * 처음에는 두 구역 v1·규칙·작업면 둘을 담은 cfg-12 가 현장에 적용되어 있다. 발행하는 묶음은 그때 유효한 구역과 staticObjects()
 * 가 주는 지금 작업면·정적 객체(구역 mock 은 작업면을 들고 있지 않아 밖에서 받는다)와 규칙을 싣는다.
 */
export function createZonesMock({
  now = Date.now,
  ackDelayMs = ACK_DELAY_MS,
  staticObjects = () => [],
}: {
  now?: () => number;
  ackDelayMs?: number;
  staticObjects?: () => readonly StaticObjectItem[];
} = {}): ZonesMock {
  // 지금 버전과 닫힌 버전. 지금 버전은 valid_to 가 null 이다.
  let versions: ZoneItem[] = structuredClone(MOCK_ZONES);
  let issued = 0;
  let bundleNumber = SEEDED_CONFIG_NUMBER;
  // 새것부터.
  const bundles: MockBundle[] = [
    {
      config_version: `${CONFIG_VERSION_PREFIX}${bundleNumber}`,
      previous_config_version: null,
      issued_at: SEEDED_AT,
      ack_at: SEEDED_AT,
      rejected: false,
      items: [...MOCK_ZONES.map(zoneItem), RULE_ITEM, ...SEEDED_SURFACE_ITEMS],
    },
  ];
  let rejectNext = false;
  let failNext: PublishFailureCode | null = null;

  const deny = createDeny(now);
  const current = () => versions.filter((z) => z.valid_to === null);
  /** target 의 valid_to 를 t 로 닫은 전체 버전 목록. */
  const closed = (target: ZoneItem, t: number) =>
    versions.map((z) => (z === target ? { ...z, valid_to: t } : z));

  const statusAt = (bundle: MockBundle, t: number): AckStatus => {
    if (t < bundle.ack_at) return 'PENDING';
    return bundle.rejected ? 'REJECTED' : 'APPLIED';
  };
  /** t 에 Tier 0 가 쓰고 있는 묶음(가장 최근에 적용된 것). */
  const activeAt = (t: number) => bundles.find((b) => statusAt(b, t) === 'APPLIED') ?? null;
  const lastAckOf = (bundle: MockBundle, status: AckStatus) =>
    status === 'PENDING'
      ? null
      : {
          result: status,
          // 그 묶음의 ACK 때 Tier 0 가 쓰던 묶음이다(뒤에 다른 묶음이 적용돼도 바뀌지 않는다).
          active_config_version: activeAt(bundle.ack_at)?.config_version ?? null,
          reject_reason: status === 'REJECTED' ? REJECT_REASON : null,
        };
  const view = (bundle: MockBundle) => {
    const status = statusAt(bundle, now());
    return {
      config_version: bundle.config_version,
      previous_config_version: bundle.previous_config_version,
      issued_at: bundle.issued_at,
      bundle_hash: `sha256:mock-${bundle.config_version}`,
      calibration_version: CALIBRATION_VERSION,
      rule_profile: DEFAULT_RULE_PROFILE,
      status,
      applied_at: status === 'APPLIED' ? bundle.ack_at : null,
      items: bundle.items,
      last_ack: lastAckOf(bundle, status),
    };
  };

  /** 서버처럼 그때 유효한 구역·작업면(보관한 것은 빠진다)과 규칙을 싣는다. */
  const bundleItems = (): BundleItem[] => [
    ...current().map(zoneItem),
    RULE_ITEM,
    ...staticObjects().map(staticObjectItem),
  ];
  /** 다음 번호의 묶음을 t 에 발행해 맨 앞에 더한다. */
  const publish = (t: number) => {
    const bundle: MockBundle = {
      config_version: `${CONFIG_VERSION_PREFIX}${++bundleNumber}`,
      previous_config_version: bundles[0]!.config_version,
      issued_at: t,
      ack_at: t + ackDelayMs,
      rejected: rejectNext,
      items: bundleItems(),
    };
    rejectNext = false;
    bundles.unshift(bundle);
    return bundle;
  };

  const handlers = [
    http.get('*/api/zones', ({ request }) => deny(request, false) ?? HttpResponse.json(current())),
    http.get('*/api/zones/:zoneId/versions', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const list = versions.filter((z) => z.zone_id === params.zoneId);
      return list.length > 0 ? HttpResponse.json(list) : fail('ZONE_NOT_FOUND');
    }),
    http.post('*/api/zones', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const fields = await checkZoneBody(request);
      if (fields instanceof Response) return fields;
      const zone: ZoneItem = {
        ...fields,
        zone_id: hexId('z', ++issued),
        zone_version: 1,
        valid_from: now(),
        valid_to: null,
      };
      versions = [...versions, zone];
      return created(zone);
    }),
    http.put('*/api/zones/:zoneId', async ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const before = current().find((z) => z.zone_id === params.zoneId);
      if (!before) return fail('ZONE_NOT_FOUND');
      const fields = await checkZoneBody(request);
      if (fields instanceof Response) return fields;
      const t = now();
      const zone: ZoneItem = {
        ...fields,
        zone_id: before.zone_id,
        zone_version: before.zone_version + 1,
        valid_from: t,
        valid_to: null,
      };
      versions = [...closed(before, t), zone];
      return HttpResponse.json(zone);
    }),
    http.delete('*/api/zones/:zoneId', ({ request, params }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      if (!versions.some((z) => z.zone_id === params.zoneId)) return fail('ZONE_NOT_FOUND');
      const before = current().find((z) => z.zone_id === params.zoneId);
      // 이미 보관했으면(두 번 누름·재시도) 그대로 204 다.
      if (before) versions = closed(before, now());
      return noContent();
    }),
    http.post('*/api/config-bundles', async ({ request }) => {
      const denied = deny(request, true);
      if (denied) return denied;
      const body: unknown = await request.json().catch(() => null);
      if (!isValidProfile(profileOf(body))) return fail('INVALID_RULE_PROFILE');
      if (failNext !== null) {
        const code = failNext;
        failNext = null;
        return fail(code);
      }
      return created(view(publish(now())));
    }),
    http.get('*/api/config-bundles', ({ request }) => {
      return (
        deny(request, false) ?? HttpResponse.json(bundles.slice(0, BUNDLE_LIST_LIMIT).map(view))
      );
    }),
    http.get('*/api/config-bundles/:version', ({ request, params }) => {
      const denied = deny(request, false);
      if (denied) return denied;
      const bundle = bundles.find((b) => b.config_version === params.version);
      return bundle ? HttpResponse.json(view(bundle)) : fail('CONFIG_BUNDLE_NOT_FOUND');
    }),
  ];

  return {
    handlers,
    config: () => {
      const t = now();
      const latest = bundles[0]!;
      return {
        published_version: latest.config_version,
        applied_version: activeAt(t)?.config_version ?? null,
        sync: SYNC_BY_STATUS[statusAt(latest, t)],
      };
    },
    rejectNextBundle: () => {
      rejectNext = true;
    },
    failNextPublish: (code = 'CALIBRATION_MISSING') => {
      failNext = code;
    },
  };
}
