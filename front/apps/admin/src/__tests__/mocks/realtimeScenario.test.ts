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

  describe('바퀴의 20~26초에는 cam3 가 키프레임을 기다린다', () => {
    const at = (t: number, faults?: boolean) => {
      const { payload } = viewerHealthAt(t, { faults });
      const cameras = (payload.tier0_health as { cameras: { id: string; status: string }[] })
        .cameras;
      return {
        system: payload.system_health,
        cam3: cameras.find((camera) => camera.id === 'cam3')?.status,
        others: cameras.filter((camera) => camera.id !== 'cam3').map((camera) => camera.status),
        // cam3 가 영상을 보내지 않는 동안에는 world 에서도 cam3 로 관측하지 않는다.
        observed: new Set(worldStateAt(t).payload.tracks.flatMap((track) => track.cameras)),
      };
    };

    it.each([
      [19_999, 'NOMINAL', 'OK'],
      [20_000, 'DEGRADED', 'WAITING_KEYFRAME'],
      [25_999, 'DEGRADED', 'WAITING_KEYFRAME'],
      [26_000, 'NOMINAL', 'OK'],
      [EVENT_CYCLE_MS + 21_000, 'DEGRADED', 'WAITING_KEYFRAME'],
    ])('바퀴 시작 + %ims: %s, cam3 %s', (offset, system, cam3) => {
      const state = at(T0 + offset);
      expect(state.system).toBe(system);
      expect(state.cam3).toBe(cam3);
      expect(state.others).toEqual(['OK', 'OK', 'OK']);
      expect(state.observed.has('cam3')).toBe(cam3 === 'OK');
    });

    it('고장 흉내를 끄면 그 구간에도 정상이다', () => {
      expect(at(T0 + 21_000, false)).toMatchObject({ system: 'NOMINAL', cam3: 'OK' });
    });
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
