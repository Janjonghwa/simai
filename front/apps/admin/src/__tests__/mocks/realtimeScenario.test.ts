import {
  EVENT_CYCLE_MS,
  riskEventsBetween,
  viewerHealthAt,
  worldStateAt,
} from '@/mocks/realtimeScenario';
import { validateMessage } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

const T0 = 1_760_342_400_000;

describe('실시간 mock 시나리오', () => {
  it('world·health 메시지는 계약을 통과한다', () => {
    for (let i = 0; i < 50; i++) {
      const t = T0 + i * 733;
      const world = validateMessage('world_state', worldStateAt(t));
      const health = validateMessage('viewer_health', viewerHealthAt(t));
      expect(world.ok ? [] : world.errors).toEqual([]);
      expect(health.ok ? [] : health.errors).toEqual([]);
    }
  });

  it('작업자는 교실(10×10m) 안에 있다', () => {
    for (let i = 0; i < 100; i++) {
      for (const track of worldStateAt(T0 + i * 977).payload.tracks) {
        const [x, y] = track.position_xy ?? [NaN, NaN];
        expect(x).toBeGreaterThanOrEqual(0);
        expect(x).toBeLessThanOrEqual(10);
        expect(y).toBeGreaterThanOrEqual(0);
        expect(y).toBeLessThanOrEqual(10);
      }
    }
  });

  it('위험 이벤트는 한 바퀴에 정해진 전이를 한 번씩 순서대로 보내고, 모두 계약을 통과한다', () => {
    const start = Math.floor(T0 / EVENT_CYCLE_MS) * EVENT_CYCLE_MS;
    const events = [];
    // 1초 간격으로 겹치지 않게 훑는다(mock 송출과 같은 방식).
    for (let t = start; t < start + EVENT_CYCLE_MS; t += 1000) {
      events.push(...riskEventsBetween(t, t + 1000));
    }
    const byEvent = new Map<string, { rule: string; steps: string[]; sequences: number[] }>();
    for (const { payload } of events) {
      const seen = byEvent.get(payload.event_id) ?? {
        rule: `${payload.rule_id} ${payload.severity}`,
        steps: [],
        sequences: [],
      };
      seen.steps.push(payload.lifecycle);
      seen.sequences.push(payload.event_sequence ?? 0);
      byEvent.set(payload.event_id, seen);
    }
    expect([...byEvent.values()].map(({ rule, steps }) => [rule, steps])).toEqual([
      ['R5 CAUTION', ['RAISED', 'RESOLVED']],
      ['R2 CRITICAL', ['RAISED', 'UPDATED', 'RESOLVED']],
      ['R1 HIGH', ['RAISED', 'RESOLVED']],
      ['R1 HIGH', ['RAISED', 'RESOLVED', 'UPDATED', 'RESOLVED']],
    ]);
    for (const { sequences } of byEvent.values()) {
      expect(sequences).toEqual(sequences.map((_, i) => i + 1));
    }
    for (const event of events) {
      const checked = validateMessage('risk_event', event);
      expect(checked.ok ? [] : checked.errors).toEqual([]);
    }
  });

  it('재개된 이벤트는 resume_count 와 확인 필요 이유를 싣는다', () => {
    const resumed = riskEventsBetween(0, EVENT_CYCLE_MS).find((e) => e.payload.resume_count === 1);
    expect(resumed?.payload).toMatchObject({
      lifecycle: 'UPDATED',
      needs_check: true,
      resolved_at: null,
      world_ids: ['w-0021'],
    });
  });

  it('바퀴마다 다른 이벤트 id 를 쓴다', () => {
    const first = riskEventsBetween(0, EVENT_CYCLE_MS)[0]?.payload.event_id;
    const second = riskEventsBetween(EVENT_CYCLE_MS, 2 * EVENT_CYCLE_MS)[0]?.payload.event_id;
    expect(first).toBeDefined();
    expect(first).not.toBe(second);
  });
});
