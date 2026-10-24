import type { MetricsResponse } from '@/features/system/types';
import type { HealthSnapshot } from '@/store/realtime.store';
import { z } from 'zod';

// 시스템 상태 화면(명세 3.11, FR-13)이 보일 값을 만든다. 화면 컴포넌트 없이 시험한다.
// tier0_health(I1 health 원문)는 계약이 객체인지까지만 검사하므로 화면이 쓰는 칸만 느슨하게 읽는다.
// 칸마다 따로 읽어, 형식이 틀린 칸만 비우고 나머지는 그린다. 없는 값은 undefined(빈칸)로 돌려준다.
// 0 으로 채우지 않는다.

const num = z.number().nullish().catch(undefined);
const text = z.string().nullish().catch(undefined);
const pair = z.object({ p50: num, p95: num }).nullish().catch(undefined);

const cameraSchema = z.object({
  // 행을 가리킬 수 없으면 뺀다. 나머지 칸은 틀려도 그 칸만 비운다.
  id: z.string(),
  status: text,
  fps: num,
  sync_offset_ms: num,
  queue_wait_ms: pair,
  frames: z
    .object({
      camera_to_gateway_gap_max_ms: num,
      gateway_to_runpod_loss: num,
      skipped_by_policy: num,
    })
    .nullish()
    .catch(undefined),
});

const tier0Schema = z.object({
  // 카메라 하나가 틀려도 나머지는 그린다. 항목은 cameraRows 에서 하나씩 검사한다.
  cameras: z.array(z.unknown()).nullish().catch(undefined),
  pipeline_lag_ms: num,
  capture_to_risk_ms: pair,
  gpu_util: num,
  gpu_memory_used_ratio: num,
  gateway_link: text,
});

type Tier0 = z.infer<typeof tier0Schema>;

// 같은 health 를 여러 칸이 읽으므로 받은 메시지 하나에 한 번만 검사한다.
const tier0Cache = new WeakMap<HealthSnapshot, Tier0 | undefined>();

/**
 * 지금의 Tier 0 health. tier0_health 는 EC2 가 마지막으로 받은 값이라, EC2 ↔ Tier 0 가 끊기면
 * 지금 값이 아니다. 그때는 비운다.
 */
function tier0(health: HealthSnapshot | null): Tier0 | undefined {
  if (!health || health.payload.tier0_link !== 'CONNECTED') return undefined;
  if (!tier0Cache.has(health)) {
    tier0Cache.set(health, tier0Schema.safeParse(health.payload.tier0_health).data);
  }
  return tier0Cache.get(health);
}

const ms = (value: number | null | undefined) =>
  value === null || value === undefined ? undefined : `${Math.round(value)} ms`;
const count = (value: number | null | undefined) =>
  value === null || value === undefined ? undefined : `${value}`;
const percent = (ratio: number | null | undefined) =>
  ratio === null || ratio === undefined ? undefined : `${Math.round(ratio * 100)}%`;

const CAMERA_STATUS: Record<string, string> = {
  OK: '정상',
  NO_SIGNAL: '신호 없음',
  WAITING_KEYFRAME: '키프레임 대기',
  SYNC_INVALID: '동기 불가',
};

const LINK: Record<string, string> = { CONNECTED: '연결됨', DISCONNECTED: '끊김' };

/** 연결 상태 문구(CONNECTED·DISCONNECTED). 모르는 값은 그대로 보인다. */
export const linkLabel = (link: string | null | undefined) =>
  link === null || link === undefined ? undefined : (LINK[link] ?? link);

export interface CameraRow {
  id: string;
  status?: string;
  ok: boolean;
  fps?: string;
  syncOffset?: string;
  /** 카메라 → 게이트웨이 간격 최대(ms). */
  gapMax?: string;
  /** 게이트웨이 → RunPod 전송 손실(장). */
  runpodLoss?: string;
  /** 정책으로 건너뛴 프레임(장). */
  skipped?: string;
  queueWaitP95?: string;
}

/** 카메라별 상태와 프레임 손실 3종(FR-13). id 가 없는 카메라는 뺀다. */
export function cameraRows(health: HealthSnapshot | null): CameraRow[] {
  const cameras = tier0(health)?.cameras ?? [];
  return cameras.flatMap((item) => {
    const camera = cameraSchema.safeParse(item);
    if (!camera.success) return [];
    const { id, status, fps, sync_offset_ms, queue_wait_ms, frames } = camera.data;
    return [
      {
        id,
        status:
          status === null || status === undefined ? undefined : (CAMERA_STATUS[status] ?? status),
        ok: status === 'OK',
        fps: fps === null || fps === undefined ? undefined : fps.toFixed(1),
        syncOffset: ms(sync_offset_ms),
        gapMax: ms(frames?.camera_to_gateway_gap_max_ms),
        runpodLoss: count(frames?.gateway_to_runpod_loss),
        skipped: count(frames?.skipped_by_policy),
        queueWaitP95: ms(queue_wait_ms?.p95),
      },
    ];
  });
}

export interface LatencyRow {
  key: string;
  label: string;
  p50?: string;
  p95?: string;
}

type Pair = { p50?: number | null; p95?: number | null } | null | undefined;

const row = (key: string, label: string, value: Pair): LatencyRow => ({
  key,
  label,
  p50: ms(value?.p50),
  p95: ms(value?.p95),
});

/**
 * 구간별 지연(명세 6.6 촬영 → 판정·현장 소리·관제 화면·반장 PWA). 촬영 → 판정은 실시간 health 에서,
 * 나머지는 지표 API(관리자)에서 받는다.
 */
export function latencyRows(
  health: HealthSnapshot | null,
  metrics: MetricsResponse | undefined,
): LatencyRow[] {
  const stages = metrics?.stages;
  const remote = stages?.capture_to_remote_delivery_ms;
  return [
    row('risk', '촬영 → 판정', tier0(health)?.capture_to_risk_ms),
    row('site', '촬영 → 현장 소리', stages?.capture_to_site_alert_ms),
    row('viewer', '촬영 → 관제 화면', remote?.viewer),
    row('pwa', '촬영 → 반장 PWA', remote?.pwa),
  ];
}

export interface StatusRow {
  key: string;
  label: string;
  value?: string;
  /** 지연 값이면 주황으로 보인다(위험 등급 색을 쓰지 않는다). */
  delay?: boolean;
  /** 시스템 이상(장면 불일치)이면 장애 띠와 같은 줄무늬로 보인다. */
  warn?: boolean;
}

/**
 * 연결 세 갈래. 노트북 ↔ Tier 0 는 I0 제어 채널(Tier 0 가 알린 gateway_link), 노트북 ↔ EC2 는
 * 노트북 하트비트(laptop.link), Tier 0 ↔ EC2 는 I1(tier0_link)이다.
 */
export function connectionRows(health: HealthSnapshot | null): StatusRow[] {
  const payload = health?.payload;
  return [
    { key: 'gateway', label: '노트북 ↔ Tier 0', value: linkLabel(tier0(health)?.gateway_link) },
    { key: 'laptop', label: '노트북 ↔ EC2', value: linkLabel(payload?.laptop.link) },
    { key: 'tier0', label: 'Tier 0 ↔ EC2', value: linkLabel(payload?.tier0_link) },
  ];
}

/** 처리 상태: 파이프라인 지연, GPU. */
export function processingRows(health: HealthSnapshot | null): StatusRow[] {
  const t0 = tier0(health);
  return [
    { key: 'lag', label: '파이프라인 지연', value: ms(t0?.pipeline_lag_ms), delay: true },
    { key: 'gpu', label: 'GPU 사용률', value: percent(t0?.gpu_util) },
    { key: 'gpu-memory', label: 'GPU 메모리', value: percent(t0?.gpu_memory_used_ratio) },
  ];
}
