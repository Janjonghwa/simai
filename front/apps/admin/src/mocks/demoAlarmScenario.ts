import { nextMsgId } from '@/mocks/scenario/shared';
import type { RiskEventMessage } from '@simai/contracts';

/** 작업 시작 이후 경보와 이동이 공유하는 시각(ms). */
export const DEMO_TIMES = {
  helmet: 5_000,
  helmetResolved: 25_000,
  approach: 10_000,
  entry: 17_000,
  entryResolved: 50_000,
  fall: 35_000,
  fallResolved: 45_000,
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
    resolveAtMs: DEMO_TIMES.entryResolved,
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
    resolveAtMs: DEMO_TIMES.fallResolved,
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

/** 새로고침을 넘어 이어 갈 시나리오 상태(연습 빌드 저장, S15P31A101-806). */
export interface DemoAlarmSchedulerState {
  /** 지금까지 나눠 준 경보 번호(event_id 끝자리). */
  sequence: number;
  /** 진행 중인 실행: 작업 시작 시각과 이 실행 경보 번호의 시작점. 멈췄거나(작업 종료) 시작 전이면 null. */
  run: { startedAt: number; base: number } | null;
}

/** 저장분이 시나리오 상태 모양이면 그 값, 아니면 undefined. */
export function parseDemoAlarmSchedulerState(raw: unknown): DemoAlarmSchedulerState | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const { sequence, run } = raw as Record<string, unknown>;
  if (typeof sequence !== 'number' || !Number.isInteger(sequence) || sequence < 0) return undefined;
  if (run === null) return { sequence, run: null };
  if (typeof run !== 'object' || run === undefined) return undefined;
  const { startedAt, base } = run as Record<string, unknown>;
  if (typeof startedAt !== 'number' || !Number.isFinite(startedAt)) return undefined;
  if (typeof base !== 'number' || !Number.isInteger(base) || base < 0) return undefined;
  return { sequence, run: { startedAt, base } };
}

/** 이어 가기에서 지난 경보를 확인할 때 읽는 저장된 마지막 전이(없으면 undefined: 저장 전에 새로고침해 잃었다). */
export type DemoAlarmLookup = (
  eventId: string,
) => { lifecycle: string; raised_at?: number } | undefined;

/**
 * 작업 시작 시 시연 경보 단계를 작업 시작 시각 기준으로 예약하고, 종료나 재시작 시 남은 타이머를 정리한다.
 * 새로고침 뒤에는 resume 이 같은 시작 시각으로 이어 간다: 이미 지난 경보는 다시 보내지 않고(저장분에 있으면 그대로), 아직 안 온
 * 경보·해소만 남은 시각에 맞춰 예약한다. 저장 전에 새로고침해 저장분에 없는 지난 전이만 원래 시각으로 한 번 채운다.
 */
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
  let run: DemoAlarmSchedulerState['run'] = null;

  const later = (fn: () => void, delayMs: number) => {
    timers.push(setTimeout(fn, Math.max(0, delayMs)));
  };

  const raisedMessage = (step: DemoAlarmStep, eventId: string, at: number): RiskEventMessage => ({
    schema_version: '2.3.0',
    msg_type: 'risk_event',
    msg_id: nextMsgId(),
    sent_at: at,
    source_id: 'ec2',
    payload: {
      event_id: eventId,
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
  });

  const resolvedMessage = (raised: RiskEventMessage, at: number): RiskEventMessage => ({
    ...raised,
    msg_id: nextMsgId(),
    sent_at: at,
    payload: {
      ...raised.payload,
      lifecycle: 'RESOLVED',
      event_sequence: 2,
      updated_at: at,
      resolved_at: at,
      held_by_tier0: false,
    },
  });

  /** startedAt 기준으로 경보·해소를 예약한다. 이미 지난 것은 lookup 으로 저장분을 보고 빠진 것만 채운다. */
  function plan(startedAt: number, base: number, lookup?: DemoAlarmLookup) {
    const t0 = now();
    steps.forEach((step, index) => {
      const eventId = `${DEMO_EVENT_ID_PREFIX}${(base + index + 1).toString(16).padStart(12, '0')}`;
      const raiseAt = startedAt + step.delayMs;
      const resolveAt = step.resolveAtMs === undefined ? undefined : startedAt + step.resolveAtMs;
      const resolveLater = (raised: RiskEventMessage) => {
        if (resolveAt === undefined) return;
        later(() => publish(resolvedMessage(raised, now())), resolveAt - now());
      };
      if (raiseAt > t0) {
        later(() => {
          const raised = raisedMessage(step, eventId, now());
          publish(raised);
          resolveLater(raised);
        }, raiseAt - t0);
        return;
      }
      // 이미 지난 경보: 저장분에 있으면 다시 보내지 않는다.
      const stored = lookup?.(eventId);
      if (stored?.lifecycle === 'RESOLVED') return;
      const raised = raisedMessage(step, eventId, stored?.raised_at ?? raiseAt);
      if (!stored) publish(raised);
      if (resolveAt === undefined) return;
      if (resolveAt > t0) resolveLater(raised);
      else publish(resolvedMessage(raised, resolveAt));
    });
  }

  const clearTimers = () => {
    timers.forEach(clearTimeout);
    timers = [];
  };

  const stop = () => {
    clearTimers();
    run = null;
  };

  /** 작업 시작 시각(기본 지금)부터 새 실행을 연다. 경보 번호는 실행마다 새로 나눠 준다. */
  const start = (startedAt: number = now()) => {
    stop();
    run = { startedAt, base: sequence };
    sequence += steps.length;
    plan(run.startedAt, run.base);
  };

  /** 저장해 둔 상태로 이어 간다(진행 중인 실행이 없으면 번호만 잇는다). */
  const resume = (state: DemoAlarmSchedulerState, lookup: DemoAlarmLookup) => {
    clearTimers();
    sequence = Math.max(sequence, state.sequence);
    run = state.run ? { ...state.run } : null;
    if (run) plan(run.startedAt, run.base, lookup);
  };

  const exportState = (): DemoAlarmSchedulerState => ({
    sequence,
    run: run ? { ...run } : null,
  });

  return { start, stop, resume, exportState };
}
