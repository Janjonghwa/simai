import type { HealthSnapshot, RealtimeState } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { z } from 'zod';

// 상단 바에 보이는 값을 viewer_health(명세 12.4 /topic/health)에서 만든다. 화면 컴포넌트 없이 시험한다.

export type SystemHealth = ViewerHealthPayload['system_health'];

/** 상태 칩. LOST 는 EC2 와 연결이 끊겨 지금 상태를 모르는 것이다. undefined 는 아직 연결 전이다. */
export type HealthChipState = SystemHealth | 'LOST';

/** alive: EC2 와 이어져 있는지(isLinkAlive). */
export function chipState(
  state: Pick<RealtimeState, 'phase' | 'openedAt' | 'health'>,
  alive: boolean,
): HealthChipState | undefined {
  if (state.phase === 'IDLE' || state.openedAt === null) return undefined;
  if (!alive) return 'LOST';
  return state.health?.payload.system_health;
}

// tier0_health 는 계약이 객체인지까지만 검사한다(I1 health 를 그대로 싣는다). 상단 바가 쓰는 칸만 따로 확인한다.
const tier0Schema = z.object({
  cameras: z.array(z.object({ status: z.string() })),
  calibration_version: z.string().nullish(),
});

type Tier0 = z.infer<typeof tier0Schema>;
// 같은 health 를 여러 칸이 읽으므로 받은 메시지 하나에 한 번만 검사한다.
const tier0Cache = new WeakMap<HealthSnapshot, Tier0 | undefined>();

// tier0_health 는 EC2 가 마지막으로 받은 값이다. EC2 ↔ Tier 0 가 끊기면 지금 값이 아니라 읽지 않는다.
function tier0(health: HealthSnapshot | null): Tier0 | undefined {
  if (!health || health.payload.tier0_link !== 'CONNECTED') return undefined;
  if (!tier0Cache.has(health)) {
    tier0Cache.set(health, tier0Schema.safeParse(health.payload.tier0_health).data);
  }
  return tier0Cache.get(health);
}

/** 정상(OK) 카메라 / 전체. */
export function cameraLabel(health: HealthSnapshot | null): string | undefined {
  const cameras = tier0(health)?.cameras;
  if (!cameras) return undefined;
  return `${cameras.filter((c) => c.status === 'OK').length}/${cameras.length}`;
}

/** 보정 상태와 버전(예: CALIBRATED cal-7). */
export function calibrationLabel(health: HealthSnapshot | null): string | undefined {
  if (!health) return undefined;
  const version = tier0(health)?.calibration_version;
  return version
    ? `${health.payload.calibration_state} ${version}`
    : health.payload.calibration_state;
}

const SYNC_LABEL = {
  PENDING: '적용 대기',
  UNVERIFIED: '적용 미확인',
  STALE: '적용 지연',
} as const;

/** 현장에 적용된 구성 버전. 아직 적용되지 않았으면 발행한 버전과 상태를 보인다(명세 FR-17). */
export function configLabel(health: HealthSnapshot | null): string | undefined {
  if (!health) return undefined;
  const { published_version, applied_version, sync } = health.payload.config;
  if (sync === 'CURRENT') return applied_version ?? undefined;
  const version = published_version ?? applied_version;
  return version ? `${version} ${SYNC_LABEL[sync]}` : SYNC_LABEL[sync];
}

/** Tier 0 가 판정에 쓰는 Scene 이 EC2 의 COMMITTED Scene 과 다를 때(명세 FR-12) 두 버전. 같거나 모르면 없다. */
export function sceneMismatch(
  health: HealthSnapshot | null,
): { committed: string; live: string } | undefined {
  const scene = health?.payload.scene;
  if (!scene?.mismatch || !scene.committed_version || !scene.live_version) return undefined;
  return { committed: scene.committed_version, live: scene.live_version };
}

/** COMMITTED Scene 버전. 현장이 다른 Scene 으로 판정하고 있으면 두 버전을 함께 보인다. */
export function sceneLabel(health: HealthSnapshot | null): string | undefined {
  const mismatch = sceneMismatch(health);
  if (mismatch) return `${mismatch.committed} ≠ 현장 ${mismatch.live}`;
  return health?.payload.scene.committed_version ?? undefined;
}

/** 관리자 화면이 알리는 장애(명세 FR-11, 3.2 이중 감시). */
export type Failure = 'CONTROL_HALTED' | 'DEVICE_DOWN' | 'REMOTE_LINK_LOST';

export const FAILURE_TITLE: Record<Failure, string> = {
  CONTROL_HALTED: '관제 정지',
  DEVICE_DOWN: '현장 장비 응답 없음',
  REMOTE_LINK_LOST: '원격 전달 단절 · 현장 경보 동작 중',
};

/**
 * 장애 종류. EC2 가 하트비트로 정한 remote_state 를 따른다. CONTROL_HALTED 는 노트북 하트비트가 끊겨도 되므로
 * 노트북 연결로 "현장 장비 응답 없음"을 나눈다(계약 viewer_health). EC2 와 끊겼으면(alive 거짓) 지금 상태를 모르므로 없다.
 */
export function failureOf(health: HealthSnapshot | null, alive: boolean): Failure | undefined {
  if (!alive || !health) return undefined;
  const { remote_state, laptop } = health.payload;
  if (remote_state === 'REMOTE_LINK_LOST') return 'REMOTE_LINK_LOST';
  if (remote_state !== 'CONTROL_HALTED') return undefined;
  return laptop.link === 'DISCONNECTED' ? 'DEVICE_DOWN' : 'CONTROL_HALTED';
}

/** 데이터 연령(초). EC2 가 잰 값에 받은 뒤 흐른 시간을 더한다. 끊겨도 계속 늘어난다. */
export function dataAgeSec(health: HealthSnapshot | null, now: number): number | undefined {
  if (!health || health.payload.data_age_ms === null) return undefined;
  return (health.payload.data_age_ms + Math.max(0, now - health.receivedAt)) / 1000;
}

const CLOCK = new Intl.DateTimeFormat('ko-KR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

/** EC2 기준 시각(명세 3.8). 이 브라우저 시계에 EC2 와의 차이(clockOffsetMs)를 더해 보인다. */
export function clockLabel(clockOffsetMs: number | undefined, now: number): string | undefined {
  return clockOffsetMs === undefined ? undefined : CLOCK.format(now + clockOffsetMs);
}

/** 마지막 데이터(world_state)를 받은 시각(EC2 시계). 받은 적 없으면 없다. */
export function lastDataClock(health: HealthSnapshot | null): string | undefined {
  if (!health || health.payload.data_age_ms === null) return undefined;
  return CLOCK.format(health.payload.as_of - health.payload.data_age_ms);
}
