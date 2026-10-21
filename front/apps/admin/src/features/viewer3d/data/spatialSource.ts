// Mock·Live 공통 입력 경계. Viewer 는 메시지를 어디서 받는지 모르고 이 인터페이스만 본다.
// 실제 STOMP 연결·인증·토픽 구독은 FE(src/lib/stomp.ts 예정)나 BE 연동이 소유한다. Spatial 은 받은 본문만 넘겨받는다.

export const SOURCE_CONNECTIONS = ['CONNECTING', 'OPEN', 'CLOSED', 'ERROR'] as const;
export type SourceConnection = (typeof SOURCE_CONNECTIONS)[number];

export interface SourceConnectionChange {
  state: SourceConnection;
  /** 이 브라우저 시계 기준 시각(ms). */
  at: number;
  reason?: string;
}

/** 구역 조회 결과. 본문은 검증 전 값이다. */
export type ZonesResult = { ok: true; raw: unknown } | { ok: false; reason: string };

export interface SpatialSourceSink {
  /**
   * 검증 전 메시지 하나(STOMP 본문을 JSON.parse 한 값 등). 계약 검사는 받는 쪽이 한다.
   * receivedAt 은 이 브라우저 시계 기준 수신 시각(ms)이다.
   */
  onMessage(raw: unknown, receivedAt: number): void;
  onConnectionChange(change: SourceConnectionChange): void;
  onZones?(result: ZonesResult, receivedAt: number): void;
}

export interface SpatialSource {
  /** 같은 id 면 같은 스트림으로 보고 다시 구독해도 데이터를 비우지 않는다. 바뀌면 받은 데이터를 비운다. */
  readonly id: string;
  readonly kind: 'MOCK' | 'EXTERNAL';
  /**
   * 구독을 시작하고 해제 함수를 돌려준다. 여러 번 구독할 수 있어야 한다(React StrictMode 는 효과를 두 번 돌린다).
   * 구독하자마자 지금 연결 상태를 한 번 알린다.
   */
  subscribe(sink: SpatialSourceSink): () => void;
}

/** FE·BE 연동 쪽이 받은 메시지를 밀어 넣는 Source. 연결을 직접 맺지 않는다. */
export interface ExternalSpatialSource extends SpatialSource {
  readonly kind: 'EXTERNAL';
  /** 지금 구독 중인 수. 누수 확인용이다. */
  readonly subscriberCount: number;
  push(raw: unknown, receivedAt?: number): void;
  pushZones(result: ZonesResult, receivedAt?: number): void;
  setConnection(state: SourceConnection, reason?: string): void;
}

/**
 * 외부 입력 Source. 예) STOMP 구독 콜백에서 `source.push(JSON.parse(frame.body))`,
 * 연결·끊김 콜백에서 `source.setConnection('OPEN')`·`source.setConnection('CLOSED', '하트비트 없음')`.
 */
export function createExternalSource(
  id: string,
  now: () => number = Date.now,
): ExternalSpatialSource {
  const sinks = new Set<SpatialSourceSink>();
  let connection: SourceConnectionChange = { state: 'CONNECTING', at: now() };
  // 알리는 도중 구독이 바뀌어도 이번 알림 대상은 고정한다.
  const each = (fn: (sink: SpatialSourceSink) => void) => [...sinks].forEach(fn);

  return {
    id,
    kind: 'EXTERNAL',
    get subscriberCount() {
      return sinks.size;
    },
    subscribe(sink) {
      sinks.add(sink);
      sink.onConnectionChange(connection);
      return () => {
        sinks.delete(sink);
      };
    },
    push(raw, receivedAt = now()) {
      each((sink) => sink.onMessage(raw, receivedAt));
    },
    pushZones(result, receivedAt = now()) {
      each((sink) => sink.onZones?.(result, receivedAt));
    },
    setConnection(state, reason) {
      connection = reason ? { state, at: now(), reason } : { state, at: now() };
      each((sink) => sink.onConnectionChange(connection));
    },
  };
}
