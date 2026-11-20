import { mergeReplay, requestReplay } from '@/features/history/api';
import { pastEventRows } from '@/features/history/pastEventRows';
import { chooseStep, fitLog, replayFiles } from '@/features/history/replay';
import { indexReplay } from '@/features/history/replayIndex';
import { replayResponseSchema } from '@/features/history/types';
import { parseReplayLog } from '@/features/viewer3d/replay/replayLog';
import {
  endWork,
  fetchReportsByWork,
  fetchWorkUnit,
  fetchWorkUnits,
  putDailyWorkUnit,
  signReport,
  startWork,
} from '@/features/work/api';
import {
  groupByDate,
  monthCells,
  monthOfKey,
  summarizeMonth,
  summarizeWorks,
} from '@/features/work/calendar/calendarMath';
import { createHttp } from '@/lib/http';
import { createDemoAlarmScheduler } from '@/mocks/demoAlarmScenario';
import { createDemoReplayStore } from '@/mocks/demoReplayStore';
import { createHistoryMock, historyReplay, historyWorks } from '@/mocks/history';
import { createTbmMock } from '@/mocks/tbm';
import { createWorkMock } from '@/mocks/work';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const BASE = 'http://api.test';
const TODAY = '2026-11-20';
const START_MS = new Date(2026, 10, 20, 14, 0, 0).getTime();

const png = () =>
  new Blob([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], { type: 'image/png' });

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  vi.useRealTimers();
  server.resetHandlers();
});
afterAll(() => server.close());

describe('목업 시연 경보 기록과 오늘 다시 보기(S15P31A101-834)', () => {
  it('시연 시나리오가 띄운 4건(6전이)을 세션 저장소에 남기고 /api/replay 가 시연 기록만 돌려준다', async () => {
    let clock = START_MS;
    const now = () => clock;

    const store = createDemoReplayStore();
    const workMock = createWorkMock({ now, eventCounts: store.countsOf });
    const tbmMock = createTbmMock({ deferDaysToHistory: true, dailyWorks: workMock.workItems });
    const historyMock = createHistoryMock({
      now,
      liveWorks: () => [...tbmMock.works, ...workMock.workItems()],
      demoReplay: store,
    });
    server.use(...workMock.handlers, ...tbmMock.handlers, ...historyMock.handlers);

    const asAdmin = createHttp({ baseUrl: BASE, getToken: () => 'mock-token.admin' });
    const work = await putDailyWorkUnit(asAdmin, TODAY);
    workMock.markSetupDone(work.work_id);
    const report = (await fetchReportsByWork(asAdmin, work.work_id)).latest!;
    await signReport(asAdmin, report.report_id, {
      image: png(),
      signerName: '김안전',
      reportVersion: report.report_version,
    });

    const started = await startWork(asAdmin, work.work_id);
    const runStart = started.runs![0]!.started_at;
    store.startRun(work.work_id, runStart);

    // 스케줄러 타이머 구간만 가짜 시계로 45초 진행한다(fetch·msw 호출에는 실 시계를 쓴다).
    vi.useFakeTimers();
    vi.setSystemTime(runStart);
    const scheduler = createDemoAlarmScheduler({
      now: () => Date.now(),
      publish: (message) => {
        if (message.payload.zone_id) message.payload.zone_id = 'z-demo-opening';
        store.recordEvent(work.work_id, message);
      },
    });
    scheduler.start();

    // 45초 진행(5초 안전모, 10초 접근, 17초 접근 해소·진입, 25초 안전모 해소, 35초 낙상)
    vi.advanceTimersByTime(45_000);
    scheduler.stop();
    clock = Date.now();
    vi.useRealTimers();

    const ended = await endWork(asAdmin, work.work_id);
    const runEnd = ended.work_unit.runs![0]!.ended_at!;
    store.endRun(work.work_id, runEnd);

    // 1. /api/replay 가 시연 경보 6전이(4건)를 스키마·재생 엔진 오류 없이 돌려준다.
    const stepMs = chooseStep(runStart, runEnd);
    const replay = await requestReplay(asAdmin, { from: runStart, to: runEnd, stepMs });
    expect(replay.initial_events).toEqual([]);
    expect(replay.events).toHaveLength(6);
    expect(replay.gaps).toEqual([]);
    expect(replay.world_states.length).toBeGreaterThan(0);

    // 17초 동시 전이(R5 해소·R1 발생)도 msg_id 가 겹치지 않아 재생 엔진이 하나도 빠뜨리지 않는다.
    expect(new Set(replay.events.map((event) => event.msg_id)).size).toBe(6);
    const log = parseReplayLog(replayFiles(replay));
    expect(log.issues).toEqual([]);
    expect(log.events).toHaveLength(6);

    const index = indexReplay(fitLog(log, { from: runStart, to: runEnd }), replay.gaps);
    expect(index.events).toHaveLength(4);
    expect(
      index.events.map((event) => ({
        ruleId: event.ruleId,
        severity: event.severity,
        worldIds: event.worldIds,
        zoneId: event.zoneId,
        resolved: event.resolvedAt !== null,
      })),
    ).toEqual([
      { ruleId: 'R9', severity: 'CAUTION', worldIds: ['w-0012'], zoneId: null, resolved: true },
      {
        ruleId: 'R5',
        severity: 'CAUTION',
        worldIds: ['w-0012'],
        zoneId: 'z-demo-opening',
        resolved: true,
      },
      {
        ruleId: 'R1',
        severity: 'CRITICAL',
        worldIds: ['w-0012'],
        zoneId: 'z-demo-opening',
        resolved: false,
      },
      { ruleId: 'R4', severity: 'CRITICAL', worldIds: ['w-0012'], zoneId: null, resolved: false },
    ]);
    expect(pastEventRows(index.events)).toHaveLength(4);

    // 2. 구간을 나눠 받아도 initial_events(20초 시점 활성 R9·R1)와 events(25초 R9 해소·35초 R4 발생)가 정확하다.
    const mid = runStart + 20_000;
    const first = replayResponseSchema.parse(historyReplay([], runStart, mid, 500, runEnd, store));
    const second = replayResponseSchema.parse(historyReplay([], mid, runEnd, 500, runEnd, store));
    expect(second.initial_events.map((event) => Reflect.get(event.payload, 'rule_id'))).toEqual([
      'R9',
      'R1',
    ]);
    expect(second.events.map((event) => Reflect.get(event.payload, 'rule_id'))).toEqual([
      'R9',
      'R4',
    ]);
    const whole = replayResponseSchema.parse(
      historyReplay([], runStart, runEnd, 500, runEnd, store),
    );
    const merged = mergeReplay([first, second]);
    expect(merged.events.map((e) => e.msg_id)).toEqual(whole.events.map((e) => e.msg_id));

    // 3. 오늘 작업의 event_counts·incident_counts·종료 미팅·달력 월 요약이 모두 위험 2·주의 2(4건)로 일치한다.
    expect(ended.work_unit.event_counts).toEqual({ CRITICAL: 2, HIGH: 2 });
    expect(ended.work_unit.incident_counts).toEqual({ CRITICAL: 2, HIGH: 2 });
    const unit = await fetchWorkUnit(asAdmin, work.work_id);
    expect(unit.event_counts).toEqual({ CRITICAL: 2, HIGH: 2 });
    expect(unit.incident_counts).toEqual({ CRITICAL: 2, HIGH: 2 });

    // 종료 미팅 자동 생성 대기(2.5초) 뒤 종료 문장이 '사건 4건'으로 채워진다.
    clock += 2_500;
    const finishedReport = (await fetchReportsByWork(asAdmin, work.work_id)).latest!;
    expect(finishedReport.form?.review.ending).toBe('작업 종료 · 총 작업 시간 00:00:45, 사건 4건');

    // 달력 목록(11월): 지난 날(11-18·19)은 그대로 두고 오늘(11-20) 위험 2·주의 2가 월 요약에 합쳐진다.
    const monthWorks = await fetchWorkUnits(asAdmin, { from: '2026-11-01', to: '2026-11-30' });
    const byDate = groupByDate(monthWorks);
    const day18 = summarizeWorks(byDate.get('2026-11-18') ?? [])!;
    const day19 = summarizeWorks(byDate.get('2026-11-19') ?? [])!;
    const day20 = summarizeWorks(byDate.get(TODAY) ?? [])!;
    expect(day18).toMatchObject({ count: 3, critical: 9, high: 27 });
    expect(day19).toMatchObject({ count: 3, critical: 17, high: 51 });
    expect(day20).toEqual({
      count: 1,
      critical: 2,
      high: 2,
      incidents: { critical: 2, high: 2 },
    });
    const monthSummary = summarizeMonth(monthCells(monthOfKey(TODAY)), byDate, TODAY);
    expect(monthSummary).toEqual({
      days: 3,
      critical: day18.critical + day19.critical + 2,
      high: day18.high + day19.high + 2,
    });

    // 지난 날(11-18) /api/replay 기록은 그대로 유지된다.
    const pastWork = historyWorks(clock).find((w) => w.date === '2026-11-18')!;
    const pastReplay = replayResponseSchema.parse(
      historyReplay(historyWorks(clock), pastWork.from_ms, pastWork.to_ms, 5_000, clock, store),
    );
    expect(pastReplay.events.length).toBeGreaterThan(0);
  });

  it('오늘 실행에 경보가 0건이거나 두 번째 실행을 해도 가짜 기록이 섞이지 않고 앞 실행의 미해소 경보가 새 실행으로 새지 않는다', () => {
    vi.useFakeTimers();
    vi.setSystemTime(START_MS);
    const store = createDemoReplayStore();

    // 실행 1: 35초 이상 진행해 미해소(R1·R4) 경보를 남긴다.
    const run1Start = Date.now();
    store.startRun('9001', run1Start);
    const scheduler = createDemoAlarmScheduler({
      now: () => Date.now(),
      publish: (message) => store.recordEvent('9001', message),
    });
    scheduler.start();
    vi.advanceTimersByTime(40_000);
    scheduler.stop();
    const run1End = Date.now();
    store.endRun('9001', run1End);

    // 실행 2: 3초 만에 종료해 시연 경보가 0건이다.
    vi.advanceTimersByTime(10_000);
    const run2Start = Date.now();
    store.startRun('9001', run2Start);
    vi.advanceTimersByTime(3_000);
    const run2End = Date.now();
    store.endRun('9001', run2End);

    const replay2 = replayResponseSchema.parse(
      historyReplay([], run2Start, run2End, 100, run2End, store),
    );
    expect(replay2.initial_events).toEqual([]);
    expect(replay2.events).toEqual([]);
    expect(replay2.gaps).toEqual([]);
    expect(replay2.world_states.length).toBeGreaterThan(0);
  });
});
