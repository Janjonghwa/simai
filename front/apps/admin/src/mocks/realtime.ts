import type { EventsMock } from '@/mocks/events';
import { riskEventsBetween, viewerHealthAt, worldStateAt } from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';

// 개발 서버 mock 송출: world 10Hz, health 1Hz, 위험 이벤트는 40초 주기 시나리오(realtimeScenario).
const WORLD_MS = 100;
const HEALTH_MS = 1000;
const EVENTS_MS = 250;

export interface RealtimeMock {
  /** health 송출을 ms 동안 멈춘다(EC2 상태 메시지 끊김 흉내). */
  pauseHealth(ms: number): void;
  /** 연결을 끊는다(서버 재시작 흉내). 클라이언트가 다시 연결한다. */
  drop(): void;
  stop(): void;
}

export function startRealtimeMock(
  broker: StompBroker,
  events: Pick<EventsMock, 'record'>,
  now: () => number = Date.now,
): RealtimeMock {
  let healthPausedUntil = 0;
  let lastEventCheck = now();
  // 연결이 없으면(로그인 전·로그아웃 뒤) 메시지를 만들지 않는다.
  const live = () => broker.connections > 0;
  const timers = [
    setInterval(() => {
      if (live()) broker.publish('/topic/world', worldStateAt(now()));
    }, WORLD_MS),
    setInterval(() => {
      const t = now();
      if (live() && t >= healthPausedUntil) broker.publish('/topic/health', viewerHealthAt(t));
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
    pauseHealth: (ms) => {
      healthPausedUntil = now() + ms;
    },
    drop: () => broker.dropAll(),
    stop: () => timers.forEach(clearInterval),
  };
}
