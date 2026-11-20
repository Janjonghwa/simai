import {
  createDemoAlarmScheduler,
  DEMO_ALARM_STEPS,
  workRunActionOf,
} from '@/mocks/demoAlarmScenario';
import { defaultHealthPeopleSnapshot } from '@/mocks/peopleNames';
import { validateMessage, type RiskEventMessage } from '@simai/contracts';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.useRealTimers();
});

describe('목업 시연 경보 시나리오(mocks/startBackend, S15P31A101-824)', () => {
  it('작업 시작 뒤 5초(R9 주의)·12초(R5 주의)·14초(R2 주의)·20초(R1+R4 위험) 순으로 계약 형식 경보를 송출한다', () => {
    vi.useFakeTimers();
    let now = 1_795_000_000_000;
    const received: { elapsed: number; message: RiskEventMessage }[] = [];
    const startAt = now;
    const scheduler = createDemoAlarmScheduler({
      now: () => now,
      publish: (message) => {
        received.push({ elapsed: now - startAt, message });
      },
    });

    scheduler.start();
    expect(received).toEqual([]);

    // 5초: 안전모 미착용(주의)
    now += 5_000;
    vi.advanceTimersByTime(5_000);
    expect(received).toHaveLength(1);
    expect(received[0]!.message.payload).toMatchObject({
      rule_id: 'R9',
      severity: 'CAUTION',
      world_ids: ['w-0015'],
      zone_id: null,
    });

    // 12초: 위험구역 접근 예측(주의)
    now += 7_000;
    vi.advanceTimersByTime(7_000);
    expect(received).toHaveLength(2);
    expect(received[1]!.message.payload).toMatchObject({
      rule_id: 'R5',
      severity: 'CAUTION',
      world_ids: ['w-0012'],
      zone_id: 'z-04',
      metrics: { time_to_entry_s: 1.6 },
    });

    // 14초: 추락 위험 가장자리(주의)
    now += 2_000;
    vi.advanceTimersByTime(2_000);
    expect(received).toHaveLength(3);
    expect(received[2]!.message.payload).toMatchObject({
      rule_id: 'R2',
      severity: 'CAUTION',
      world_ids: ['w-0012'],
      zone_id: 'z-04',
      metrics: { edge_distance_m: 0.8 },
    });

    // 20초: 위험구역 진입 + 낙상 의심(위험)
    now += 6_000;
    vi.advanceTimersByTime(6_000);
    expect(received).toHaveLength(5);
    expect(received[3]!.message.payload).toMatchObject({
      rule_id: 'R1',
      severity: 'CRITICAL',
      world_ids: ['w-0012'],
      zone_id: 'z-04',
      metrics: { zone_distance_m: 0 },
    });
    expect(received[4]!.message.payload).toMatchObject({
      rule_id: 'R4',
      severity: 'CRITICAL',
      world_ids: ['w-0012'],
      zone_id: 'z-04',
    });

    // 모든 메시지는 계약 스키마를 통과하고, 대상 작업자 ID 는 목업 작업자 이름표에 등록되어 있다.
    const namedWorlds = new Map(
      defaultHealthPeopleSnapshot(now).personNames.map((p) => [p.world_id, p.display_name]),
    );
    for (const { message } of received) {
      expect(validateMessage('risk_event', message)).toMatchObject({ ok: true });
      for (const id of message.payload.world_ids) {
        expect(namedWorlds.get(id)).toMatch(/^(김철수|이영희)$/);
      }
    }
    expect(new Set(received.map((r) => r.message.payload.event_id)).size).toBe(
      DEMO_ALARM_STEPS.length,
    );
  });

  it('작업 종료(stop) 시 남은 시연 경보 타이머를 취소하고, 다시 시작하면 처음부터 다시 울린다', () => {
    vi.useFakeTimers();
    const published: RiskEventMessage[] = [];
    const scheduler = createDemoAlarmScheduler({ publish: (m) => published.push(m) });

    scheduler.start();
    vi.advanceTimersByTime(5_000);
    expect(published).toHaveLength(1);

    scheduler.stop();
    vi.advanceTimersByTime(30_000);
    expect(published).toHaveLength(1);

    scheduler.start();
    vi.advanceTimersByTime(20_000);
    expect(published).toHaveLength(6);
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
