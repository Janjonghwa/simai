import type { EventsMock } from '@/mocks/events';
import { readMockUser } from '@/mocks/mockUser';
import {
  MOCK_CONFIG,
  riskEventsBetween,
  viewerHealthAt,
  worldStateAt,
  type MockConfig,
  type MockOutage,
} from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import { http, HttpResponse } from 'msw/http';

// 개발 서버 mock 송출: world 10Hz, health 1Hz, 위험 이벤트는 40초 주기 시나리오(realtimeScenario).
const WORLD_MS = 100;
const HEALTH_MS = 1000;
const EVENTS_MS = 250;

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
  stop(): void;
}

export function startRealtimeMock(
  broker: StompBroker,
  events: Pick<EventsMock, 'record'>,
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
  const timers = [
    setInterval(() => {
      if (live() && !fault) {
        const configVersion = config().applied_version;
        broker.publish(
          '/topic/world',
          worldStateAt(now(), { sceneMismatch: staleScene, configVersion }),
        );
      }
    }, WORLD_MS),
    setInterval(() => {
      const t = now();
      if (live() && t >= healthPausedUntil) broker.publish('/topic/health', healthAt(t));
    }, HEALTH_MS),
    setInterval(() => {
      const t = now();
      // 저장소에는 늘 반영한다. 로그인하면 목록(GET /api/events)으로 지금 상태를 받는다.
      for (const message of riskEventsBetween(lastEventCheck, t)) {
        const stored = events.record(message);
        if (live()) broker.publish('/topic/events', stored);
      }
      lastEventCheck = t;
    }, EVENTS_MS),
  ];
  return {
    handlers: [
      http.get('*/api/health', ({ request }) => {
        // 로그인한 계정이면 역할과 상관없이 본다. 만료된 실서버 토큰(혼합 모드)은 서버처럼 거절한다.
        const user = readMockUser(request.headers.get('Authorization'));
        if (!user || (user.expiresAt !== undefined && user.expiresAt <= now())) {
          return HttpResponse.json(
            { status: 401, detail: '로그인이 필요합니다', code: 'UNAUTHORIZED' },
            { status: 401 },
          );
        }
        return HttpResponse.json(healthAt(now()));
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
    stop: () => timers.forEach(clearInterval),
  };
}
