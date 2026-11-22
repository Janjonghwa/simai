import {
  arrayOf,
  cloneJson,
  isNumber,
  isNumberOrNull,
  isRecord,
  isString,
  type MockSessionPart,
} from '@/mocks/mockSession';
import type { RiskEventMessage } from '@simai/contracts';

/** 목업 작업의 등급별 사건 수(화면 2단 기준: 위험 CRITICAL, 주의 HIGH). */
export interface DemoEventCounts {
  CRITICAL: number;
  HIGH: number;
}

/** 실시간 시연 메시지를 `/api/replay` 저장 형식(서버 ReplayRepository 모양)으로 복사한다. */
function toReplayEvent(message: RiskEventMessage) {
  const cloned = JSON.parse(JSON.stringify(message)) as RiskEventMessage;
  const payload = {
    ...cloned.payload,
    severity: cloned.payload.severity === 'HIGH' ? ('CAUTION' as const) : cloned.payload.severity,
    delivery: 'REALTIME' as const,
  };
  delete payload.held_by_tier0;
  return {
    ...cloned,
    payload,
  };
}

/** `/api/replay` 응답에 실을 시연 경보 전이(원래 msg_id 유지, held_by_tier0 제거, delivery REALTIME). */
export type ReplayStoredEvent = ReturnType<typeof toReplayEvent>;

/** 세션 메모리에 남기는 시연 실행 한 건과 그 실행 동안 발생한 경보 전이 기록. */
export interface DemoReplayRun {
  workId: string;
  startedAt: number;
  endedAt: number | null;
  events: ReplayStoredEvent[];
}

/** 히스토리·작업 목업이 시연 기록 저장소에서 읽는 읽기 인터페이스. */
export interface DemoReplayReader {
  /** `[from, to]` 구간이 들어 있는 시연 실행을 찾는다. 없으면 undefined. */
  findRun(from: number, to: number): DemoReplayRun | undefined;
  /** `run` 에서 `from` 시각에 활성(마지막 전이가 RESOLVED 가 아님)인 이벤트 목록. */
  initialEventsAt(run: DemoReplayRun, from: number): ReplayStoredEvent[];
  /** `run` 에서 `(from, to]` 사이에 일어난 경보 전이 목록(시각순). */
  eventsBetween(run: DemoReplayRun, from: number, to: number): ReplayStoredEvent[];
  /** 해당 작업에 시연 실행 기록이 남아 있는지. */
  hasWork(workId: string): boolean;
  /** 해당 작업의 시연 경보 발생(RAISED) 수. 시연 실행 기록이 없으면 null. */
  countsOf(workId: string): DemoEventCounts | null;
}

export interface DemoReplayStore extends DemoReplayReader {
  /** 새 시연 실행 구간을 연다(열려 있던 같은 작업 실행은 닫는다). */
  startRun(workId: string, startedAt: number): void;
  /** 진행 중인 시연 실행에 경보 전이 하나를 남긴다. */
  recordEvent(workId: string, message: RiskEventMessage): void;
  /** 진행 중인 시연 실행 구간을 닫는다. */
  endRun(workId: string, endedAt: number): void;
  /** 진행 중인 시연 실행에서 그 이벤트의 마지막 전이. 없으면 undefined(새로고침 뒤 시나리오 이어 가기가 지난 경보를 확인한다). */
  latestEvent(workId: string, eventId: string): ReplayStoredEvent | undefined;
  /** 저장소를 비운다(시험 초기화용). */
  clear(): void;
  /** 연습 빌드 저장·복원(S15P31A101-806): 실행 구간과 경보 전이 기록. */
  persist: MockSessionPart;
}

/** 이벤트 전이 시각(재생 엔진 replayLog.ts 와 같은 우선순위). */
export function transitionTimeOf(message: {
  sent_at: number;
  payload: { updated_at?: number; resolved_at?: number | null; raised_at?: number };
}): number {
  return (
    message.payload.updated_at ??
    message.payload.resolved_at ??
    message.payload.raised_at ??
    message.sent_at
  );
}

/**
 * 목업 시연 경보(demoAlarmScenario)가 띄운 실행·경보 전이를 세션 메모리에 남기는 저장소.
 * `/api/replay` 와 작업 목록(`event_counts`·`incident_counts`)·종료 미팅이 이 저장소를 함께 읽는다.
 */
export function createDemoReplayStore(): DemoReplayStore {
  const runs: DemoReplayRun[] = [];

  const openRunOf = (workId: string) =>
    [...runs].reverse().find((run) => run.workId === workId && run.endedAt === null);

  function startRun(workId: string, startedAt: number) {
    const prev = openRunOf(workId);
    if (prev) prev.endedAt = startedAt;
    runs.push({ workId, startedAt, endedAt: null, events: [] });
  }

  function recordEvent(workId: string, message: RiskEventMessage) {
    const run = openRunOf(workId);
    if (!run) return;
    run.events.push(toReplayEvent(message));
  }

  function endRun(workId: string, endedAt: number) {
    const run = openRunOf(workId);
    if (run) run.endedAt = endedAt;
  }

  function findRun(from: number, to: number): DemoReplayRun | undefined {
    // 같은 날 여러 번 실행했을 때 가장 최근 실행부터 찾는다.
    for (let i = runs.length - 1; i >= 0; i -= 1) {
      const run = runs[i]!;
      if (run.startedAt <= from && (run.endedAt === null || to <= run.endedAt)) {
        return run;
      }
    }
    return undefined;
  }

  function initialEventsAt(run: DemoReplayRun, from: number): ReplayStoredEvent[] {
    const latest = new Map<string, ReplayStoredEvent>();
    for (const message of run.events) {
      if (transitionTimeOf(message) <= from) {
        latest.set(message.payload.event_id, message);
      }
    }
    return [...latest.values()].filter((message) => message.payload.lifecycle !== 'RESOLVED');
  }

  function eventsBetween(run: DemoReplayRun, from: number, to: number): ReplayStoredEvent[] {
    return run.events
      .map((message, order) => ({ message, order, t: transitionTimeOf(message) }))
      .filter(({ t }) => t > from && t <= to)
      .sort((a, b) => a.t - b.t || a.order - b.order)
      .map(({ message }) => message);
  }

  const hasWork = (workId: string) => runs.some((run) => run.workId === workId);

  function countsOf(workId: string): DemoEventCounts | null {
    const matched = runs.filter((run) => run.workId === workId);
    if (matched.length === 0) return null;
    let critical = 0;
    let high = 0;
    for (const run of matched) {
      for (const message of run.events) {
        if (message.payload.lifecycle !== 'RAISED') continue;
        if (message.payload.severity === 'CRITICAL') critical += 1;
        else if (message.payload.severity === 'CAUTION') {
          high += 1;
        }
      }
    }
    return { CRITICAL: critical, HIGH: high };
  }

  function latestEvent(workId: string, eventId: string): ReplayStoredEvent | undefined {
    const run = openRunOf(workId);
    if (!run) return undefined;
    for (let i = run.events.length - 1; i >= 0; i -= 1) {
      const message = run.events[i]!;
      if (message.payload.event_id === eventId) return message;
    }
    return undefined;
  }

  const persist: MockSessionPart = {
    exportState: () => cloneJson(runs),
    loadState: (raw) => {
      const saved = arrayOf(
        raw,
        (run): run is DemoReplayRun =>
          isRecord(run) &&
          isString(run.workId) &&
          isNumber(run.startedAt) &&
          isNumberOrNull(run.endedAt) &&
          arrayOf(
            run.events,
            (m): m is ReplayStoredEvent =>
              isRecord(m) && isRecord(m.payload) && isString(m.payload.event_id),
          ) !== undefined,
      );
      if (!saved) return undefined;
      return () => {
        runs.splice(0, runs.length, ...cloneJson(saved));
      };
    },
  };

  return {
    startRun,
    latestEvent,
    persist,
    recordEvent,
    endRun,
    findRun,
    initialEventsAt,
    eventsBetween,
    hasWork,
    countsOf,
    clear: () => {
      runs.length = 0;
    },
  };
}
