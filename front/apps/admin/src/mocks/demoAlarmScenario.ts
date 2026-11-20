import { nextMsgId } from '@/mocks/scenario/shared';
import type { RiskEventMessage } from '@simai/contracts';

/** 목업 시연 경보 단계 한 건(S15P31A101-824). 사람 ID 는 mocks/peopleNames 의 목업 작업자(이영희·김철수)와 맞춘다. */
export interface DemoAlarmStep {
  delayMs: number;
  ruleId: string;
  severity: 'CAUTION' | 'CRITICAL';
  personId: string;
  zoneId?: string;
  metrics: RiskEventMessage['payload']['metrics'];
}

/**
 * 목업 빌드 작업 시작 시연 경보 시나리오:
 * - 5초: 안전모 미착용(주의, 이영희 w-0015)
 * - 12초: 위험구역 접근 예측(주의, 김철수 w-0012, 단상 개구부 z-04)
 * - 14초: 추락 위험 가장자리(주의, 김철수 w-0012, 단상 개구부 z-04)
 * - 20초: 위험구역 진입 + 낙상 의심(위험, 김철수 w-0012, 단상 개구부 z-04)
 */
export const DEMO_ALARM_STEPS: readonly DemoAlarmStep[] = [
  { delayMs: 5_000, ruleId: 'R9', severity: 'CAUTION', personId: 'w-0015', metrics: {} },
  {
    delayMs: 12_000,
    ruleId: 'R5',
    severity: 'CAUTION',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: { time_to_entry_s: 1.6 },
  },
  {
    delayMs: 14_000,
    ruleId: 'R2',
    severity: 'CAUTION',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: { edge_distance_m: 0.8 },
  },
  {
    delayMs: 20_000,
    ruleId: 'R1',
    severity: 'CRITICAL',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: { zone_distance_m: 0 },
  },
  {
    delayMs: 20_000,
    ruleId: 'R4',
    severity: 'CRITICAL',
    personId: 'w-0012',
    zoneId: 'z-04',
    metrics: {},
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
              rationale: null,
              proposed_severity: null,
              raised_at: at,
              updated_at: at,
              held_by_tier0: true,
            },
          };
          publish(message);
        }, step.delayMs),
      );
    }
  };

  return { start, stop };
}
