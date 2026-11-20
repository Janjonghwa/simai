import { nextMsgId } from '@/mocks/scenario/shared';
import type { RiskEventMessage } from '@simai/contracts';

/** 작업 시작 이후 경보와 이동이 공유하는 시각(ms). */
export const DEMO_TIMES = {
  helmet: 5_000,
  helmetResolved: 25_000,
  approach: 10_000,
  entry: 17_000,
  fall: 35_000,
} as const;

export interface DemoAlarmStep {
  delayMs: number;
  ruleId: string;
  severity: 'CAUTION' | 'CRITICAL';
  personId: string;
  zoneId?: string;
  metrics: RiskEventMessage['payload']['metrics'];
  rationale?: string;
  resolveAtMs?: number;
}

export const DEMO_ALARM_STEPS: readonly DemoAlarmStep[] = [
  {
    delayMs: DEMO_TIMES.helmet,
    resolveAtMs: DEMO_TIMES.helmetResolved,
    ruleId: 'R9',
    severity: 'CAUTION',
    personId: 'w-0012',
    metrics: {},
    rationale: '작업자가 안전모를 쓰지 않고 있습니다. 머리 보호 장비 착용이 필요합니다.',
  },
  {
    delayMs: DEMO_TIMES.approach,
    resolveAtMs: DEMO_TIMES.entry,
    ruleId: 'R5',
    severity: 'CAUTION',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: { speed_mps: 0.88, zone_distance_m: 0.58 },
    rationale:
      '작업자가 단상 개구부 쪽으로 다가가고 있습니다. 이대로 움직이면 곧 위험 구역에 들어설 것으로 보입니다.',
  },
  {
    delayMs: DEMO_TIMES.entry,
    ruleId: 'R1',
    severity: 'CRITICAL',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: { zone_distance_m: 0 },
    rationale:
      '작업자가 단상 개구부 위험 구역의 경계를 넘어 안으로 들어갔습니다. 개구부 아래로 떨어질 수 있어 바로 확인이 필요합니다.',
  },
  {
    delayMs: DEMO_TIMES.fall,
    ruleId: 'R4',
    severity: 'CRITICAL',
    personId: 'w-0012',
    metrics: { head_z_m: 1.09 },
    rationale:
      '작업자가 평평한 작업면 위에 서 있다가 갑자기 바닥에 넘어져 머리 높이가 급격히 낮아졌습니다. 낙상 사고로 보입니다.',
  },
];

const DEMO_EVENT_ID_PREFIX = '0192a6f0-7c1e-7b3a-9f2d-';
const WORK_RUN_PATH = /\/api\/work-units\/[^/]+\/(start|end)$/;

/** 성공(2xx)한 작업 시작·종료 요청인지 판별한다. mocks/work.ts 를 건드리지 않고 MSW 응답 이벤트에서 시나리오를 여닫는다. */
export function workRunActionOf(
  request: Pick<Request, 'method' | 'url'>,
  response: Pick<Response, 'ok'>,
): 'start' | 'end' | null {
  if (request.method.toUpperCase() !== 'POST' || !response.ok) return null;
  let pathname: string;
  try {
    pathname = new URL(request.url, 'http://localhost').pathname;
  } catch {
    return null;
  }
  const match = WORK_RUN_PATH.exec(pathname);
  return match ? (match[1] as 'start' | 'end') : null;
}

/** 작업 시작 시 시연 경보 단계를 예약하고, 종료나 재시작 시 남은 타이머를 정리한다. */
export function createDemoAlarmScheduler({
  publish,
  now = Date.now,
  steps = DEMO_ALARM_STEPS,
}: {
  publish: (message: RiskEventMessage) => void;
  now?: () => number;
  steps?: readonly DemoAlarmStep[];
}) {
  let timers: ReturnType<typeof setTimeout>[] = [];
  let sequence = 0;

  const stop = () => {
    timers.forEach(clearTimeout);
    timers = [];
  };

  const start = () => {
    stop();
    for (const step of steps) {
      timers.push(
        setTimeout(() => {
          const at = now();
          const message: RiskEventMessage = {
            schema_version: '2.3.0',
            msg_type: 'risk_event',
            msg_id: nextMsgId(),
            sent_at: at,
            source_id: 'ec2',
            payload: {
              event_id: `${DEMO_EVENT_ID_PREFIX}${(++sequence).toString(16).padStart(12, '0')}`,
              event_sequence: 1,
              rule_version: 1001,
              source: 'LIVE',
              lifecycle: 'RAISED',
              verification: 'VERIFIED',
              confidence: 'HIGH',
              needs_check: false,
              rule_id: step.ruleId,
              severity: step.severity,
              world_ids: [step.personId],
              zone_id: step.zoneId ?? null,
              metrics: step.metrics,
              rationale: step.rationale ?? null,
              proposed_severity: null,
              raised_at: at,
              updated_at: at,
              held_by_tier0: true,
            },
          };
          publish(message);
          if (step.resolveAtMs !== undefined) {
            timers.push(
              setTimeout(() => {
                const resolvedAt = now();
                publish({
                  ...message,
                  msg_id: nextMsgId(),
                  sent_at: resolvedAt,
                  payload: {
                    ...message.payload,
                    lifecycle: 'RESOLVED',
                    event_sequence: 2,
                    updated_at: resolvedAt,
                    resolved_at: resolvedAt,
                    held_by_tier0: false,
                  },
                });
              }, step.resolveAtMs - step.delayMs),
            );
          }
        }, step.delayMs),
      );
    }
  };

  return { start, stop };
}
