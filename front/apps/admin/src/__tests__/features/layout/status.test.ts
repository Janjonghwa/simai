import { clockLabel, lastDataClock } from '@/features/layout/status';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import type { HealthSnapshot } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;

function snapshot(change: Partial<ViewerHealthPayload> = {}, receivedAt = T): HealthSnapshot {
  const payload = { ...viewerHealthAt(T).payload, ...change };
  return { payload, receivedAt, clockOffsetMs: 0 };
}

describe('시계', () => {
  it('시계는 EC2 와의 차이를 더한 시각이다', () => {
    const now = new Date(2026, 9, 21, 14, 30, 5).getTime();
    expect(clockLabel(0, now)).toBe('14:30:05');
    expect(clockLabel(-5000, now)).toBe('14:30:00');
    expect(clockLabel(undefined, now)).toBeUndefined();
  });

  it('마지막 데이터 시각은 EC2 시각에서 데이터 연령을 뺀 때다', () => {
    const asOf = new Date(2026, 9, 21, 14, 30, 5).getTime();
    expect(lastDataClock(snapshot({ as_of: asOf, data_age_ms: 12_000 }))).toBe('14:29:53');
    expect(lastDataClock(snapshot({ data_age_ms: null }))).toBeUndefined();
    expect(lastDataClock(null)).toBeUndefined();
  });
});
