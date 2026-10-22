import type { ExternalSpatialSource } from '@/features/viewer3d';
import type { ViewerHealthPayload, WorldStatePayload } from '@simai/contracts';
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

export interface WorldSnapshot {
  payload: WorldStatePayload;
  /** 이 브라우저 시계로 받은 시각(ms). */
  receivedAt: number;
}

export interface RealtimeState {
  phase: LinkPhase;
  /** 마지막으로 연결이 열린 시각. 연결 뒤 health 를 아직 못 받았을 때 끊김 판단의 기준이다. */
  openedAt: number | null;
  health: HealthSnapshot | null;
  /** 가장 새 World 상태(t_latest_global 기준). 3D 뷰어 없이 그리는 2D 비상 화면이 쓴다(명세 13.6). */
  world: WorldSnapshot | null;
  /**
   * 연결이 (다시) 열린 뒤 첫 world 는 순서와 상관없이 받는다. 서버 재시작·시계 보정으로 t_latest_global 이
   * 뒤로 가면 이후 프레임을 모두 버려 평면이 멈추기 때문이다.
   */
  worldResync: boolean;
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
  /** 더 새 프레임만 받는다. 늦게 온 이전 프레임은 버린다. */
  receiveWorld: (world: WorldSnapshot) => void;
  reject: () => void;
  reset: () => void;
}

const INITIAL = {
  phase: 'IDLE',
  openedAt: null,
  health: null,
  world: null,
  worldResync: false,
  rejected: 0,
  source: null,
} as const satisfies Partial<RealtimeState>;

// 10Hz 로 오는 world 는 최신 프레임 하나만 둔다. 3D 뷰어는 같은 메시지를 Source 로 받아 자기 저장소에 둔다(viewer3d).
// world 를 쓰는 화면(2D 평면)은 필요한 층만 구독한다. 다른 구독자는 원시값 셀렉터라 다시 그리지 않는다.
export const useRealtimeStore = create<RealtimeState>()((set) => ({
  ...INITIAL,
  begin: () => set({ ...INITIAL, phase: 'CONNECTING' }),
  setSource: (source) => set({ source }),
  setPhase: (phase) => set({ phase }),
  opened: (at) => set({ phase: 'OPEN', openedAt: at, worldResync: true }),
  receiveHealth: (health) => set({ health }),
  receiveWorld: (world) =>
    set((s) =>
      s.world &&
      !s.worldResync &&
      s.world.payload.t_latest_global >= world.payload.t_latest_global &&
      // 연결이 그대로여도 시계가 뒤로 가면 이후 프레임을 모두 버리게 된다. 한동안 받지 못했으면 받는다.
      world.receivedAt - s.world.receivedAt <= WORLD_STALE_MS
        ? s
        : { world, worldResync: false },
    ),
  reject: () => set((s) => ({ rejected: s.rejected + 1 })),
  reset: () => set(INITIAL),
}));

export type RealtimeStore = typeof useRealtimeStore;

/** /topic/health 는 1초마다 온다. 3번 연속 빠지면 끊긴 것으로 본다(명세 3장 하트비트, 13.2). */
export const HEALTH_TIMEOUT_MS = 3000;

/** world 는 10Hz 로 온다. 이 시간(ms) 넘게 없으면 health 가 와도 위치가 멈춘 것으로 본다. */
export const WORLD_STALE_MS = 1500;

/** 최근 world 를 받았는지. 연결이 살아 있어도 world 만 멈출 수 있다(파이프라인 정지). */
export function isWorldFresh(
  state: Pick<RealtimeState, 'world'>,
  now: number,
  staleMs = WORLD_STALE_MS,
): boolean {
  return state.world !== null && now - state.world.receivedAt <= staleMs;
}

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
