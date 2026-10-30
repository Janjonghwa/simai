import { env } from '@/lib/env';

/**
 * 서버가 아직 없는 API 를 msw 로 REST·STOMP 흉내 낸다(개발 서버 전용, main.tsx 가 DEV 에서만 불러서 빌드에는 들어가지 않는다).
 * VITE_API_REAL 에 적은 경로(예: /api/auth)는 흉내 내지 않고 개발 서버 프록시로 실서버에 보낸다(vite.config.ts).
 */
export async function startMockBackend() {
  const [
    { network },
    { handlers, staticObjectsMock },
    { readMockUser },
    { withoutRealPaths },
    { createStompBroker },
    { startRealtimeMock },
    { createEventsMock },
    { createClipsMock },
    { createZonesMock },
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
  ]);
  // 토큰 모양만 본다: mock 로그인 토큰이나 sub·role 이 있는 JWT(혼합 모드, 서명은 보지 않는다).
  // 로그아웃 뒤 토큰 없이 붙지 않는지, 만료된 실서버 토큰이 서버처럼 ERROR 로 거절되는지 볼 수 있다.
  // 역할도 넘겨 관리자 전용 토픽(/topic/admin/events)을 다른 계정이 구독하면 서버처럼 ERROR 로 끊는다.
  const broker = createStompBroker({
    url: env.VITE_STOMP_URL,
    authorize: (authorization) => {
      const user = readMockUser(authorization);
      return user && (user.expiresAt === undefined || user.expiresAt > Date.now()) ? user : false;
    },
  });
  const events = createEventsMock({ broker });
  const clips = createClipsMock({ findEvent: events.find });
  // health 의 구성 묶음 상태(발행·ACK)는 구역 mock 이 정한다. 발행하는 묶음에는 지금 작업면을 싣는다.
  const zones = createZonesMock({ staticObjects: staticObjectsMock.current });
  // GET /api/health 가 STOMP 흉내와 같은 상태를 주도록 실시간 mock 을 먼저 만든다.
  const { handlers: realtimeHandlers, ...realtimeMock } = startRealtimeMock(
    broker,
    events,
    Date.now,
    zones.config,
  );
  network.configure({
    // 실서버 경로의 mock 은 빼서 그 요청이 그대로 나가게 한다(처리되지 않은 요청은 bypass).
    handlers: [
      ...withoutRealPaths(
        [
          ...handlers,
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
  // simaiMock.outage('tier0' | 'laptop' | 'remote' | null)(장애 띠), simaiMock.sceneMismatch(true),
  // simaiMock.rejectNextBundle()(다음 구성 묶음을 Tier 0 가 거부해 STALE),
  // simaiMock.failNextPublish('CALIBRATION_MISSING' | 'CURRENT_CALIBRATION_MISSING' | 'RULES_MISSING' |
  // 'SITE_NOT_SINGLE' | 'PUBLIC_URL_NOT_SET')(다음 발행 요청을 서버 오류 409·503 으로 거절, 기본 CALIBRATION_MISSING)
  // simaiMock.expireStomp()(STOMP 토큰 만료: 다음 연결을 ERROR 로 거절 → 갱신 뒤 다시 붙기),
  // simaiMock.actionMismatch(eventId?, cleared?)(관리자에게 조치 불일치를 보낸다)
  Object.assign(globalThis, {
    simaiMock: {
      ...realtimeMock,
      expireAuth: events.expireAuth,
      rejectNextBundle: zones.rejectNextBundle,
      failNextPublish: zones.failNextPublish,
    },
  });
}
