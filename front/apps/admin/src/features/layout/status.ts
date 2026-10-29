import type { HealthSnapshot, RealtimeState } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';

// 연결 상태·장애 띠·시계에 보이는 값을 viewer_health(명세 12.4 /topic/health)에서 만든다. 화면 컴포넌트 없이 시험한다.

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

/** 관리자 화면이 알리는 장애(명세 FR-11, 3.2 이중 감시). */
export type Failure = 'CONTROL_HALTED' | 'DEVICE_DOWN' | 'REMOTE_LINK_LOST';

/** 장애 띠에 보이는 장애. LINK_LOST 는 이 화면과 EC2 의 연결이 끊긴 것이다(상단 바에 상태 칩이 없어 띠로 알린다). */
export type BandFailure = Failure | 'LINK_LOST';

export const FAILURE_TITLE: Record<BandFailure, string> = {
  LINK_LOST: '현장 연결 끊김',
  CONTROL_HALTED: '관제 정지',
  DEVICE_DOWN: '현장 장비 응답 없음',
  REMOTE_LINK_LOST: '원격 전달 단절 · 현장 경보 동작 중',
};

/** 제목 뒤에 붙는 설명(관제 데모와 같은 문구). */
export const FAILURE_DETAIL: Record<BandFailure, string | undefined> = {
  LINK_LOST: '현장 노트북 경보는 계속될 수 있음',
  CONTROL_HALTED: '판정 서버(Tier 0) 응답 없음',
  DEVICE_DOWN: '현장 노트북(영상 중계·현장 경보) 하트비트 끊김',
  REMOTE_LINK_LOST: undefined,
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

/**
 * 장애 띠에 보일 장애. EC2 와 이어진 적이 있는데 지금 끊겼으면(상태 칩의 LOST) 그것을 먼저 보인다.
 * 이어져 있으면 EC2 가 알린 장애(failureOf)다. 아직 연결 전이면 없다.
 */
export function bandFailure(
  state: Pick<RealtimeState, 'phase' | 'openedAt' | 'health'>,
  alive: boolean,
): BandFailure | undefined {
  if (chipState(state, alive) === 'LOST') return 'LINK_LOST';
  return failureOf(state.health, alive);
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
