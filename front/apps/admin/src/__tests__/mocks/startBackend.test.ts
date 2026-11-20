import {
  createDemoAlarmScheduler,
  DEMO_ALARM_STEPS,
  workRunActionOf,
} from '@/mocks/demoAlarmScenario';
import { ppeViewAt } from '@/mocks/scenario/ppe';
import { worldStateAt } from '@/mocks/scenario/world';
import { validateMessage, type RiskEventMessage } from '@simai/contracts';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.useRealTimers();
});

describe('목업 시연 경보 시나리오(mocks/startBackend, S15P31A101-824)', () => {
  it('작업 시작 뒤 5·10·17·35초에 네 경보를 계약 형식으로 송출한다', () => {
    vi.useFakeTimers();
    const received: RiskEventMessage[] = [];
    const scheduler = createDemoAlarmScheduler({ publish: (message) => received.push(message) });
    scheduler.start();
    for (const [delay, rule, severity, zone] of [
      [5_000, 'R9', 'CAUTION', null],
      [5_000, 'R5', 'CAUTION', 'z-04'],
      [7_000, 'R1', 'CRITICAL', 'z-04'],
      [18_000, 'R4', 'CRITICAL', null],
    ] as const) {
      vi.advanceTimersByTime(delay);
      expect(
        received.filter((m) => m.payload.lifecycle === 'RAISED').at(-1)?.payload,
      ).toMatchObject({
        rule_id: rule,
        severity,
        zone_id: zone,
        world_ids: ['w-0012'],
      });
    }
    vi.advanceTimersByTime(25_000);
    expect(received.filter((m) => m.payload.lifecycle === 'RAISED')).toHaveLength(4);
    const resolved = received.filter((m) => m.payload.lifecycle === 'RESOLVED');
    expect(resolved.map((m) => m.payload.rule_id)).toEqual(['R5', 'R9']);
    for (const message of resolved) {
      const raised = received.find(
        (m) => m.payload.event_id === message.payload.event_id && m.payload.lifecycle === 'RAISED',
      );
      expect(message.payload).toMatchObject({
        event_sequence: 2,
        raised_at: raised?.payload.raised_at,
        resolved_at: message.sent_at,
      });
    }
    for (const message of received)
      expect(validateMessage('risk_event', message)).toMatchObject({ ok: true });
    expect(new Set(received.map((message) => message.payload.event_id)).size).toBe(
      DEMO_ALARM_STEPS.length,
    );
  });

  it('종료 시 남은 타이머를 취소하고 다시 시작하면 처음부터 울린다', () => {
    vi.useFakeTimers();
    const published: RiskEventMessage[] = [];
    const scheduler = createDemoAlarmScheduler({ publish: (message) => published.push(message) });
    scheduler.start();
    vi.advanceTimersByTime(10_000);
    scheduler.stop();
    vi.advanceTimersByTime(50_000);
    expect(published).toHaveLength(2);
    scheduler.start();
    vi.advanceTimersByTime(43_000);
    expect(published.filter((m) => m.payload.lifecycle === 'RAISED')).toHaveLength(6);
  });

  it('목업 트랙은 경보 시각에 한 사람이 접근·진입·하강한 뒤 제자리에서 일어난다', () => {
    const TIME = 1_795_000_000_000;
    const at = (ms: number) =>
      worldStateAt(1_795_000_000_000, { demoElapsedMs: ms }).payload.tracks;
    expect(at(0)[0]?.position_xy).toEqual(at(5_000)[0]?.position_xy);
    expect(ppeViewAt(TIME, 4_999).payload.people[0]?.helmet.state).toBe('WORN');
    expect(ppeViewAt(TIME, 5_000).payload.people[0]?.helmet.state).toBe('NOT_WORN');
    expect(ppeViewAt(TIME, 25_000).payload.people[0]?.helmet.state).toBe('WORN');
    expect(at(10_000)).toHaveLength(1);
    expect(at(10_000)[0]?.position_xy[0]).toBeCloseTo(4.62);
    expect(at(17_000)[0]?.position_xy[0]).toBeGreaterThan(5.2);
    expect(at(35_000)[0]?.head_z).toBe(1.09);
    expect(at(43_000)[0]?.position_xy[0]).toBeCloseTo(6);
    expect(at(60_000)[0]?.position_xy[0]).toBeCloseTo(6);
  });

  it('workRunActionOf 는 성공(2xx)한 POST /api/work-units/:id/start·end 만 감지한다', () => {
    expect(
      workRunActionOf(
        { method: 'POST', url: 'https://mock.simai.invalid/api/work-units/9001/start' },
        { ok: true },
      ),
    ).toBe('start');
    expect(
      workRunActionOf(
        { method: 'POST', url: 'https://mock.simai.invalid/api/work-units/9001/end' },
        { ok: true },
      ),
    ).toBe('end');
    expect(
      workRunActionOf(
        { method: 'POST', url: 'https://mock.simai.invalid/api/work-units/9001/start' },
        { ok: false },
      ),
    ).toBeNull();
    expect(
      workRunActionOf(
        { method: 'GET', url: 'https://mock.simai.invalid/api/work-units/9001/start' },
        { ok: true },
      ),
    ).toBeNull();
    expect(
      workRunActionOf(
        { method: 'POST', url: 'https://mock.simai.invalid/api/events/1/ack' },
        { ok: true },
      ),
    ).toBeNull();
  });
});
