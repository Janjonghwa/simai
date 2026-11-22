import { env } from '@/lib/env';
import { EVENTS_TOPIC } from '@/lib/stomp';
import {
  createDemoAlarmScheduler,
  parseDemoAlarmSchedulerState,
  workRunActionOf,
} from '@/mocks/demoAlarmScenario';

/**
 * 서버가 아직 없는 API 를 msw 로 REST·STOMP 흉내 낸다(개발 서버 전용, main.tsx 가 DEV 에서만 불러서 빌드에는 들어가지 않는다).
 * VITE_API_REAL 에 적은 경로(예: /api/auth)는 흉내 내지 않고 개발 서버 프록시로 실서버에 보낸다(vite.config.ts).
 */
export async function startMockBackend() {
  const [
    { network },
    { handlers, staticObjectsMock, tbmMock, workMock, setupMock, demoReplayStore },
    { readMockUser },
    { withoutRealPaths },
    { createStompBroker },
    { startRealtimeMock },
    { createEventsMock },
    { createClipsMock },
    { createZonesMock },
    { createPeopleNamesMock },
    { seededEventsAt },
    { mockStateStorage },
    { createMockSession, isRecord },
    { msgIdSerial, resumeMsgIdSerial },
  ] = await Promise.all([
    import('virtual:msw'),
    import('@/mocks/handlers'),
    import('@/mocks/mockUser'),
    import('@/mocks/realPaths'),
    import('@/mocks/stompBroker'),
    import('@/mocks/realtime'),
    import('@/mocks/events'),
    import('@/mocks/clips'),
    import('@/mocks/zones'),
    import('@/mocks/peopleNames'),
    import('@/mocks/realtimeScenario'),
    import('@/mocks/mockPersist'),
    import('@/mocks/mockSession'),
    import('@/mocks/scenario/shared'),
  ]);
  const isMockBuild = import.meta.env.VITE_MOCK_BUILD === 'true';
  // 토큰 모양만 본다: mock 로그인 토큰이나 sub·role 이 있는 JWT(혼합 모드, 서명은 보지 않는다).
  // 로그아웃 뒤 토큰 없이 붙지 않는지, 만료된 실서버 토큰이 서버처럼 ERROR 로 거절되는지 볼 수 있다.
  const broker = createStompBroker({
    url: env.VITE_STOMP_URL,
    authorize: (authorization) => {
      const user = readMockUser(authorization);
      return user !== undefined && (user.expiresAt === undefined || user.expiresAt > Date.now());
    },
  });
  // 목업 빌드는 작업 시작 시연 시나리오가 경보를 만들므로 빈 목록으로 시작하고, 개발 서버는 진행 중·지난 관제 시드를 싣는다.
  const events = createEventsMock({
    broker,
    summaryCompleteDelayMs: 1_500,
    seed: isMockBuild ? [] : seededEventsAt(Date.now()),
  });
  let demoStartedAt: number | undefined;
  let demoWorkId: string | undefined;
  // 연습 빌드는 mock 상태를 localStorage 에 남겨 새로고침·새 탭·주소 직접 입력 뒤에도 이어 간다(S15P31A101-806, mocks/mockSession).
  // 개발 서버·시험은 저장소가 null 이라 지금처럼 메모리만 쓴다.
  const storage = isMockBuild ? mockStateStorage() : null;
  let saveSoon = () => {};
  let saveNow = () => {};
  const scheduler = createDemoAlarmScheduler({
    publish: (message) => {
      const opening = zones.currentZones().find((zone) => zone.name === '단상 개구부');
      if (message.payload.zone_id && opening) message.payload.zone_id = opening.zone_id;
      // 시연 경보 전이를 세션 기록 저장소에 남겨 오늘 다시 보기(/api/replay)와 사건 수 집계가 같은 기록을 쓰게 한다.
      if (demoWorkId) demoReplayStore.recordEvent(demoWorkId, message);
      broker.publish(EVENTS_TOPIC, events.record(message));
      saveSoon();
    },
  });
  if (isMockBuild) {
    network.events.on('response:mocked', ({ request, response }) => {
      const action = workRunActionOf(request, response);
      if (action === 'start') {
        const workId = new URL(request.url).pathname.split('/').at(-2);
        const run = workId
          ? workMock
              .workItems()
              .find((work) => work.work_id === workId)
              ?.runs?.at(-1)
          : undefined;
        const startedAt = run?.started_at ?? Date.now();
        demoStartedAt = startedAt;
        demoWorkId = workId;
        if (workId) demoReplayStore.startRun(workId, startedAt);
        scheduler.start(startedAt);
        saveNow();
      } else if (action === 'end') {
        scheduler.stop();
        const workId = new URL(request.url).pathname.split('/').at(-2);
        if (workId) {
          const run = workMock
            .workItems()
            .find((work) => work.work_id === workId)
            ?.runs?.at(-1);
          demoReplayStore.endRun(workId, run?.ended_at ?? Date.now());
        }
        demoWorkId = undefined;
        saveNow();
      } else {
        // GET 도 상태를 앞으로 보낸다(세팅 단계·초안 진행은 조회할 때 시각으로 정한다). 짧게 모아 한 번 저장한다.
        saveSoon();
      }
    });
  }
  const clips = createClipsMock({ findEvent: events.find, demoReady: isMockBuild });
  // health 의 구성 묶음 상태(발행·ACK)는 구역 mock 이 정한다. 발행하는 묶음에는 지금 작업면을 싣는다. 구역을 저장하면 세팅 mock 의
  // 세션이 COMPLETED·zones_committed_at 이 된다. 작업 관리의 '세팅 완료'(SETUP_DONE)는 세팅 화면의 「세팅 완료」(POST .../complete, -615)가 정한다.
  const zones = createZonesMock({
    staticObjects: staticObjectsMock.store,
    mockBuild: isMockBuild,
    onCommit: setupMock.completeAfterZonesCommit,
  });
  tbmMock.bindLayoutZones(zones.layoutZones);
  // 세팅 구역 단계의 처음 목록 채우기(-629)는 지금 관제 구역을 읽는다
  setupMock.bindLiveZones(() => zones.currentZones());
  const peopleNames = createPeopleNamesMock(Date.now);
  // GET /api/health 가 STOMP 흉내와 같은 상태를 주도록 실시간 mock 을 먼저 만든다.
  const { handlers: realtimeHandlers, ...realtimeMock } = startRealtimeMock(
    broker,
    Date.now,
    zones.config,
    peopleNames,
    // 목업 빌드는 작업 시작 전에도 시연 인원(추창만 1명, elapsed=0)만 보인다.
    isMockBuild ? () => (demoStartedAt === undefined ? 0 : Date.now() - demoStartedAt) : undefined,
  );
  if (storage) {
    // 시연 진행(작업 시작 시각·작업·경보 번호)과 msg_id 번호. 시각이 같아야 3D 사람 움직임(elapsed)과 남은 경보가 이어진다.
    const demoPart = {
      exportState: () => ({
        startedAt: demoStartedAt ?? null,
        workId: demoWorkId ?? null,
        scheduler: scheduler.exportState(),
        msgIdSerial: msgIdSerial(),
      }),
      loadState: (raw: unknown) => {
        if (!isRecord(raw)) return undefined;
        const { startedAt, workId, msgIdSerial: serial } = raw;
        const state = parseDemoAlarmSchedulerState(raw.scheduler);
        if (
          !state ||
          (startedAt !== null && typeof startedAt !== 'number') ||
          (workId !== null && typeof workId !== 'string') ||
          typeof serial !== 'number'
        ) {
          return undefined;
        }
        return () => {
          demoStartedAt = startedAt ?? undefined;
          demoWorkId = workId ?? undefined;
          resumeMsgIdSerial(serial);
          // 작업이 아직 진행 중일 때만 시나리오를 잇는다(종료한 작업은 다시 돌리지 않는다).
          const running =
            workId !== null &&
            [...workMock.workItems(), ...tbmMock.works].some(
              (work) => work.work_id === workId && work.status === 'IN_PROGRESS',
            );
          scheduler.resume(running ? state : { ...state, run: null }, (eventId) =>
            workId === null ? undefined : demoReplayStore.latestEvent(workId, eventId)?.payload,
          );
        };
      },
    };
    const session = createMockSession({
      storage,
      // 적용 순서: 작업면·구역 → 세팅 → 작업·TBM → 이벤트·다시 보기 → 사람 이름 → 시연(작업 진행 여부와 다시 보기 기록을 읽는다).
      parts: {
        zones: zones.persist,
        setup: setupMock.persist,
        work: workMock.persist,
        tbm: tbmMock.persist,
        events: events.persist,
        demoReplay: demoReplayStore.persist,
        peopleNames: peopleNames.persist,
        demo: demoPart,
      },
    });
    session.restore();
    saveSoon = session.scheduleSave;
    saveNow = session.save;
    // 탭을 닫거나 새로고침하기 직전에 기다리던 저장을 마저 한다.
    globalThis.addEventListener?.('pagehide', session.save);
    globalThis.document?.addEventListener('visibilitychange', () => {
      if (globalThis.document.visibilityState === 'hidden') session.save();
    });
  }
  network.configure({
    // 실서버 경로의 mock 은 빼서 그 요청이 그대로 나가게 한다(처리되지 않은 요청은 bypass).
    handlers: [
      ...withoutRealPaths(
        [
          ...handlers,
          ...peopleNames.handlers,
          ...events.handlers,
          ...clips.handlers,
          ...zones.handlers,
          ...realtimeHandlers,
        ],
        env.VITE_API_REAL,
      ),
      broker.handler,
    ],
    onUnhandledFrame: 'bypass',
    // msw 는 가로챈 요청·프레임마다 콘솔에 남긴다. world 가 초당 10번 와서 콘솔이 넘치므로 끈다(REST 기록도 같이 꺼진다).
    context: { quiet: true },
  });
  await network.enable();
  // 개발 도구 콘솔에서 흉내 낸다: simaiMock.pauseHealth(5000), simaiMock.drop(),
  // simaiMock.expireAuth()(다음 ACK 를 토큰 만료로 거절해 갱신 뒤 다시 보내기를 본다),
  // simaiMock.outage('tier0' | 'laptop' | 'remote' | null)(장애 흉내), simaiMock.sceneMismatch(true),
  // simaiMock.rejectNextBundle()(다음 구성 묶음을 Tier 0 가 거부해 STALE),
  // simaiMock.failNextPublish('CALIBRATION_MISSING' | 'CURRENT_CALIBRATION_MISSING' | 'RULES_MISSING' |
  // 'SITE_NOT_SINGLE' | 'PUBLIC_URL_NOT_SET')(다음 발행 요청을 서버 오류 409·503 으로 거절, 기본 CALIBRATION_MISSING)
  // simaiMock.expireStomp()(STOMP 토큰 만료: 다음 연결을 ERROR 로 거절 → 갱신 뒤 다시 붙기)
  // simaiMock.markSetupDone(workId?)(오늘 날짜 작업의 세팅이 끝났다고 표시: 진행이 SETUP_DONE 이 되고 보고서 초안이 2.5초 뒤 채워진다)
  Object.assign(globalThis, {
    simaiMock: {
      ...realtimeMock,
      expireAuth: events.expireAuth,
      failNextSummary: events.failNextSummary,
      updateSummary: events.updateSummary,
      rejectNextBundle: zones.rejectNextBundle,
      failNextPublish: zones.failNextPublish,
      markSetupDone: workMock.markSetupDone,
      bindWatch: peopleNames.bindWatch,
    },
  });
}
