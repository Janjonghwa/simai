import type { ExternalSpatialSource, SourceConnection } from '@/features/viewer3d';
import {
  HEALTH_TIMEOUT_MS,
  isLinkAlive,
  type RealtimeState,
  type RealtimeStore,
} from '@/store/realtime.store';
import { validateMessage, type RiskEventPayload } from '@simai/contracts';
import { Client, ReconnectionTimeMode, type IMessage } from '@stomp/stompjs';

/** 관제 앱이 구독하는 토픽(명세 12.4). 조치 불일치(/topic/admin/events)는 그 화면을 만들 때 더한다. */
export const TOPICS = ['/topic/world', '/topic/health', '/topic/events'] as const;
const HEALTH_TOPIC = '/topic/health';
const EVENTS_TOPIC = '/topic/events';

/** 연결에서 화면 쪽으로 알리는 일. 화면 데이터(Query 캐시 등)는 받는 쪽이 정한다. */
export interface RealtimeListeners {
  /** /topic/events 로 받아 계약 검사를 통과한 risk_event. */
  riskEvent: (event: RiskEventPayload, receivedAt: number) => void;
  /** 연결이 열릴 때마다(처음·재연결). 끊긴 사이 놓친 상태를 다시 받는 데 쓴다. */
  open: () => void;
}

const HEARTBEAT_MS = 10_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

export interface RealtimeSession {
  token: string;
  userId: string;
}

export interface Realtime {
  /** 세션 토큰으로 연결한다. 이전 연결이 있으면 끊고 데이터를 비운 뒤 새로 연결한다. */
  start(session: RealtimeSession): void;
  /** 연결을 끊고 받은 데이터를 비운다. 끊김이 끝나면 풀린다. */
  stop(): Promise<void>;
  /** 알림을 받는다. 해제 함수를 돌려준다. */
  on<K extends keyof RealtimeListeners>(type: K, listener: RealtimeListeners[K]): () => void;
}

export type SourceFactory = (id: string, now: () => number) => ExternalSpatialSource;

// viewer3d 입구는 Viewer3D(three.js)도 내보낸다. 정적으로 import 하면 로그인 화면 번들에 3D 코드가 들어가므로
// 연결을 시작할 때 불러온다(관제 화면이 어차피 같은 코드를 받는다).
const loadViewerSource = () =>
  import('@/features/viewer3d').then((viewer): SourceFactory => viewer.createExternalSource);

export interface RealtimeOptions {
  url: string;
  store: RealtimeStore;
  /** 뷰어 입력(Source)을 만드는 함수를 불러온다. 시험에서 바꿔 넣을 수 있다. */
  loadSourceFactory?: () => Promise<SourceFactory>;
  now?: () => number;
  healthTimeoutMs?: number;
  /** 끊긴 뒤 첫 재연결까지 기다리는 시간(ms). 실패할 때마다 두 배로 늘려 최대 30초까지 기다린다. */
  reconnectDelayMs?: number;
  /** health 가 끊겼는지 다시 보는 간격(ms). */
  watchIntervalMs?: number;
}

interface ViewerLink {
  state: SourceConnection;
  reason?: string;
}

/** 뷰어에 알릴 연결 상태. STOMP 가 열려 있어도 health 가 끊기면 끊긴 것으로 알린다. */
function viewerLink(state: RealtimeState, now: number, timeoutMs: number): ViewerLink {
  if (state.phase === 'OPEN') {
    return isLinkAlive(state, now, timeoutMs)
      ? { state: 'OPEN' }
      : { state: 'CLOSED', reason: '상태 메시지 끊김' };
  }
  if (state.phase === 'CLOSED') return { state: 'CLOSED', reason: '연결 끊김' };
  return { state: 'CONNECTING' };
}

// 화면은 연결을 직접 다루지 않는다. 로그인·로그아웃에 맞춰 bindSession 이 start·stop 을 부른다.
export function createRealtime({
  url,
  store,
  loadSourceFactory = loadViewerSource,
  now = Date.now,
  healthTimeoutMs = HEALTH_TIMEOUT_MS,
  reconnectDelayMs = 1000,
  watchIntervalMs = 500,
}: RealtimeOptions): Realtime {
  let current: { client: Client; source: ExternalSpatialSource; dispose: () => void } | null = null;
  // start·stop 마다 올린다. Source 를 불러오는 사이 stop·다른 start 가 오면 그 연결은 열지 않는다.
  let generation = 0;
  const listeners: { [K in keyof RealtimeListeners]: Set<RealtimeListeners[K]> } = {
    riskEvent: new Set(),
    open: new Set(),
  };

  function on<K extends keyof RealtimeListeners>(type: K, listener: RealtimeListeners[K]) {
    listeners[type].add(listener);
    return () => {
      listeners[type].delete(listener);
    };
  }

  // 받는 쪽 하나가 실패해도 나머지는 받고 연결 처리도 이어 간다. 오류는 따로 던져 콘솔에 남긴다.
  function emit<K extends keyof RealtimeListeners>(
    type: K,
    ...args: Parameters<RealtimeListeners[K]>
  ) {
    for (const listener of listeners[type]) {
      try {
        (listener as (...a: Parameters<RealtimeListeners[K]>) => void)(...args);
      } catch (error) {
        queueMicrotask(() => {
          throw error;
        });
      }
    }
  }

  function stop() {
    generation += 1;
    if (!current) {
      if (store.getState().phase !== 'IDLE') store.getState().reset();
      return Promise.resolve();
    }
    const { client, source, dispose } = current;
    current = null;
    dispose();
    source.setConnection('CLOSED', '연결 종료');
    store.getState().reset();
    // 이미 끊긴 소켓을 정리하다 실패해도 새 연결에는 영향이 없다.
    return client.deactivate().catch(() => undefined);
  }

  function start(session: RealtimeSession) {
    void stop();
    const ticket = generation;
    store.getState().begin();
    // 뷰어 코드를 받지 못하면(일시적 네트워크 오류) 재연결과 같은 간격으로 다시 받는다.
    const load = (attempt: number) => {
      loadSourceFactory().then(
        (createSource) => {
          if (ticket !== generation) return;
          open(session, createSource(`live:${session.userId}:${ticket}`, now));
        },
        () => {
          if (ticket !== generation) return;
          store.getState().setPhase('CLOSED');
          const delay = Math.min(reconnectDelayMs * 2 ** attempt, MAX_RECONNECT_DELAY_MS);
          setTimeout(() => {
            if (ticket === generation) load(attempt + 1);
          }, delay);
        },
      );
    };
    load(0);
  }

  function open({ token }: RealtimeSession, source: ExternalSpatialSource) {
    store.getState().setSource(source);
    // 끊은 뒤 늦게 도착한 콜백은 버린다.
    const isCurrent = () => current?.source === source;

    const receive = (topic: string) => (message: IMessage) => {
      if (!isCurrent()) return;
      const receivedAt = now();
      let raw: unknown;
      try {
        raw = JSON.parse(message.body);
      } catch {
        store.getState().reject();
        return;
      }
      // 3D 뷰어는 받은 본문을 직접 계약으로 검사하고 순서를 정리한다(viewer3d 의 Source 입력).
      source.push(raw, receivedAt);
      if (topic === EVENTS_TOPIC) {
        // 이벤트 목록·경보는 앱 화면이 따로 쓰므로 앱도 검사한다.
        const event = validateMessage('risk_event', raw);
        if (!event.ok) {
          store.getState().reject();
          return;
        }
        emit('riskEvent', event.message.payload, receivedAt);
        return;
      }
      if (topic !== HEALTH_TOPIC) return;
      const checked = validateMessage('viewer_health', raw);
      if (!checked.ok) {
        store.getState().reject();
        return;
      }
      store.getState().receiveHealth({
        payload: checked.message.payload,
        receivedAt,
        clockOffsetMs: checked.message.sent_at - receivedAt,
      });
    };

    const client = new Client({
      brokerURL: url,
      connectHeaders: { Authorization: `Bearer ${token}` },
      heartbeatIncoming: HEARTBEAT_MS,
      heartbeatOutgoing: HEARTBEAT_MS,
      reconnectDelay: reconnectDelayMs,
      reconnectTimeMode: ReconnectionTimeMode.EXPONENTIAL,
      maxReconnectDelay: MAX_RECONNECT_DELAY_MS,
      beforeConnect: () => {
        if (isCurrent()) store.getState().setPhase('CONNECTING');
      },
      onConnect: () => {
        if (!isCurrent()) return;
        store.getState().opened(now());
        for (const topic of TOPICS) client.subscribe(topic, receive(topic));
        emit('open');
      },
      onWebSocketClose: () => {
        if (isCurrent()) store.getState().setPhase('CLOSED');
      },
    });

    // 단계가 바뀔 때와 시간이 지나 health 가 끊겼을 때 모두 뷰어에 알린다. 같은 상태는 다시 알리지 않는다.
    let last: ViewerLink | null = null;
    const sync = () => {
      const next = viewerLink(store.getState(), now(), healthTimeoutMs);
      if (last?.state === next.state && last.reason === next.reason) return;
      last = next;
      source.setConnection(next.state, next.reason);
    };
    const unsubscribe = store.subscribe(sync);
    const timer = setInterval(sync, watchIntervalMs);
    current = {
      client,
      source,
      dispose: () => {
        unsubscribe();
        clearInterval(timer);
      },
    };
    sync();
    client.activate();
  }

  return { start, stop, on };
}
