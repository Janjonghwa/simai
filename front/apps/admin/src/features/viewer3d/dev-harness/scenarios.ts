import { SKELETON_VISIBLE_KEY } from '@/features/viewer3d/data/contractAdapter';
import type { SourceConnection } from '@/features/viewer3d/data/spatialSource';

// 개발·시험 전용 Mock 시나리오. 실제 관측·판정이 아니다. 브라우저에서 위험을 판정하지 않고, 계약에 맞게 미리 만든 이벤트만 재생한다.
// 같은 (시나리오, tick, t0) 는 늘 같은 메시지를 만든다. MockSource 스트림과 MSW REST 응답이 이 한 곳에서 나온다.

export const SCENARIOS = [
  { key: 'normal', label: '정상', hint: '사람 3명 관측' },
  { key: 'observation-loss', label: '관측 손실', hint: 'OBSERVED→PREDICTED→UNKNOWN' },
  {
    key: 'risk-lifecycle',
    label: '위험 전이 회귀(합성)',
    hint: 'RAISED→UPDATED→중첩→RESOLVED 전이 회귀용. 구역 밖 관계자가 포함될 수 있어 지오펜싱 시연이 아니다',
  },
  {
    key: 'geofence',
    label: '지오펜싱 정합 시연',
    hint: '구역 진입→체류 상향→이탈 해소. 이벤트 대상은 그 tick 에 구역 안에 있는 사람뿐이다',
  },
  { key: 'disconnect', label: '통신 장애', hint: '끊김·역전·중복·복구' },
  { key: 'invalid-input', label: '잘못된 입력', hint: '계약 오류·미상 Z' },
] as const;

export type ScenarioKey = (typeof SCENARIOS)[number]['key'];

export const isScenarioKey = (value: unknown): value is ScenarioKey =>
  SCENARIOS.some((s) => s.key === value);

/** World 10 Hz. */
export const TICK_MS = 100;
/** Health 1 Hz. */
export const HEALTH_EVERY = 10;

export const MOCK_TRACK_IDS = ['w-0001', 'w-0002', 'w-0003'] as const;
const VERSIONS = { calibration: 'cal-demo', scene: 'scene-demo-1', config: 'cfg-demo-1' };
const NEXT_SCENE = 'scene-demo-2';

/** 시나리오마다 한 주기의 tick 수. 같은 동작이 이 간격으로 되풀이된다(시각과 ID 는 계속 늘어난다). */
export const PERIOD: Record<ScenarioKey, number> = {
  normal: 80,
  'observation-loss': 60,
  'risk-lifecycle': 80,
  geofence: 120,
  disconnect: 70,
  'invalid-input': 10,
};

/**
 * 이벤트를 만드는 시나리오의 재생 상한(tick). 주기마다 새 event_id 가 생기고 Store 는 해소된 이벤트도 남기므로
 * 개발 시연이 끝없이 쌓이지 않게 이 지점에서 재생을 멈춘다('처음부터'로 다시 시작). 운영 Store 규칙이 아니다.
 */
export const DEMO_MAX_TICKS: Partial<Record<ScenarioKey, number>> = {
  'risk-lifecycle': 4000,
  geofence: 4800,
};

export type ScenarioEmit =
  | { type: 'message'; raw: unknown }
  | { type: 'connection'; state: SourceConnection; reason: string };

const CODE: Record<ScenarioKey, number> = {
  normal: 1,
  'observation-loss': 2,
  'risk-lifecycle': 3,
  disconnect: 4,
  'invalid-input': 5,
  geofence: 6,
};

const hex = (n: number, width: number) => n.toString(16).padStart(width, '0').slice(-width);
const round = (n: number) => Math.round(n * 1000) / 1000;

/** 결정적 msg_id(UUID 형식). slot 으로 같은 tick 의 메시지를 구분한다. */
export const mockMsgId = (key: ScenarioKey, tick: number, slot: number) =>
  `${hex(0x5e000000 + CODE[key], 8)}-0000-4000-8000-${hex(tick * 16 + slot, 12)}`;

/** 결정적 event_id(UUIDv7 형식). 주기마다 새 이벤트다. */
export const mockEventId = (cycle: number, n: number) =>
  `0192a6f0-${hex(cycle, 4)}-7${hex(n, 3)}-8000-${hex(0xdec0de, 12)}`;

const tAt = (t0: number, tick: number) => t0 + tick * TICK_MS;

function envelope<P extends object>(
  msgType: string,
  msgId: string,
  sentAt: number,
  payload: P,
  schemaVersion: string,
) {
  return {
    schema_version: schemaVersion,
    msg_type: msgType,
    msg_id: msgId,
    sent_at: sentAt,
    source_id: 'ec2',
    payload,
  };
}

// ---- 사람 ----

type Xy = [number, number];

function poseOf(id: (typeof MOCK_TRACK_IDS)[number], tick: number): Xy {
  switch (id) {
    case 'w-0001': {
      const a = (2 * Math.PI * tick) / 80;
      return [3 + 1.2 * Math.cos(a), 6.8 + 1.2 * Math.sin(a)];
    }
    case 'w-0002': {
      const phase = (tick % 100) / 100;
      return [5 + 3 * (phase < 0.5 ? phase * 2 : 2 - phase * 2), 3];
    }
    case 'w-0003':
      return [7.5 + 0.3 * Math.sin((2 * Math.PI * tick) / 50), 7.5];
  }
}

/**
 * 지오펜싱 시연 경로(주기 120, 10 Hz, 1 m/s). 경계 위에 서지 않게 0.05 m 어긋나게 둔다.
 * - w-0001: y=2.25 를 따라 x 3.95→7.95(0~40), 머묾(40~69), 7.95→3.95(70~110). z-02(x 6.5~9) 안: tick 26~84.
 * - w-0003: y=7.0 을 따라 x 0.45→2.95(0~25), 머묾(25~59), 2.95→0.45(60~85). z-01(x 1.5~4.5) 안: tick 11~74.
 * - w-0002: (5.2, 5.0) 에 서 있는 구역 밖 사람.
 */
function geofencePose(id: (typeof MOCK_TRACK_IDS)[number], tick: number): Xy {
  const p = tick % PERIOD.geofence;
  switch (id) {
    case 'w-0001': {
      const x = p < 40 ? 3.95 + 0.1 * p : p < 70 ? 7.95 : p < 110 ? 7.95 - 0.1 * (p - 70) : 3.95;
      return [x, 2.25];
    }
    case 'w-0002':
      return [5.2, 5.0];
    case 'w-0003': {
      const x = p < 25 ? 0.45 + 0.1 * p : p < 60 ? 2.95 : p < 85 ? 2.95 - 0.1 * (p - 60) : 0.45;
      return [x, 7.0];
    }
  }
}

// COCO17 관절의 골반 기준 수평 오프셋과 높이(m). 귀 하나는 가려진 관절(null)로 둔다.
const JOINTS: [number, number, number][] = [
  [0.0, 0.1, 1.6],
  [-0.04, 0.09, 1.63],
  [0.04, 0.09, 1.63],
  [-0.08, 0.02, 1.61],
  [0.08, 0.02, 1.61],
  [-0.2, 0, 1.42],
  [0.2, 0, 1.42],
  [-0.28, 0, 1.15],
  [0.28, 0, 1.15],
  [-0.3, 0.05, 0.9],
  [0.3, 0.05, 0.9],
  [-0.12, 0, 0.95],
  [0.12, 0, 0.95],
  [-0.12, 0.02, 0.5],
  [0.12, 0.02, 0.5],
  [-0.12, 0, 0.08],
  [0.12, 0, 0.08],
];
const HIDDEN_JOINT = 3;
const UNCERTAIN_JOINT = 4;

function skeleton([x, y]: Xy) {
  return {
    joint_layout: 'COCO17',
    joints: JOINTS.map(([dx, dy, z], i) =>
      i === HIDDEN_JOINT ? null : [round(x + dx), round(y + dy), z],
    ),
    [SKELETON_VISIBLE_KEY]: JOINTS.map((_, i) => i !== HIDDEN_JOINT),
    validity: JOINTS.map((_, i) =>
      i === HIDDEN_JOINT ? 'UNAVAILABLE' : i === UNCERTAIN_JOINT ? 'UNCERTAIN' : 'VALID',
    ),
    joint_source: JOINTS.map(() => 'observed'),
  };
}

function observed(
  id: (typeof MOCK_TRACK_IDS)[number],
  tick: number,
  t: number,
  pose: typeof poseOf = poseOf,
) {
  const [x, y] = pose(id, tick).map(round) as Xy;
  const [nx, ny] = pose(id, tick + 1);
  return {
    world_id: id,
    t_last_valid: t,
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
    identity_confidence: 'HIGH',
    // w-0003 은 이전 ID w-0009 를 병합한 Track 이다(별칭 보존 확인용).
    identity_revision: id === 'w-0003' ? 1 : 0,
    ...(id === 'w-0003' ? { aliases: ['w-0009'] } : {}),
    frame_age_ms: 120,
    position_xy: [x, y],
    position_xy_sigma: [0.08, 0.08],
    pelvis_xyz: [x, y, 0.95],
    ground_anchor_xyz: [x, y, 0],
    ground_anchor_sigma: [0.06, 0.06, 0.03],
    surface_source: 'observed',
    support_surface_id: 'floor',
    support_surface_validity: 'VALID',
    contact_validity: 'VALID',
    z_validity: 'VALID',
    head_z: 1.68,
    head_z_sigma: 0.05,
    head_z_validity: 'VALID',
    velocity: [round((nx - x) * 10), round((ny - y) * 10), 0],
    cameras: ['cam1', 'cam3'],
    ...(id === 'w-0001' ? { skeleton: skeleton([x, y]) } : {}),
  };
}

/**
 * w-0002 관측 손실(주기 60): 0~19 OBSERVED, 20~29 PREDICTED(마지막 접지점을 inferred 로 유지), 30~39 PREDICTED(접지 모름),
 * 40~59 UNKNOWN. 위치는 마지막 관측 위치를 유지하고 불확실성만 2 m/s × 공백으로 키운다(명세 6.1).
 */
function lossTrack(tick: number, t0: number) {
  const p = tick % 60;
  if (p < 20) return observed('w-0002', tick, tAt(t0, tick));
  const lastTick = tick - p + 19;
  const tLast = tAt(t0, lastTick);
  const [x, y] = poseOf('w-0002', lastTick).map(round) as Xy;
  const gapS = (tAt(t0, tick) - tLast) / 1000;
  const sigma = round(0.08 + 2 * gapS);
  const keepAnchor = p < 30;
  return {
    world_id: 'w-0002',
    t_last_valid: tLast,
    observation_state: p < 40 ? 'PREDICTED' : 'UNKNOWN',
    track_lifecycle: 'OCCLUDED',
    identity_confidence: 'MEDIUM',
    identity_revision: 0,
    position_xy: [x, y],
    position_xy_sigma: [sigma, sigma],
    pelvis_xyz: null,
    ground_anchor_xyz: keepAnchor ? [x, y, 0] : null,
    ...(keepAnchor
      ? { ground_anchor_sigma: [sigma, sigma, 0.03], surface_source: 'inferred' }
      : {}),
    support_surface_id: keepAnchor ? 'floor' : null,
    // 접지점을 유지하는 동안 지지 표면 자체는 확인된 것이고(바닥), 접촉·높이만 UNCERTAIN 이다(계약 규칙).
    support_surface_validity: keepAnchor ? 'VALID' : 'UNKNOWN',
    contact_validity: keepAnchor ? 'UNCERTAIN' : 'UNAVAILABLE',
    z_validity: keepAnchor ? 'UNCERTAIN' : 'UNAVAILABLE',
    head_z: null,
    head_z_sigma: null,
    head_z_validity: 'UNAVAILABLE',
    cameras: [],
  };
}

/** 카메라 1대에만 보여 높이를 모르는 Track. Z 를 0 으로 채우지 않는다. */
function unknownZTrack(tick: number, t: number) {
  const [x, y] = poseOf('w-0003', tick).map(round) as Xy;
  return {
    world_id: 'w-0003',
    t_last_valid: t,
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
    identity_confidence: 'LOW',
    identity_revision: 0,
    position_xy: [x, y],
    position_xy_sigma: [0.4, 0.4],
    pelvis_xyz: null,
    ground_anchor_xyz: null,
    support_surface_id: null,
    support_surface_validity: 'UNKNOWN',
    contact_validity: 'UNAVAILABLE',
    z_validity: 'UNAVAILABLE',
    head_z: null,
    head_z_sigma: null,
    head_z_validity: 'UNAVAILABLE',
    cameras: ['cam2'],
  };
}

function sceneAt(key: ScenarioKey, tick: number) {
  const p = tick % PERIOD.disconnect;
  return key === 'disconnect' && p >= 41 && p < 50 ? NEXT_SCENE : VERSIONS.scene;
}

/** 시나리오의 tick 시점 World(world_state 메시지). 연결 상태와 관계없이 서버 쪽에 있는 값이다(REST 스냅샷도 이것을 쓴다). */
export function scenarioWorld(key: ScenarioKey, tick: number, t0: number) {
  const t = tAt(t0, tick);
  const tracks = MOCK_TRACK_IDS.map((id) => {
    if (key === 'observation-loss' && id === 'w-0002') return lossTrack(tick, t0);
    if (key === 'invalid-input' && id === 'w-0003') return unknownZTrack(tick, t);
    if (key === 'geofence') return observed(id, tick, t, geofencePose);
    return observed(id, tick, t);
  });
  return envelope(
    'world_state',
    mockMsgId(key, tick, 0),
    t + 20,
    {
      t_latest_global: t,
      calibration_version: VERSIONS.calibration,
      scene_version: sceneAt(key, tick),
      config_version: VERSIONS.config,
      input_provenance: {
        frames: { source: 'SIM', dataset_id: `mock/${key}` },
        tracks: { source: 'SIM', provider_version: 'spatial-mock-0.1.0' },
        is_upper_bound: true,
      },
      tracks,
    },
    '2.1.0',
  );
}

// ---- Health ----

function health(key: ScenarioKey, tick: number, t0: number, degraded: boolean) {
  const t = tAt(t0, tick);
  const live = sceneAt(key, tick);
  return envelope(
    'viewer_health',
    mockMsgId(key, tick, 1),
    t + 30,
    {
      as_of: t + 25,
      system_health: degraded ? 'DEGRADED' : 'NOMINAL',
      remote_state: 'NORMAL',
      tier0_link: 'CONNECTED',
      laptop: { link: 'CONNECTED', last_heartbeat_at: t },
      data_age_ms: degraded ? 2200 : 120,
      calibration_state: 'CALIBRATED',
      config: {
        published_version: VERSIONS.config,
        applied_version: VERSIONS.config,
        sync: 'CURRENT',
      },
      scene: {
        committed_version: VERSIONS.scene,
        live_version: live,
        mismatch: live !== VERSIONS.scene,
      },
      tier0_health: null,
    },
    '1.0.0',
  );
}

// ---- 위험 이벤트 ----

interface EventStep {
  n: number;
  seq: number | null;
  lifecycle: 'RAISED' | 'UPDATED' | 'RESOLVED';
  severity: 'CRITICAL' | 'HIGH';
  source?: 'LIVE' | 'PLANNED';
  /** 다른 msg_id 로 다시 보내는 늦은 전이(시험용). */
  slot: number;
}

const RULES: Record<
  number,
  {
    rule: string;
    zone: string;
    worldIds: string[];
    metrics: Record<string, number>;
    raisedAt: number;
  }
> = {
  1: { rule: 'R1', zone: 'z-01', worldIds: ['w-0001'], metrics: { dwell_s: 1.4 }, raisedAt: 10 },
  2: {
    rule: 'R2',
    zone: 'z-02',
    worldIds: ['w-0001', 'w-0002'],
    metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 },
    raisedAt: 30,
  },
  3: { rule: 'R1', zone: 'z-01', worldIds: ['w-0003'], metrics: { dwell_s: 0.6 }, raisedAt: 40 },
};

// 주기 80 안에서의 전이. E1·E2 가 같은 사람(w-0001)에 겹친다. E3 은 PLANNED 출처라 LIVE 와 섞이지 않아야 한다.
const RISK_STEPS: Record<number, EventStep[]> = {
  10: [{ n: 1, seq: 1, lifecycle: 'RAISED', severity: 'HIGH', slot: 2 }],
  20: [{ n: 1, seq: 2, lifecycle: 'UPDATED', severity: 'CRITICAL', slot: 2 }],
  30: [{ n: 2, seq: 1, lifecycle: 'RAISED', severity: 'CRITICAL', slot: 3 }],
  35: [{ n: 1, seq: 2, lifecycle: 'UPDATED', severity: 'CRITICAL', slot: 4 }],
  40: [{ n: 3, seq: null, lifecycle: 'RAISED', severity: 'HIGH', source: 'PLANNED', slot: 5 }],
  50: [{ n: 1, seq: 3, lifecycle: 'RESOLVED', severity: 'CRITICAL', slot: 2 }],
  56: [{ n: 1, seq: 2, lifecycle: 'UPDATED', severity: 'CRITICAL', slot: 6 }],
  65: [{ n: 2, seq: 2, lifecycle: 'RESOLVED', severity: 'CRITICAL', slot: 3 }],
  // 계획 이벤트도 주기 안에서 해소해 다음 주기로 쌓이지 않게 한다.
  75: [{ n: 3, seq: null, lifecycle: 'RESOLVED', severity: 'HIGH', source: 'PLANNED', slot: 5 }],
};

function riskEvent(step: EventStep, tick: number, t0: number) {
  const cycle = Math.floor(tick / PERIOD['risk-lifecycle']);
  const rule = RULES[step.n]!;
  const cycleStart = cycle * PERIOD['risk-lifecycle'];
  const raisedAt = tAt(t0, cycleStart + rule.raisedAt);
  const t = tAt(t0, tick);
  const source = step.source ?? 'LIVE';
  const uncertain = step.n === 2;
  return envelope(
    'risk_event',
    mockMsgId('risk-lifecycle', tick, step.slot),
    t + 40,
    {
      event_id: mockEventId(cycle, step.n),
      ...(step.seq !== null ? { event_sequence: step.seq } : {}),
      rule_id: rule.rule,
      rule_version: 1,
      source,
      lifecycle: step.lifecycle,
      verification: 'VERIFIED',
      severity: step.severity,
      confidence: uncertain ? 'LOW' : 'HIGH',
      needs_check: uncertain,
      confidence_reasons: uncertain ? ['PREDICTED'] : [],
      resume_count: 0,
      world_ids: rule.worldIds,
      zone_id: rule.zone,
      metrics: rule.metrics,
      rationale: null,
      proposed_severity: null,
      scene_version: VERSIONS.scene,
      raised_at: raisedAt,
      updated_at: t,
      resolved_at: step.lifecycle === 'RESOLVED' ? t : null,
      ...(source === 'LIVE' ? { gateway_id: 'tier0-mock', delivery: 'REALTIME' } : {}),
    },
    '2.1.0',
  );
}

// ---- 잘못된 입력 ----

// ---- 지오펜싱 정합 시연 ----
// 판정 결과를 미리 적어 둔 Fixture 다. 브라우저는 판정하지 않는다. 시험(zoneContainment)이 경로와 맞는지 따로 확인한다.

const GEOFENCE_RULES: Record<number, { zone: string; worldIds: string[]; entry: number }> = {
  11: { zone: 'z-02', worldIds: ['w-0001'], entry: 26 },
  12: { zone: 'z-01', worldIds: ['w-0003'], entry: 11 },
};

interface GeofenceStep {
  n: 11 | 12;
  seq: number;
  lifecycle: 'RAISED' | 'UPDATED' | 'RESOLVED';
  severity: 'CRITICAL' | 'HIGH';
}

/** 주기 120 안의 전이. 진입한 tick 에 발생, 2초 머물면 상향, 나간 첫 tick 에 해소. */
export const GEOFENCE_STEPS: Record<number, GeofenceStep[]> = {
  11: [{ n: 12, seq: 1, lifecycle: 'RAISED', severity: 'HIGH' }],
  26: [{ n: 11, seq: 1, lifecycle: 'RAISED', severity: 'HIGH' }],
  46: [{ n: 11, seq: 2, lifecycle: 'UPDATED', severity: 'CRITICAL' }],
  75: [{ n: 12, seq: 2, lifecycle: 'RESOLVED', severity: 'HIGH' }],
  85: [{ n: 11, seq: 3, lifecycle: 'RESOLVED', severity: 'CRITICAL' }],
};

function geofenceEvent(step: GeofenceStep, tick: number, t0: number) {
  const cycle = Math.floor(tick / PERIOD.geofence);
  const rule = GEOFENCE_RULES[step.n]!;
  const entryTick = cycle * PERIOD.geofence + rule.entry;
  const t = tAt(t0, tick);
  return envelope(
    'risk_event',
    mockMsgId('geofence', tick, 2 + (step.n - 11)),
    t + 40,
    {
      event_id: mockEventId(cycle, step.n),
      event_sequence: step.seq,
      rule_id: 'R1',
      rule_version: 1,
      source: 'LIVE',
      lifecycle: step.lifecycle,
      verification: 'VERIFIED',
      severity: step.severity,
      confidence: 'HIGH',
      needs_check: false,
      confidence_reasons: [],
      resume_count: 0,
      world_ids: rule.worldIds,
      zone_id: rule.zone,
      metrics: { dwell_s: round((tick - entryTick) / 10) },
      rationale: null,
      proposed_severity: null,
      scene_version: VERSIONS.scene,
      raised_at: tAt(t0, entryTick),
      updated_at: t,
      resolved_at: step.lifecycle === 'RESOLVED' ? t : null,
      gateway_id: 'tier0-mock',
      delivery: 'REALTIME',
    },
    '2.1.0',
  );
}

/** 주기 10 안의 위치별 계약 오류. 모두 거부돼야 한다. */
export const INVALID_STEPS = [2, 4, 6, 8, 9] as const;

function invalidMessage(p: number, tick: number, t0: number): unknown {
  const t = tAt(t0, tick);
  const id = mockMsgId('invalid-input', tick, 8);
  const world = scenarioWorld('invalid-input', tick, t0);
  const firstTrack = world.payload.tracks[0] as Record<string, unknown>;
  switch (p) {
    case 2:
      return 'not-a-message';
    case 4:
      return envelope('mystery_type', id, t, {}, '1.0.0');
    case 6:
      // 골반 좌표가 2개뿐이다.
      return {
        ...world,
        msg_id: id,
        payload: { ...world.payload, tracks: [{ ...firstTrack, pelvis_xyz: [1, 2] }] },
      };
    case 8:
      // 높이를 모르는데 접지점 Z 를 0 으로 채웠다(Z=0 보정 금지).
      return {
        ...world,
        msg_id: id,
        payload: {
          ...world.payload,
          tracks: [{ ...firstTrack, z_validity: 'UNAVAILABLE', contact_validity: 'UNAVAILABLE' }],
        },
      };
    default:
      return envelope(
        'risk_event',
        id,
        t,
        { ...(riskEvent(RISK_STEPS[10]![0]!, 10, t0).payload as object), severity: 'EXTREME' },
        '2.1.0',
      );
  }
}

/** tick 시점에 Source 가 내보낼 것. */
export function scenarioTick(key: ScenarioKey, tick: number, t0: number): ScenarioEmit[] {
  const p = tick % PERIOD[key];
  const out: ScenarioEmit[] = [];
  const message = (raw: unknown) => out.push({ type: 'message', raw });

  if (key === 'disconnect') {
    if (p === 20) return [{ type: 'connection', state: 'CLOSED', reason: 'Mock: EC2 연결 끊김' }];
    if (p > 20 && p < 40) return [];
    if (p === 40) return [{ type: 'connection', state: 'CONNECTING', reason: 'Mock: 재연결 시도' }];
    if (p === 41) {
      out.push({ type: 'connection', state: 'OPEN', reason: 'Mock: 재연결' });
      // 늦게 도착한 끊기기 전 World(새 msg_id, 이전 시각)와 끊기기 직전 World 의 재전송(같은 msg_id).
      const stale = scenarioWorld(key, tick - 25, t0);
      message({ ...stale, msg_id: mockMsgId(key, tick, 9) });
      message(scenarioWorld(key, tick - 22, t0));
      message(health(key, tick, t0, true));
    }
  }

  message(scenarioWorld(key, tick, t0));
  if (p % HEALTH_EVERY === 0) message(health(key, tick, t0, false));

  if (key === 'risk-lifecycle') {
    for (const step of RISK_STEPS[p] ?? []) message(riskEvent(step, tick, t0));
  }
  if (key === 'geofence') {
    for (const step of GEOFENCE_STEPS[p] ?? []) message(geofenceEvent(step, tick, t0));
  }
  if (key === 'invalid-input' && (INVALID_STEPS as readonly number[]).includes(p)) {
    message(invalidMessage(p, tick, t0));
  }
  return out;
}

// ---- 구역(DEMO 형상) ----

/**
 * GET /api/zones 의 DEMO 응답. 실제 응답 형상 계약이 아직 없어(BE -120·FE -189) contract 표시로 DEMO 임을 밝힌다.
 * z-03 은 바닥에서 떨어진 공중 구역, z-bad 는 min 이 max 보다 커서 거부돼야 하는 구역이다.
 */
export const DEMO_ZONES_RESPONSE = {
  contract: 'DEMO_UNCONFIRMED',
  note: 'Spatial 개발용 DEMO 구역. 실제 GET /api/zones 응답 형상이 아니다.',
  zones: [
    {
      zone_id: 'z-01',
      name: '예시 구역 A',
      aabb: { min_xyz: [1.5, 5.5, 0], max_xyz: [4.5, 8.5, 2] },
    },
    {
      zone_id: 'z-02',
      name: '예시 구역 B',
      aabb: { min_xyz: [6.5, 1, 0], max_xyz: [9, 3.5, 1.4] },
    },
    {
      zone_id: 'z-03',
      name: '예시 구역 C(공중)',
      aabb: { min_xyz: [4.5, 7.5, 1.2], max_xyz: [6.5, 9, 2.4] },
    },
    { zone_id: 'z-bad', name: '형상 오류 예시', aabb: { min_xyz: [3, 3, 1], max_xyz: [2, 4, 2] } },
  ],
};
