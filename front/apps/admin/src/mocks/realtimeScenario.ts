import type {
  RiskEventMessage,
  ViewerHealthMessage,
  WorldStateMessage,
  WorldTrack,
} from '@simai/contracts';

// 개발 서버 mock 이 STOMP 로 보내는 메시지. 계약 예제(contracts/examples)를 바탕으로 시각과 위치만 바꾼다.
// 시험(__tests__/mocks/realtimeScenario.test.ts)이 만든 메시지를 모두 계약 검증기로 검사한다.

const VERSIONS = { calibration: 'cal-7', scene: 'scene-3', config: 'cfg-12' } as const;
const CAMERAS = ['cam1', 'cam2', 'cam3', 'cam4'] as const;

/** 교실(10×10m) 안을 원을 그리며 걷는 작업자. 주기(ms)마다 한 바퀴 돈다. */
const WORKERS = [
  { id: 'w-0012', cx: 3.0, cy: 5.0, r: 1.6, periodMs: 24_000, phase: 0 },
  { id: 'w-0015', cx: 6.6, cy: 3.0, r: 1.2, periodMs: 18_000, phase: 0.4 },
  { id: 'w-0021', cx: 6.0, cy: 7.2, r: 1.8, periodMs: 30_000, phase: 0.7 },
] as const;

const r2 = (value: number) => Math.round(value * 100) / 100;
const hex12 = (n: number) => (n % 2 ** 48).toString(16).padStart(12, '0');

let messages = 0;
/** 메시지마다 다른 msg_id(UUID 형식). */
const nextMsgId = () => `0192a6f0-7c1e-4b3a-9f2d-${hex12(++messages)}`;

/** 바퀴(EVENT_CYCLE_MS) 안에서 cam3 가 키프레임을 기다리는 구간(ms). 시스템 상태 화면의 빈칸·비정상 카메라를 보인다. */
const CAM3_DOWN = { from: 20_000, to: 26_000 } as const;

const cam3DownAt = (t: number) => {
  const inCycle = t % EVENT_CYCLE_MS;
  return inCycle >= CAM3_DOWN.from && inCycle < CAM3_DOWN.to;
};

/** 시각 t 에 영상을 보내는 카메라만 남긴다. cam3 가 키프레임을 기다리는 동안은 cam3 로 관측하지 않는다. */
const sending = (ids: readonly string[], t: number) =>
  ids.filter((id) => id !== 'cam3' || !cam3DownAt(t));

function track(worker: (typeof WORKERS)[number], t: number): WorldTrack {
  const turn = (t / worker.periodMs + worker.phase) % 1;
  const angle = 2 * Math.PI * turn;
  const x = r2(worker.cx + worker.r * Math.cos(angle));
  const y = r2(worker.cy + worker.r * Math.sin(angle));
  const speed = (2 * Math.PI * worker.r) / (worker.periodMs / 1000);
  return {
    world_id: worker.id,
    t_last_valid: t - 140,
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
    identity_confidence: 'HIGH',
    identity_revision: 0,
    frame_age_ms: 140,
    position_xy: [x, y],
    position_xy_sigma: [0.08, 0.08],
    pelvis_xyz: [x, y, 0.92],
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
    velocity: [r2(-speed * Math.sin(angle)), r2(speed * Math.cos(angle)), 0],
    cameras: sending(['cam1', 'cam2', 'cam3'], t),
  };
}

/** /topic/world: 시각 t(ms)의 World 상태. EC2 가 봉투만 새로 쓴다(source_id ec2, 명세 12.4). */
export function worldStateAt(t: number): WorldStateMessage {
  return {
    schema_version: '2.0.0',
    msg_type: 'world_state',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: 'ec2',
    payload: {
      t_latest_global: t - 120,
      calibration_version: VERSIONS.calibration,
      scene_version: VERSIONS.scene,
      config_version: VERSIONS.config,
      input_provenance: {
        frames: { source: 'LIVE', camera_ids: sending(CAMERAS, t) },
        calibration: { source: 'G1_AUTO', calibration_version: VERSIONS.calibration },
        tracks: { source: 'TIER0_LIVE', provider_version: 'tier0-0.1.0' },
        is_upper_bound: false,
      },
      tracks: WORKERS.map((worker) => track(worker, t)),
    },
  };
}

/**
 * 카메라 한 대의 Tier 0 health(계약 예제 contracts/examples/viewer_health.json 모양). 키프레임을 기다리면
 * 처리 FPS 는 0 이고(계약 필수 값), 잴 수 없는 값은 null 이거나 싣지 않는다(예제의 cam2 와 같다).
 */
function cameraHealth(id: (typeof CAMERAS)[number], index: number, t: number, down: boolean) {
  if (down) {
    return {
      id,
      fps: 0,
      last_capture: null,
      sync_offset_ms: null,
      sync_valid: null,
      status: 'WAITING_KEYFRAME',
    };
  }
  const wave = Math.floor(t / 1000 + index * 3) % 5;
  return {
    id,
    fps: r2(14.6 + wave * 0.1),
    last_capture: t - 120,
    sync_offset_ms: 4 + index * 2,
    sync_valid: true,
    status: 'OK',
    frame_age_ms: { p50: 120 + wave * 5, p95: 200 + wave * 10 },
    queue_wait_ms: { p50: 8 + index, p95: 36 + wave * 4 },
    frames: {
      received: 25,
      gateway_to_runpod_loss: index === 1 ? wave % 2 : 0,
      skipped_by_policy: 10,
      laptop_skipped: 4,
      unattributed_gap: 0,
      late_dropped: 0,
      camera_to_gateway_gap_max_ms: 70 + wave * 10,
    },
  };
}

/**
 * /topic/health: 시각 t(ms)의 관제 상태. 데이터 연령은 0.2초 안팎이고, 바퀴마다 cam3 가 잠깐 키프레임을 기다린다.
 * faults 가 false 면 고장 흉내 없이 늘 정상이다(시각과 상관없이 NOMINAL 을 기대하는 시험).
 */
export function viewerHealthAt(
  t: number,
  { faults = true }: { faults?: boolean } = {},
): ViewerHealthMessage {
  const cam3Down = faults && cam3DownAt(t);
  const system = cam3Down ? 'DEGRADED' : 'NOMINAL';
  const wave = Math.floor(t / 1000) % 5;
  return {
    schema_version: '1.0.0',
    msg_type: 'viewer_health',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: 'ec2',
    payload: {
      as_of: t - 10,
      system_health: system,
      remote_state: 'NORMAL',
      tier0_link: 'CONNECTED',
      laptop: { link: 'CONNECTED', last_heartbeat_at: t - 200 },
      data_age_ms: 180 + (Math.floor(t / 1000) % 5) * 15,
      calibration_state: 'CALIBRATED',
      config: {
        published_version: VERSIONS.config,
        applied_version: VERSIONS.config,
        sync: 'CURRENT',
      },
      scene: { committed_version: VERSIONS.scene, live_version: VERSIONS.scene, mismatch: false },
      tier0_health: {
        system_health: system,
        t_latest_global: t - 120,
        cameras: CAMERAS.map((id, index) => cameraHealth(id, index, t, id === 'cam3' && cam3Down)),
        pipeline_lag_ms: 110 + wave * 5,
        capture_to_risk_ms: { p50: 300 + wave * 5, p95: 460 + wave * 10 },
        calibration_state: 'CALIBRATED',
        calibration_version: VERSIONS.calibration,
        config_version: VERSIONS.config,
        config_sync: 'CURRENT',
        gateway_link: 'CONNECTED',
        gpu_util: r2(0.68 + wave * 0.01),
        gpu_memory_used_ratio: 0.62,
      },
    },
  };
}

/** 위험 이벤트 한 바퀴(ms). 바퀴마다 아래 이벤트들이 같은 순서로 일어난다. */
export const EVENT_CYCLE_MS = 40_000;

type Payload = RiskEventMessage['payload'];
type Base = Pick<Payload, 'rule_id' | 'severity' | 'world_ids' | 'zone_id'>;

/**
 * - edge: w-0015 개구부 근접 CRITICAL(발생 → 가까워짐 → 해소)
 * - zone: 같은 사람 w-0015 의 위험 구역 진입 HIGH(한 묶음 표시)
 * - predict: w-0012 진입 예측 CAUTION(안내만, 소리 없음)
 * - unsure: w-0021 높이를 몰라 확인 필요인 HIGH(해소 → 재개 → 해소)
 */
const EVENTS: Record<'edge' | 'zone' | 'predict' | 'unsure', Base> = {
  edge: { rule_id: 'R2', severity: 'CRITICAL', world_ids: ['w-0015'], zone_id: 'z-04' },
  zone: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0015'], zone_id: 'z-02' },
  predict: { rule_id: 'R5', severity: 'CAUTION', world_ids: ['w-0012'], zone_id: 'z-02' },
  unsure: { rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0021'], zone_id: 'z-04' },
};
type EventKey = keyof typeof EVENTS;

const UNSURE: Partial<Payload> = {
  verification: 'UNVERIFIABLE',
  confidence: 'LOW',
  needs_check: true,
  confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
};

interface Step {
  at: number;
  key: EventKey;
  lifecycle: Payload['lifecycle'];
  metrics: Payload['metrics'];
  change?: Partial<Payload>;
}

const STEPS: Step[] = [
  { at: 4_000, key: 'predict', lifecycle: 'RAISED', metrics: { time_to_entry_s: 1.6 } },
  { at: 8_000, key: 'predict', lifecycle: 'RESOLVED', metrics: { time_to_entry_s: 3.2 } },
  {
    at: 10_000,
    key: 'edge',
    lifecycle: 'RAISED',
    metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 },
  },
  { at: 12_000, key: 'zone', lifecycle: 'RAISED', metrics: { zone_distance_m: 0 } },
  {
    at: 16_000,
    key: 'edge',
    lifecycle: 'UPDATED',
    metrics: { edge_distance_m: 0.55, height_diff_m: 2.1 },
  },
  {
    at: 20_000,
    key: 'unsure',
    lifecycle: 'RAISED',
    metrics: { zone_distance_m: 0 },
    change: UNSURE,
  },
  { at: 22_000, key: 'zone', lifecycle: 'RESOLVED', metrics: { zone_distance_m: 0.6 } },
  {
    at: 26_000,
    key: 'edge',
    lifecycle: 'RESOLVED',
    metrics: { edge_distance_m: 1.4, height_diff_m: 2.1 },
  },
  // 관측 해소는 관측으로 확인했을 때만이라 해소 단계는 VERIFIED 다(계약 risk_event, 명세 6.4).
  { at: 27_000, key: 'unsure', lifecycle: 'RESOLVED', metrics: { zone_distance_m: 0.5 } },
  {
    at: 31_000,
    key: 'unsure',
    lifecycle: 'UPDATED',
    metrics: { zone_distance_m: 0 },
    change: { ...UNSURE, resume_count: 1, resolved_at: null },
  },
  {
    at: 36_000,
    key: 'unsure',
    lifecycle: 'RESOLVED',
    metrics: { zone_distance_m: 0.7 },
    change: { resume_count: 1 },
  },
];

const KEYS = Object.keys(EVENTS) as EventKey[];

/** 바퀴·이벤트마다 다른 event_id(UUIDv7 형식). */
export const mockEventId = (cycle: number, key: EventKey) =>
  `0192a6f0-7c1e-7b3a-9f2d-${hex12(cycle * KEYS.length + KEYS.indexOf(key) + 1)}`;

function riskEvent(cycle: number, step: Step, t: number): RiskEventMessage {
  const start = cycle * EVENT_CYCLE_MS;
  const same = STEPS.filter((s) => s.key === step.key);
  const raisedAt = start + same[0]!.at;
  return {
    schema_version: '2.1.0',
    msg_type: 'risk_event',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: 'ec2',
    payload: {
      event_id: mockEventId(cycle, step.key),
      event_sequence: same.indexOf(step) + 1,
      rule_version: 3,
      source: 'LIVE',
      verification: 'VERIFIED',
      confidence: 'HIGH',
      needs_check: false,
      ...EVENTS[step.key],
      lifecycle: step.lifecycle,
      metrics: step.metrics,
      rationale: null,
      proposed_severity: null,
      raised_at: raisedAt,
      updated_at: t,
      ...(step.lifecycle === 'RESOLVED' ? { resolved_at: t } : {}),
      ...step.change,
    },
  };
}

/** /topic/events: (from, to] 사이에 일어난 이벤트 전이. 같은 구간을 두 번 넘기지 않으면 전이마다 한 번씩 나온다. */
export function riskEventsBetween(from: number, to: number): RiskEventMessage[] {
  const out: RiskEventMessage[] = [];
  for (
    let cycle = Math.floor(from / EVENT_CYCLE_MS);
    cycle <= Math.floor(to / EVENT_CYCLE_MS);
    cycle++
  ) {
    for (const step of STEPS) {
      const at = cycle * EVENT_CYCLE_MS + step.at;
      if (at > from && at <= to) out.push(riskEvent(cycle, step, at));
    }
  }
  return out;
}

/** 다른 mock(이벤트 저장소)이 쓰는 msg_id. */
export { nextMsgId };
