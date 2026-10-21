import type { ExternalSpatialSource } from '@/features/viewer3d';
import type { ViewerHealthPayload } from '@simai/contracts';
import { create } from 'zustand';

/** STOMP 연결 단계. IDLE 은 로그인 전·로그아웃 뒤다. */
export type LinkPhase = 'IDLE' | 'CONNECTING' | 'OPEN' | 'CLOSED';

export interface HealthSnapshot {
  payload: ViewerHealthPayload;
  /** 이 브라우저 시계로 받은 시각(ms). */
  receivedAt: number;
  /** EC2 시계 − 이 브라우저 시계(ms). envelope 의 sent_at 으로 잰다(전송 지연만큼 작게 나온다). */
  clockOffsetMs: number;
}

export interface RealtimeState {
  phase: LinkPhase;
  /** 마지막으로 연결이 열린 시각. 연결 뒤 health 를 아직 못 받았을 때 끊김 판단의 기준이다. */
  openedAt: number | null;
  health: HealthSnapshot | null;
  /** JSON 이 아니거나 계약 검사에 실패해 버린 메시지 수. */
  rejected: number;
  /** 3D 뷰어 입력. 세션마다 새로 만들어 이전 사용자의 데이터가 뷰어에 남지 않게 한다. */
  source: ExternalSpatialSource | null;
  /** 새 세션의 연결을 시작한다. 이전 값은 모두 비운다. */
  begin: () => void;
  setSource: (source: ExternalSpatialSource) => void;
  setPhase: (phase: LinkPhase) => void;
  opened: (at: number) => void;
  receiveHealth: (health: HealthSnapshot) => void;
  reject: () => void;
  reset: () => void;
}

const INITIAL = {
  phase: 'IDLE',
  openedAt: null,
  health: null,
  rejected: 0,
  source: null,
} as const satisfies Partial<RealtimeState>;

// 10Hz 로 오는 world 는 여기 두지 않는다. 뷰어가 Source 로 받아 자기 저장소에 둔다(viewer3d).
export const useRealtimeStore = create<RealtimeState>()((set) => ({
  ...INITIAL,
  begin: () => set({ ...INITIAL, phase: 'CONNECTING' }),
  setSource: (source) => set({ source }),
  setPhase: (phase) => set({ phase }),
  opened: (at) => set({ phase: 'OPEN', openedAt: at }),
  receiveHealth: (health) => set({ health }),
  reject: () => set((s) => ({ rejected: s.rejected + 1 })),
  reset: () => set(INITIAL),
}));

export type RealtimeStore = typeof useRealtimeStore;

/** /topic/health 는 1초마다 온다. 3번 연속 빠지면 끊긴 것으로 본다(명세 3장 하트비트, 13.2). */
export const HEALTH_TIMEOUT_MS = 3000;

/** EC2 와 이어져 있는지. STOMP 가 열려 있어도 health 가 정한 시간 넘게 없으면 끊긴 것이다. */
export function isLinkAlive(
  state: Pick<RealtimeState, 'phase' | 'openedAt' | 'health'>,
  now: number,
  timeoutMs = HEALTH_TIMEOUT_MS,
): boolean {
  if (state.phase !== 'OPEN') return false;
  const lastAlive = Math.max(state.openedAt ?? 0, state.health?.receivedAt ?? 0);
  return now - lastAlive <= timeoutMs;
}
