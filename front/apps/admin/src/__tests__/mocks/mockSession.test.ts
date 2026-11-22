// @vitest-environment node
import { createSetupSession } from '@/features/setup/api';
import { fetchWorkUnit, putDailyWorkUnit } from '@/features/work/api';
import { commitZone } from '@/features/zones/api';
import { bodyOf, formOf } from '@/features/zones/zoneForm';
import { readZones } from '@/features/zones/zones';
import { createHttp } from '@/lib/http';
import {
  createDemoAlarmScheduler,
  DEMO_ALARM_STEPS,
  DEMO_TIMES,
  parseDemoAlarmSchedulerState,
} from '@/mocks/demoAlarmScenario';
import { createDemoReplayStore } from '@/mocks/demoReplayStore';
import { createEventsMock } from '@/mocks/events';
import { clearMockState, MOCK_STATE_PREFIX, type MockStateStorage } from '@/mocks/mockPersist';
import {
  createMockSession,
  MOCK_SESSION_KEY,
  MOCK_SESSION_VERSION,
  seoulDateKey,
} from '@/mocks/mockSession';
import { createPeopleNamesMock } from '@/mocks/peopleNames';
import { createSetupMock } from '@/mocks/setup';
import { createStaticObjectsMock } from '@/mocks/staticObjects';
import type { StompBroker } from '@/mocks/stompBroker';
import { createTbmMock } from '@/mocks/tbm';
import { createWorkMock } from '@/mocks/work';
import { createZonesMock, MOCK_ZONES } from '@/mocks/zones';
import type { RiskEventMessage } from '@simai/contracts';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// 연습 빌드 mock 상태 저장·복원(S15P31A101-806 후속): 새로고침(새 mock 묶음) 뒤에도 작업·세팅·구역·이벤트·시연 진행이 이어지고,
// 판·날짜·모양이 맞지 않는 저장분은 버리고 처음 상태로 시작한다.

const BASE = 'http://api.test';
// 03:00 UTC = 서울 12:00. 어느 시간대에서 돌려도 날짜가 2026-11-20 이다.
const T0 = Date.UTC(2026, 10, 20, 3, 0);
const TODAY = '2026-11-20';
const SESSION_KEY = MOCK_STATE_PREFIX + MOCK_SESSION_KEY;

function memoryStorage(): MockStateStorage & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    get length() {
      return map.size;
    },
    key: (i) => [...map.keys()][i] ?? null,
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

const broker = { publish: () => {} } as unknown as StompBroker;

/** handlers.ts·startBackend 와 같은 연결로 mock 묶음 하나(새로고침 한 번 = 새 묶음)를 만든다. */
function buildMocks(now: () => number) {
  const staticObjects = createStaticObjectsMock();
  const setup = createSetupMock({ now, worldAnchorAccepted: true });
  const demoReplay = createDemoReplayStore();
  const work = createWorkMock({
    now,
    setupSessions: () => setup.sessions.values(),
    eventCounts: demoReplay.countsOf,
  });
  const tbm = createTbmMock({ deferDaysToHistory: true, dailyWorks: work.workItems });
  const zones = createZonesMock({
    now,
    staticObjects: staticObjects.store,
    onCommit: setup.completeAfterZonesCommit,
  });
  setup.bindLiveZones(() => zones.currentZones());
  const events = createEventsMock({ broker, now });
  const peopleNames = createPeopleNamesMock(now);
  const parts = {
    zones: zones.persist,
    setup: setup.persist,
    work: work.persist,
    tbm: tbm.persist,
    events: events.persist,
    demoReplay: demoReplay.persist,
    peopleNames: peopleNames.persist,
  };
  const handlers = [
    ...work.handlers,
    ...tbm.handlers,
    ...zones.handlers,
    ...setup.handlers,
    ...events.handlers,
  ];
  return { setup, work, tbm, zones, events, demoReplay, peopleNames, parts, handlers };
}

const exportAll = (parts: ReturnType<typeof buildMocks>['parts']) =>
  Object.fromEntries(Object.entries(parts).map(([name, part]) => [name, part.exportState()]));

const raised = (eventId: string, at: number): RiskEventMessage => ({
  schema_version: '2.3.0',
  msg_type: 'risk_event',
  msg_id: `0192a6f0-7c1e-4b3a-9f2d-${at.toString(16).padStart(12, '0').slice(-12)}`,
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
    rule_id: 'R1',
    severity: 'CRITICAL',
    world_ids: ['w-0012'],
    zone_id: null,
    metrics: {},
    rationale: null,
    proposed_severity: null,
    raised_at: at,
    updated_at: at,
    held_by_tier0: true,
  },
});

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  server.resetHandlers();
  vi.useRealTimers();
});
afterAll(() => server.close());

const api = createHttp({ baseUrl: BASE, getToken: () => 'mock-token.admin' });

describe('mock 상태 저장·복원(mocks/mockSession)', () => {
  it('작업·세팅·구역·이벤트·다시 보기·사람 이름을 남기고 새 mock 묶음에 그대로 되살린다', async () => {
    let clock = T0;
    const now = () => clock;
    const storage = memoryStorage();
    const first = buildMocks(now);
    server.resetHandlers(...first.handlers);

    const work = await putDailyWorkUnit(api, TODAY);
    const session = await createSetupSession(api, {
      work_unit_id: Number(work.work_id),
      camera_heights_m: { cam_A: 2.7, cam_B: 2.7, cam_C: 2.7, cam_D: 2.7 },
    });
    const z04 = readZones(MOCK_ZONES)[0]!;
    await commitZone(api, {
      zone: { op: 'CREATE', body: bodyOf(z04, { ...formOf(z04), name: '새 개구부' }, null) },
      setup_session_id: session.setup_session_id,
    });
    first.events.record(raised('evt-1', clock));
    first.demoReplay.startRun(work.work_id, clock);
    first.demoReplay.recordEvent(work.work_id, raised('evt-1', clock));
    first.peopleNames.bindWatch('watch-03', 'w-0021', 'MANUAL');
    clock += 1_000;

    createMockSession({ storage, parts: first.parts, now }).save();
    const saved = JSON.parse(storage.map.get(SESSION_KEY)!) as Record<string, unknown>;
    expect(saved).toMatchObject({ version: MOCK_SESSION_VERSION, day: TODAY });

    // 새로고침: 새 mock 묶음은 처음 상태이고, 복원하면 앞 묶음과 같은 상태가 된다.
    const second = buildMocks(now);
    expect(second.work.workItems()).toEqual([]);
    expect(createMockSession({ storage, parts: second.parts, now }).restore()).toBe('restored');
    expect(exportAll(second.parts)).toEqual(exportAll(first.parts));

    server.resetHandlers(...second.handlers);
    expect((await fetchWorkUnit(api, work.work_id)).progress).toBe('SETUP');
    expect(second.setup.sessions.get(session.setup_session_id)?.status).toBe('COMPLETED');
    expect(second.zones.currentZones().map((z) => z.name)).toContain('새 개구부');
    expect(second.events.find('evt-1')?.lifecycle).toBe('RAISED');
    expect(second.demoReplay.countsOf(work.work_id)).toEqual({ CRITICAL: 1, HIGH: 0 });
    expect(second.peopleNames.bindings().some((b) => b.device_id === 'watch-03')).toBe(true);
    // 다음 번호도 이어 간다(새 날짜 작업이 같은 id 를 받지 않는다).
    const next = await putDailyWorkUnit(api, '2026-11-21');
    expect(next.work_id).not.toBe(work.work_id);
  });

  it('올린 서명 이미지(바이트)도 base64 로 남겼다가 되살린다', async () => {
    const storage = memoryStorage();
    const first = buildMocks(() => T0);
    server.resetHandlers(...first.handlers);
    const bytes = new Uint8Array([137, 80, 78, 71, 0, 255, 1, 2]);
    await fetch(`${BASE}/mock-signature-upload/key-1`, { method: 'PUT', body: bytes });
    createMockSession({ storage, parts: first.parts, now: () => T0 }).save();

    const second = buildMocks(() => T0);
    expect(createMockSession({ storage, parts: second.parts, now: () => T0 }).restore()).toBe(
      'restored',
    );
    server.resetHandlers(...second.handlers);
    const image = await fetch(`${BASE}/mock-signature-image/key-1`);
    expect(new Uint8Array(await image.arrayBuffer())).toEqual(bytes);
  });

  it('남긴 값이 없으면 none, 저장소가 없으면(운영·개발 서버) 저장도 복원도 하지 않는다', () => {
    const mocks = buildMocks(() => T0);
    const storage = memoryStorage();
    expect(createMockSession({ storage, parts: mocks.parts, now: () => T0 }).restore()).toBe(
      'none',
    );
    const off = createMockSession({ storage: null, parts: mocks.parts, now: () => T0 });
    off.save();
    off.scheduleSave();
    expect(off.restore()).toBe('none');
    expect(storage.map.size).toBe(0);
  });

  it.each([
    ['판이 다르면', (s: Record<string, unknown>) => ({ ...s, version: MOCK_SESSION_VERSION + 1 })],
    ['어제(서울) 저장분이면', (s: Record<string, unknown>) => ({ ...s, day: '2026-11-19' })],
    [
      '한 부분의 모양이 틀리면',
      (s: Record<string, unknown>) => ({
        ...s,
        parts: { ...(s.parts as Record<string, unknown>), work: { works: 'x' } },
      }),
    ],
    [
      '부분이 빠졌으면',
      (s: Record<string, unknown>) => {
        const rest = { ...(s.parts as Record<string, unknown>) };
        delete rest.events;
        return { ...s, parts: rest };
      },
    ],
  ])('%s 키를 지우고 아무것도 적용하지 않는다(처음 상태)', async (_label, corrupt) => {
    const storage = memoryStorage();
    const first = buildMocks(() => T0);
    server.resetHandlers(...first.handlers);
    await putDailyWorkUnit(api, TODAY);
    first.events.record(raised('evt-1', T0));
    createMockSession({ storage, parts: first.parts, now: () => T0 }).save();
    const saved = JSON.parse(storage.map.get(SESSION_KEY)!) as Record<string, unknown>;
    storage.setItem(SESSION_KEY, JSON.stringify(corrupt(saved)));

    const second = buildMocks(() => T0);
    const fresh = exportAll(second.parts);
    expect(createMockSession({ storage, parts: second.parts, now: () => T0 }).restore()).toBe(
      'discarded',
    );
    expect(storage.map.has(SESSION_KEY)).toBe(false);
    // 모양이 맞는 부분(구역·이벤트 등)도 적용하지 않아 부분끼리 어긋나지 않는다.
    expect(exportAll(second.parts)).toEqual(fresh);
  });

  it('JSON 이 깨졌으면 버리고 처음 상태로 시작한다', () => {
    const storage = memoryStorage();
    storage.setItem(SESSION_KEY, '{"version":1,"day":');
    const mocks = buildMocks(() => T0);
    expect(createMockSession({ storage, parts: mocks.parts, now: () => T0 }).restore()).toBe(
      'discarded',
    );
    expect(storage.map.has(SESSION_KEY)).toBe(false);
    expect(mocks.work.workItems()).toEqual([]);
  });

  it('하루가 지나면(서울 자정) 같은 저장분을 버린다', () => {
    const storage = memoryStorage();
    const mocks = buildMocks(() => T0);
    createMockSession({ storage, parts: mocks.parts, now: () => T0 }).save();
    // 서울 2026-11-21 00:00 = UTC 11-20 15:00
    const nextDay = Date.UTC(2026, 10, 20, 15, 0);
    expect(seoulDateKey(nextDay)).toBe('2026-11-21');
    expect(
      createMockSession({
        storage,
        parts: buildMocks(() => nextDay).parts,
        now: () => nextDay,
      }).restore(),
    ).toBe('discarded');
  });

  it('scheduleSave 는 짧게 모아 한 번 저장하고, 「연습 처음부터 다시」는 저장 키도 지운다', () => {
    vi.useFakeTimers();
    const storage = memoryStorage();
    const writes = vi.spyOn(storage, 'setItem');
    const mocks = buildMocks(() => T0);
    const session = createMockSession({ storage, parts: mocks.parts, now: () => T0 });
    session.scheduleSave();
    session.scheduleSave();
    expect(writes).not.toHaveBeenCalled();
    vi.advanceTimersByTime(300);
    expect(writes).toHaveBeenCalledTimes(1);
    storage.setItem(`${MOCK_STATE_PREFIX}gpu`, '{"on":false}');
    storage.setItem('other-app', 'keep');
    clearMockState(storage);
    expect([...storage.map.keys()]).toEqual(['other-app']);
    // 지운 뒤 새로고침 직전(pagehide)의 저장이 지운 상태를 되살리지 않는다.
    session.save();
    expect([...storage.map.keys()]).toEqual(['other-app']);
  });
});

describe('시연 경보 시나리오 이어 가기(createDemoAlarmScheduler.resume)', () => {
  const START = T0;

  /** 처음 실행을 at 까지 돌려 보낸 전이와 저장할 상태를 얻는다. */
  function runUntil(at: number) {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    const sent: RiskEventMessage[] = [];
    const scheduler = createDemoAlarmScheduler({ publish: (m) => sent.push(m) });
    scheduler.start(START);
    vi.advanceTimersByTime(at);
    const state = JSON.parse(JSON.stringify(scheduler.exportState())) as unknown;
    scheduler.stop();
    return { sent, state: parseDemoAlarmSchedulerState(state)! };
  }

  const lookupOf = (sent: readonly RiskEventMessage[]) => (eventId: string) =>
    sent.filter((m) => m.payload.event_id === eventId).at(-1)?.payload;

  it('20초에 새로고침하면 지난 경보는 다시 보내지 않고 남은 경보(35초 낙상)와 해소만 제때 보낸다', () => {
    const { sent, state } = runUntil(20_000);
    expect(sent.map((m) => `${m.payload.rule_id}:${m.payload.lifecycle}`)).toEqual([
      'R9:RAISED',
      'R5:RAISED',
      'R1:RAISED',
      'R5:RESOLVED',
    ]);
    expect(state.run).toEqual({ startedAt: START, base: 0 });

    // 새로고침: 새 스케줄러가 같은 시작 시각으로 이어 간다.
    vi.setSystemTime(START + 20_000);
    const after: RiskEventMessage[] = [];
    const resumed = createDemoAlarmScheduler({ publish: (m) => after.push(m) });
    resumed.resume(state, lookupOf(sent));
    expect(after).toEqual([]);
    vi.advanceTimersByTime(DEMO_TIMES.helmetResolved - 20_000);
    expect(after.map((m) => `${m.payload.rule_id}:${m.payload.lifecycle}`)).toEqual([
      'R9:RESOLVED',
    ]);
    vi.advanceTimersByTime(DEMO_TIMES.fall - DEMO_TIMES.helmetResolved - 1);
    expect(after.some((m) => m.payload.rule_id === 'R4')).toBe(false);
    vi.advanceTimersByTime(1);
    expect(after.at(-1)?.payload).toMatchObject({
      rule_id: 'R4',
      lifecycle: 'RAISED',
      raised_at: START + DEMO_TIMES.fall,
    });
    vi.advanceTimersByTime(30_000);
    expect(after.map((m) => `${m.payload.rule_id}:${m.payload.lifecycle}`)).toEqual([
      'R9:RESOLVED',
      'R4:RAISED',
      'R4:RESOLVED',
      'R1:RESOLVED',
    ]);
    // 이어 보낸 해소는 처음 실행의 event_id·raised_at 을 그대로 쓴다.
    const firstEntry = sent.find((m) => m.payload.rule_id === 'R1')!;
    expect(after.find((m) => m.payload.rule_id === 'R1')?.payload).toMatchObject({
      event_id: firstEntry.payload.event_id,
      raised_at: firstEntry.payload.raised_at,
      resolved_at: START + DEMO_TIMES.entryResolved,
    });
    // 앞뒤를 합치면 처음부터 끝까지 돌린 것과 같은 4건 8전이다.
    const all = [...sent, ...after];
    expect(all.filter((m) => m.payload.lifecycle === 'RAISED')).toHaveLength(
      DEMO_ALARM_STEPS.length,
    );
    expect(all.filter((m) => m.payload.lifecycle === 'RESOLVED')).toHaveLength(
      DEMO_ALARM_STEPS.length,
    );
  });

  it('저장 전에 새로고침해 저장분에 없는 지난 경보는 원래 시각으로 한 번만 채운다', () => {
    const { sent, state } = runUntil(18_000);
    // R1(17초) 경보를 저장하기 전에 새로고침했다고 친다.
    const kept = sent.filter((m) => m.payload.rule_id !== 'R1');
    vi.setSystemTime(START + 18_000);
    const after: RiskEventMessage[] = [];
    createDemoAlarmScheduler({ publish: (m) => after.push(m) }).resume(state, lookupOf(kept));
    expect(after.map((m) => `${m.payload.rule_id}:${m.payload.lifecycle}`)).toEqual(['R1:RAISED']);
    expect(after[0]?.payload.raised_at).toBe(START + DEMO_TIMES.entry);
  });

  it('작업이 끝났으면(run 없음) 아무것도 다시 보내지 않고 다음 실행은 새 번호를 쓴다', () => {
    const { sent, state } = runUntil(60_000);
    vi.setSystemTime(START + 60_000);
    const after: RiskEventMessage[] = [];
    const resumed = createDemoAlarmScheduler({ publish: (m) => after.push(m) });
    resumed.resume({ ...state, run: null }, lookupOf(sent));
    vi.advanceTimersByTime(120_000);
    expect(after).toEqual([]);
    resumed.start();
    vi.advanceTimersByTime(DEMO_TIMES.helmet);
    const ids = new Set(sent.map((m) => m.payload.event_id));
    expect(after).toHaveLength(1);
    expect(ids.has(after[0]!.payload.event_id)).toBe(false);
  });

  it('모양이 틀린 시나리오 상태는 받지 않는다', () => {
    expect(parseDemoAlarmSchedulerState({ sequence: 4, run: null })).toEqual({
      sequence: 4,
      run: null,
    });
    expect(parseDemoAlarmSchedulerState({ sequence: '4', run: null })).toBeUndefined();
    expect(parseDemoAlarmSchedulerState({ sequence: 4, run: { startedAt: 'x', base: 0 } })).toBe(
      undefined,
    );
    expect(parseDemoAlarmSchedulerState(null)).toBeUndefined();
  });
});
