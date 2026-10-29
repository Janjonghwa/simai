import type {
  ExternalSpatialSource,
  SourceConnection,
  SpatialSourceSink,
} from '@/features/viewer3d';
import {
  HEALTH_TIMEOUT_MS,
  isLinkAlive,
  type RealtimeState,
  type RealtimeStore,
} from '@/store/realtime.store';
import { validateMessage, type RiskEventPayload } from '@simai/contracts';
import { Client, ReconnectionTimeMode, type IMessage } from '@stomp/stompjs';

/** 관제 앱이 구독하는 토픽(명세 12.4). 조치 불일치(/topic/admin/events)는 그 화면을 만들 때 더한다. */
export const TOPICS = ['/topic/world', '/topic/health', '/topic/events', '/topic/avatar'] as const;
const WORLD_TOPIC = '/topic/world';
const HEALTH_TOPIC = '/topic/health';
const EVENTS_TOPIC = '/topic/events';
const AVATAR_TOPIC = '/topic/avatar';

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
  userId: string;
  /**
   * 연결·재연결할 때마다 읽는다. 갱신으로 토큰이 바뀌어도 연결을 다시 열지 않고 다음 재연결에 새 토큰을 싣는다.
   * Promise 면 기다린다(만료가 가까우면 먼저 갱신한다, authSession.freshToken).
   */
  getToken: () => string | undefined | Promise<string | undefined>;
}

export interface Realtime {
  /** 사용자의 토큰으로 연결한다. 이전 연결이 있으면 끊고 데이터를 비운 뒤 새로 연결한다. */
  start(session: RealtimeSession): void;
  /** 연결을 끊고 받은 데이터를 비운다. 끊김이 끝나면 풀린다. */
  stop(): Promise<void>;
  /** 알림을 받는다. 해제 함수를 돌려준다. */
  on<K extends keyof RealtimeListeners>(type: K, listener: RealtimeListeners[K]): () => void;
}

export type SourceFactory = (id: string, now: () => number) => ExternalSpatialSource;

// viewer3d 입구는 Viewer3D(three.js)도 내보낸다. 정적으로 import 하면 로그인 화면 번들에 3D 코드가 들어가므로
// 연결과 따로 불러온다. 3D 코드를 받지 못해도 연결·경보·world 는 계속 받는다(2D 비상 화면, 명세 13.6).
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

interface Received {
  raw: unknown;
  receivedAt: number;
}

interface ReceivedEvent extends Received {
  sequence: number | undefined;
  resolved: boolean;
}

/** 다시 넘길 이벤트 수. 넘치면 해소된 이벤트부터, 없으면 가장 오래 소식이 없던 이벤트부터 뺀다. */
const MAX_REPLAY_EVENTS = 200;

interface Connection {
  client: Client;
  /** 3D 뷰어 입력. 뷰어 코드를 받기 전에는 없다. */
  source: ExternalSpatialSource | null;
  /**
   * 계약을 통과한 가장 새 world·health 와 이벤트별 가장 새 risk_event. Source 는 구독 전 메시지를 남기지 않으므로,
   * 3D 뷰어가 구독하는 순간(코드를 늦게 받았거나 3D 를 다시 띄움) 그 뷰어에 넘겨 진행 중 경보를 놓치지 않게 한다.
   */
  replay: { world: Received | null; health: Received | null; events: Map<string, ReceivedEvent> };
  /** 뷰어에 연결 상태를 알리는 구독·타이머를 푼다. */
  dispose: () => void;
}

// 다른 쪽(화면·3D 뷰어)의 오류는 따로 던져 콘솔에 남기고 연결 처리는 이어 간다. 3D 가 고장 나도 2D·경보는 계속 받는다.
const safely = (fn: () => void) => {
  try {
    fn();
  } catch (error) {
    queueMicrotask(() => {
      throw error;
    });
  }
};

function rememberEvent(
  events: Map<string, ReceivedEvent>,
  event: RiskEventPayload,
  received: Received,
) {
  const prev = events.get(event.event_id);
  // 늦게 온 이전 상태로 덮지 않는다. 같은 순번이면 확인 기록이 늘었을 수 있어 새것으로 본다.
  if (prev?.sequence !== undefined && (event.event_sequence ?? 0) < prev.sequence) return;
  events.delete(event.event_id);
  events.set(event.event_id, {
    ...received,
    sequence: event.event_sequence,
    resolved: event.lifecycle === 'RESOLVED',
  });
  if (events.size <= MAX_REPLAY_EVENTS) return;
  // 진행 중 경보는 소식이 오래 없어도 남긴다.
  const resolved = [...events].find(([, e]) => e.resolved)?.[0];
  events.delete(resolved ?? events.keys().next().value!);
}

/** 구독하는 순간 그 뷰어에 가장 새 world·health·이벤트를 넘기는 Source. 3D 뷰어 코드는 그대로 두고 감싼다. */
function withReplay(connection: Connection, source: ExternalSpatialSource): ExternalSpatialSource {
  const subscribe = (sink: SpatialSourceSink) => {
    const unsubscribe = source.subscribe(sink);
    const { world, health, events } = connection.replay;
    for (const item of [world, health, ...events.values()]) {
      if (item) safely(() => sink.onMessage(item.raw, item.receivedAt));
    }
    return unsubscribe;
  };
  return Object.create(source, { subscribe: { value: subscribe } }) as ExternalSpatialSource;
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
  let current: Connection | null = null;
  // start·stop 마다 올린다. Source 이름에 붙여 세션마다 다른 스트림으로 보이게 한다.
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

  // 받는 쪽 하나가 실패해도 나머지는 받고 연결 처리도 이어 간다.
  function emit<K extends keyof RealtimeListeners>(
    type: K,
    ...args: Parameters<RealtimeListeners[K]>
  ) {
    for (const listener of listeners[type]) {
      safely(() => (listener as (...a: Parameters<RealtimeListeners[K]>) => void)(...args));
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
    if (source) safely(() => source.setConnection('CLOSED', '연결 종료'));
    store.getState().reset();
    // 이미 끊긴 소켓을 정리하다 실패해도 새 연결에는 영향이 없다.
    return client.deactivate().catch(() => undefined);
  }

  function start(session: RealtimeSession) {
    void stop();
    const ticket = generation;
    store.getState().begin();
    const connection = connect(session);
    const load = (attempt: number) => {
      loadSourceFactory()
        .then((createSource) => {
          if (current === connection) {
            attachSource(connection, createSource(`live:${session.userId}:${ticket}`, now));
          }
        })
        .catch((error: unknown) => {
          // 뷰어 코드를 받지 못했거나(일시적 네트워크 오류) Source 를 만들지 못했다. 재연결과 같은 간격으로 다시 받는다.
          // 그동안에도 연결은 쓴다(2D 평면·경보).
          if (current !== connection || connection.source) return;
          console.warn('3D 뷰어 코드를 불러오지 못해 다시 시도한다', error);
          const delay = Math.min(reconnectDelayMs * 2 ** attempt, MAX_RECONNECT_DELAY_MS);
          setTimeout(() => {
            if (current === connection) load(attempt + 1);
          }, delay);
        });
    };
    load(0);
  }

  /**
   * 뷰어 입력을 붙인다. 붙인 뒤로 받은 본문을 넘기고, 연결 단계가 바뀌거나 health 가 끊기면 알린다.
   * 화면에는 구독하는 순간 가장 새 메시지를 받는 Source 를 준다(withReplay).
   */
  function attachSource(connection: Connection, source: ExternalSpatialSource) {
    // 같은 상태는 다시 알리지 않는다.
    let last: ViewerLink | null = null;
    const sync = () => {
      const next = viewerLink(store.getState(), now(), healthTimeoutMs);
      if (last?.state === next.state && last.reason === next.reason) return;
      last = next;
      safely(() => source.setConnection(next.state, next.reason));
    };
    const unsubscribe = store.subscribe(sync);
    const timer = setInterval(sync, watchIntervalMs);
    connection.dispose = () => {
      unsubscribe();
      clearInterval(timer);
    };
    sync();
    // 준비가 끝난 뒤에 붙인다. 앞에서 실패하면 Source 가 없는 것으로 보고 다시 시도한다.
    connection.source = source;
    store.getState().setSource(withReplay(connection, source));
  }

  function connect({ getToken }: RealtimeSession): Connection {
    // 끊은 뒤 늦게 도착한 콜백은 버린다.
    const isCurrent = () => current === connection;

    /** 앱이 쓰는 메시지를 검사해 저장소·이벤트 캐시에 넘긴다. 계약에 맞지 않으면 개수만 센다. */
    const handle = (topic: string, received: Received) => {
      const { raw, receivedAt } = received;
      const { replay } = connection;
      if (topic === AVATAR_TOPIC) {
        const avatar = validateMessage('avatar_frame', raw);
        if (!avatar.ok) return store.getState().reject();
        // 포즈는 실시간 표시용이다. 이전 포즈를 재생하지 않고 다음 갱신을 기다린다(0.5 s 캡슐 폴백).
        return;
      }
      if (topic === WORLD_TOPIC) {
        // 2D 비상 화면은 3D 뷰어 없이 그리므로 앱도 검사해 최신 프레임을 둔다.
        const world = validateMessage('world_state', raw);
        if (!world.ok) return store.getState().reject();
        store.getState().receiveWorld({ payload: world.message.payload, receivedAt });
        // 저장소가 받은(더 새) 프레임만 다시 넘길 것으로 둔다. 늦게 온 이전 프레임은 두지 않는다.
        if (store.getState().world?.payload === world.message.payload) replay.world = received;
        return;
      }
      if (topic === EVENTS_TOPIC) {
        // 이벤트 목록·경보는 앱 화면이 따로 쓰므로 앱도 검사한다.
        const event = validateMessage('risk_event', raw);
        if (!event.ok) return store.getState().reject();
        rememberEvent(replay.events, event.message.payload, received);
        emit('riskEvent', event.message.payload, receivedAt);
        return;
      }
      if (topic !== HEALTH_TOPIC) return;
      const checked = validateMessage('viewer_health', raw);
      if (!checked.ok) return store.getState().reject();
      store.getState().receiveHealth({
        payload: checked.message.payload,
        receivedAt,
        clockOffsetMs: checked.message.sent_at - receivedAt,
      });
      replay.health = received;
    };

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
      try {
        handle(topic, { raw, receivedAt });
      } finally {
        // 3D 뷰어는 받은 본문을 직접 계약으로 검사하고 순서를 정리한다(viewer3d 의 Source 입력).
        // 앱 처리 뒤에 넘기고 실패를 가둔다. 3D 가 고장 나도 2D 평면·경보는 계속 받는다.
        const { source } = connection;
        if (source) safely(() => source.push(raw, receivedAt));
      }
    };

    const client = new Client({
      brokerURL: url,
      heartbeatIncoming: HEARTBEAT_MS,
      heartbeatOutgoing: HEARTBEAT_MS,
      reconnectDelay: reconnectDelayMs,
      reconnectTimeMode: ReconnectionTimeMode.EXPONENTIAL,
      maxReconnectDelay: MAX_RECONNECT_DELAY_MS,
      // stompjs 는 이 함수가 끝난 뒤 connectHeaders 를 읽는다. 재연결마다 가장 새 토큰을 싣는다.
      beforeConnect: async (self) => {
        if (isCurrent()) store.getState().setPhase('CONNECTING');
        // 실패해도 헤더 없이 붙어 거절당하고 다음 재연결에서 다시 받는다.
        const token = await Promise.resolve(getToken()).catch(() => undefined);
        self.connectHeaders = token ? { Authorization: `Bearer ${token}` } : {};
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

    const connection: Connection = {
      client,
      source: null,
      replay: { world: null, health: null, events: new Map() },
      dispose: () => undefined,
    };
    current = connection;
    client.activate();
    return connection;
  }

  return { start, stop, on };
}
