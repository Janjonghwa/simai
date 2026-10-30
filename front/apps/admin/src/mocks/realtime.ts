import { ADMIN_EVENTS_TOPIC, EVENTS_TOPIC, HEALTH_TOPIC, WORLD_TOPIC } from '@/lib/stomp';
import type { EventsMock } from '@/mocks/events';
import { readLiveUser, unauthorized } from '@/mocks/problem';
import {
  actionMismatchesBetween,
  actionMismatchMessage,
  EVENT_CYCLE_MS,
  MOCK_CONFIG,
  mockEventId,
  riskEventsBetween,
  viewerHealthAt,
  worldStateAt,
  type MismatchEventState,
  type MockConfig,
  type MockOutage,
} from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import { http, HttpResponse } from 'msw/http';

// 개발 서버 mock 송출: world 10Hz, health 1Hz, 위험 이벤트는 40초 주기 시나리오(realtimeScenario).
const WORLD_MS = 100;
const HEALTH_MS = 1000;
const EVENTS_MS = 250;

/** actionMismatch 에 이벤트 id 만 준 채 저장소에 그 이벤트가 없을 때 쓰는 상태: 순번 1 의 활성. */
const UNSTORED_EVENT_STATE: MismatchEventState = {
  event_sequence: 1,
  lifecycle: 'UPDATED',
  zone_id: null,
};

export interface RealtimeMock {
  /**
   * GET /api/health(명세 12.6, 열람). /topic/health 와 같은 지금 상태(장애·Scene 불일치 흉내 포함)를 준다.
   * 응답 형식은 FE 제안(viewer_health 메시지 그대로, features/layout/api.ts)을 따른다.
   */
  handlers: ReturnType<typeof http.get>[];
  /** health 송출을 ms 동안 멈춘다(EC2 상태 메시지 끊김 흉내). REST 조회는 그대로 답한다. */
  pauseHealth(ms: number): void;
  /** 연결을 끊는다(서버 재시작 흉내). 클라이언트가 다시 연결한다. */
  drop(): void;
  /**
   * 장애를 흉내 낸다(명세 FR-11). 그때부터 world 가 오지 않고, 3초 뒤 EC2 가 health 로 알린다. null 이면 복구한다.
   * 위험 이벤트는 그대로 흘린다(흉내를 단순하게 둔다).
   */
  outage(kind: MockOutage | null): void;
  /** Tier 0 가 이전 Scene 으로 판정하는 상태(명세 FR-12)를 켜고 끈다. */
  sceneMismatch(on: boolean): void;
  /**
   * 조치 불일치(action_mismatch)를 /topic/admin/events 로 바로 보낸다(관리자 연결만 받는다). eventId 를 안 주면 이번 바퀴의
   * zone 이벤트(w-0015 R1)다. cleared 를 true 로 주면 불일치가 풀린 것이다. 저장소에 없는 이벤트는 순번 1 의 활성으로 본다.
   */
  actionMismatch(eventId?: string, cleared?: boolean): void;
  /**
   * 토큰 만료를 흉내 낸다: 다음 STOMP 연결(CONNECT)을 ERROR 로 거절하고 지금 연결을 끊는다. 클라이언트가 토큰을 갱신해
   * 다시 붙는지 본다(실서버는 만료된 토큰의 CONNECT 를 ERROR 프레임으로 거절한다).
   */
  expireStomp(): void;
  stop(): void;
}

export function startRealtimeMock(
  broker: StompBroker,
  // find 는 조치 불일치가 저장소의 지금 순번·단계를 따르게 한다(없으면 시나리오의 순번).
  events: Pick<EventsMock, 'record'> & {
    find?: (eventId: string) => MismatchEventState | undefined;
  },
  now: () => number = Date.now,
  // 구성 묶음 상태(발행·ACK). 구역 mock 이 정한다(mocks/zones).
  config: () => MockConfig = () => MOCK_CONFIG,
): RealtimeMock {
  let healthPausedUntil = 0;
  let lastEventCheck = now();
  let fault: { kind: MockOutage; since: number } | undefined;
  let staleScene = false;
  // 연결이 없으면(로그인 전·로그아웃 뒤) 메시지를 만들지 않는다.
  const live = () => broker.connections > 0;
  const healthAt = (t: number) =>
    viewerHealthAt(t, { outage: fault, sceneMismatch: staleScene, config: config() });

  function publishWorld() {
    if (!live() || fault) return;
    const configVersion = config().applied_version;
    broker.publish(WORLD_TOPIC, worldStateAt(now(), { sceneMismatch: staleScene, configVersion }));
  }

  function publishHealth() {
    const t = now();
    if (live() && t >= healthPausedUntil) broker.publish(HEALTH_TOPIC, healthAt(t));
  }

  function publishEvents() {
    const t = now();
    const connected = live();
    // 저장소에는 늘 반영한다. 로그인하면 목록(GET /api/events)으로 지금 상태를 받는다.
    for (const message of riskEventsBetween(lastEventCheck, t)) {
      const stored = events.record(message);
      if (connected) broker.publish(EVENTS_TOPIC, stored);
    }
    // 같은 구간의 조치 불일치는 이벤트 전이를 저장한 뒤 보낸다(해소와 같은 시각이면 해소된 순번으로 푼다).
    for (const message of actionMismatchesBetween(lastEventCheck, t, (id) => events.find?.(id))) {
      if (connected) broker.publish(ADMIN_EVENTS_TOPIC, message);
    }
    lastEventCheck = t;
  }

  const timers = [
    setInterval(publishWorld, WORLD_MS),
    setInterval(publishHealth, HEALTH_MS),
    setInterval(publishEvents, EVENTS_MS),
  ];

  return {
    handlers: [
      http.get('*/api/health', ({ request }) => {
        // 로그인한 계정이면 역할과 상관없이 본다. 만료된 실서버 토큰(혼합 모드)은 서버처럼 거절한다.
        const t = now();
        return readLiveUser(request, t) ? HttpResponse.json(healthAt(t)) : unauthorized();
      }),
    ],
    pauseHealth: (ms) => {
      healthPausedUntil = now() + ms;
    },
    drop: () => broker.dropAll(),
    outage: (kind) => {
      fault = kind ? { kind, since: now() } : undefined;
    },
    sceneMismatch: (on) => {
      staleScene = on;
    },
    actionMismatch: (eventId, cleared = false) => {
      const t = now();
      const id = eventId ?? mockEventId(Math.floor(t / EVENT_CYCLE_MS), 'zone');
      const state = events.find?.(id) ?? UNSTORED_EVENT_STATE;
      broker.publish(ADMIN_EVENTS_TOPIC, actionMismatchMessage(id, state, t, { cleared }));
    },
    expireStomp: () => {
      broker.rejectNextConnects();
      broker.dropAll();
    },
    stop: () => timers.forEach(clearInterval),
  };
}
