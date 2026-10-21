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
    cameras: ['cam1', 'cam2', 'cam3'],
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
        frames: { source: 'LIVE', camera_ids: [...CAMERAS] },
        calibration: { source: 'G1_AUTO', calibration_version: VERSIONS.calibration },
        tracks: { source: 'TIER0_LIVE', provider_version: 'tier0-0.1.0' },
        is_upper_bound: false,
      },
      tracks: WORKERS.map((worker) => track(worker, t)),
    },
  };
}

/** /topic/health: 시각 t(ms)의 관제 상태. 카메라 4대 정상, 데이터 연령은 0.2초 안팎. */
export function viewerHealthAt(t: number): ViewerHealthMessage {
  return {
    schema_version: '1.0.0',
    msg_type: 'viewer_health',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: 'ec2',
    payload: {
      as_of: t - 10,
      system_health: 'NOMINAL',
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
        system_health: 'NOMINAL',
        t_latest_global: t - 120,
        cameras: CAMERAS.map((id) => ({
          id,
          fps: 14.8,
          last_capture: t - 120,
          sync_offset_ms: 6,
          status: 'OK',
        })),
        pipeline_lag_ms: 120,
        calibration_state: 'CALIBRATED',
        calibration_version: VERSIONS.calibration,
        config_version: VERSIONS.config,
        config_sync: 'CURRENT',
      },
    },
  };
}

/** 위험 이벤트 한 바퀴(ms): 발생 → 갱신 → 해소. */
export const EVENT_CYCLE_MS = 40_000;
const EVENT_STEPS = [
  { at: 10_000, lifecycle: 'RAISED', sequence: 1, edge: 0.82 },
  { at: 16_000, lifecycle: 'UPDATED', sequence: 2, edge: 0.55 },
  { at: 26_000, lifecycle: 'RESOLVED', sequence: 3, edge: 1.4 },
] as const;

function riskEvent(cycle: number, step: (typeof EVENT_STEPS)[number], t: number): RiskEventMessage {
  const raisedAt = cycle * EVENT_CYCLE_MS + EVENT_STEPS[0].at;
  return {
    schema_version: '1.3.0',
    msg_type: 'risk_event',
    msg_id: nextMsgId(),
    sent_at: t,
    source_id: 'ec2',
    payload: {
      // 바퀴마다 다른 이벤트(UUIDv7 형식).
      event_id: `0192a6f0-7c1e-7b3a-9f2d-${hex12(cycle + 1)}`,
      event_sequence: step.sequence,
      rule_id: 'R2',
      rule_version: 3,
      source: 'LIVE',
      lifecycle: step.lifecycle,
      verification: 'VERIFIED',
      severity: 'CRITICAL',
      confidence: 'HIGH',
      needs_check: false,
      confidence_reasons: [],
      resume_count: 0,
      world_ids: ['w-0015'],
      zone_id: 'z-04',
      metrics: { edge_distance_m: step.edge, height_diff_m: 2.1 },
      rationale: null,
      proposed_severity: null,
      raised_at: raisedAt,
      updated_at: t,
      ...(step.lifecycle === 'RESOLVED' ? { resolved_at: t } : {}),
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
    for (const step of EVENT_STEPS) {
      const at = cycle * EVENT_CYCLE_MS + step.at;
      if (at > from && at <= to) out.push(riskEvent(cycle, step, at));
    }
  }
  return out;
}
