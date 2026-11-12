import type { HealthSnapshot } from '@/store/realtime.store';

// 시계·마지막 수신 시각에 보이는 값을 viewer_health(명세 12.4 /topic/health)에서 만든다. 화면 컴포넌트 없이 시험한다.

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
