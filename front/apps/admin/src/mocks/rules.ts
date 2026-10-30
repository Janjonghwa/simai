import { readLiveUser, unauthorized } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// 서버 GET /api/rules(BE 곽나혁, docs/design/server-rules-scenes.md) 대신 쓰는 mock. 값은 ai 등록부 시드(R__rule_versions.sql)와
// 같은 운영 판(R1~R7)이고 R8~R10 은 확장 규칙이라 비활성이다. 로그인한 모든 역할이 읽는다.

const OPERATIONAL = {
  floor_reference: false,
  height_threshold_m: 2.0,
  name: 'operational',
  test: false,
};
const TEST = {
  floor_reference: true,
  height_threshold_m: 0.35,
  name: 'classroom-test-podium-40cm',
  test: true,
};

/** 규칙 판이 적용되기 시작한 시각(ms). 시드 값이다. */
const FROM_MS = 1_760_342_400_000;

/** 여러 규칙이 공유하는 시드 값. 위치 불확실성 배수, 사람 반지름(m), 보정 여유(m), 비교 허용 오차. */
const SIGMA_K = 2.0;
const PERSON_RADIUS_M = 0.3;
const MARGIN_M = 0.3;
const TOLERANCE = 1e-9;

type Profile = 'OPERATION' | 'CLASSROOM_TEST';

const version = (
  ruleId: string,
  profile: Profile,
  ruleVersion: number,
  thresholds: Record<string, unknown>,
  defaultSeverity: string | null,
) => ({
  rule_id: ruleId,
  rule_version: ruleVersion,
  profile,
  thresholds,
  default_severity: defaultSeverity,
  legal_basis: null,
  reads: ['tracks.observation_state', 'tracks.position_xy'],
  valid_from_ms: FROM_MS,
  valid_to_ms: null,
});

/** 프로파일마다 규칙 판을 만든다. 교실 시험 프로파일은 바닥 기준·낮은 높이 문턱(단상 40cm)을 쓴다. */
function currents(profile: Profile) {
  const test = profile === 'CLASSROOM_TEST';
  const base = test ? 1000 : 0;
  const height = test ? TEST : OPERATIONAL;
  const v = (n: number) => base + n;
  return {
    R1: version(
      'R1',
      profile,
      1,
      {
        calibration_margin: MARGIN_M,
        calibration_widened_states: ['DRIFTED', 'PROVISIONAL', 'UNCALIBRATED'],
        k: SIGMA_K,
        margin: MARGIN_M,
        r6: { severity: null, sigma_k: SIGMA_K },
        radius: PERSON_RADIUS_M,
        tol: TOLERANCE,
      },
      null,
    ),
    R2: version(
      'R2',
      profile,
      v(1),
      {
        clear: 1.3,
        k: SIGMA_K,
        profile: height,
        radius: PERSON_RADIUS_M,
        raise: 1.0,
        tol: TOLERANCE,
      },
      'CRITICAL',
    ),
    R3: version(
      'R3',
      profile,
      v(1),
      {
        k: SIGMA_K,
        profile: height,
        radius: PERSON_RADIUS_M,
        threshold: height.height_threshold_m,
        tol: TOLERANCE,
      },
      'HIGH',
    ),
    R4: version(
      'R4',
      profile,
      1,
      {
        drop_min: 0.8,
        fb_clear: 0.8,
        fb_low: 1.0,
        followup: 2000,
        hold: 2000,
        lost_memory: 10000,
        lost_window: 1000,
        low_clear: 0.8,
        low_raise: 0.6,
        recovery: 3000,
        support_memory: 10000,
        window: 1000,
      },
      'CRITICAL',
    ),
    R5: version(
      'R5',
      profile,
      v(1),
      {
        edge_reach: 1.0,
        horizon_s: 2.0,
        k: SIGMA_K,
        limit_s: 2.5,
        margin: MARGIN_M,
        max_speed: 4.0,
        max_staleness: 500,
        min_closing: 0.4,
        min_speed: 0.2,
        profile: height,
        radius: PERSON_RADIUS_M,
        tol: TOLERANCE,
      },
      'CAUTION',
    ),
    R7: version('R7', profile, 1, { camera_stall_ms: 500, min_cameras: 2 }, null),
  };
}

const NAMES: [string, string, 'MVP' | 'EXTENSION'][] = [
  ['R1', '위험 구역 진입', 'MVP'],
  ['R2', '개구부·가장자리 근접', 'MVP'],
  ['R3', '고소 위치', 'MVP'],
  ['R4', '낙상 의심', 'MVP'],
  ['R5', '진입 예측', 'MVP'],
  ['R6', '위험 구역 내 관측 상실', 'MVP'],
  ['R7', '구역 감시 능력 저하', 'MVP'],
  ['R8', '장비 근접', 'EXTENSION'],
  ['R9', 'PPE 필수 구역 미착용', 'EXTENSION'],
  ['R10', '불확실 점유 근접', 'EXTENSION'],
];

export interface RulesMockOptions {
  profile?: Profile;
  configVersion?: string;
  /** 발행만 하고 아직 적용 전인 더 새 구성 묶음. */
  pendingConfigVersion?: string | null;
  now?: () => number;
}

export function rulesBody({
  profile = 'OPERATION',
  configVersion = 'cfg-3',
  pendingConfigVersion = null,
}: RulesMockOptions = {}) {
  const current = currents(profile) as Record<string, ReturnType<typeof version>>;
  return {
    rule_profile: profile,
    config_version: configVersion,
    pending_config_version: pendingConfigVersion,
    rules: NAMES.map(([ruleId, name, stage]) => {
      const own = current[ruleId];
      if (ruleId === 'R6') {
        // 자기 판이 없다. 감싼 규칙 R1 의 r6 설정으로 적용된다.
        return { rule_id: ruleId, name, stage, active: true, current: null, applied_with: ['R1'] };
      }
      return { rule_id: ruleId, name, stage, active: own !== undefined, current: own ?? null };
    }),
    versions: Object.values(current).map(
      ({ rule_id, rule_version, profile: p, valid_from_ms, valid_to_ms }) => ({
        rule_id,
        rule_version,
        profile: p,
        valid_from_ms,
        valid_to_ms,
      }),
    ),
  };
}

export function createRulesMock(options: RulesMockOptions = {}) {
  const { now = Date.now } = options;
  return {
    handlers: [
      http.get('*/api/rules', ({ request }) => {
        if (!readLiveUser(request, now())) return unauthorized();
        return HttpResponse.json(rulesBody(options));
      }),
    ],
  };
}
