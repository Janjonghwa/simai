import '@/index.css';
// 저장된 다크·화이트 모드를 화면을 그리기 전에 적용한다.
import '@/features/layout/theme.store';

import { bindEventsCache } from '@/features/events/eventsCache';
import { installAudioUnlock } from '@/lib/alarmSound';
import { authSession } from '@/lib/api';
import { bindSession } from '@/lib/bindSession';
// 환경변수가 틀리면 화면을 그리기 전에 멈춘다.
import { env } from '@/lib/env';
import { queryClient } from '@/lib/queryClient';
import { realtime } from '@/lib/realtime';
import { routeTree } from '@/routeTree.gen';
import { useSessionStore } from '@/store/session.store';
import { clearZoneDrafts } from '@/store/zoneDrafts.store';
import { QueryClientProvider } from '@tanstack/react-query';
import { createRouter, RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

/** 앱을 켤 때 세션 복구를 기다리는 시간(ms). 넘으면 로그인 화면부터 그린다. */
const RESTORE_WAIT_MS = 3000;

const router = createRouter({
  routeTree,
  context: { sessionStore: useSessionStore, queryClient },
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
  interface StaticDataRouteOption {
    /** 상단 바 아래까지 화면을 채우고 상단 바를 그 위에 띄운다(실시간 관제). */
    fullBleed?: boolean;
    /** 오른쪽에 이벤트 패널이 있다(실시간 관제). 경보 배너가 그 자리를 비키고 「상세」를 이 화면에서 연다. */
    eventPanel?: boolean;
    /** 구역 편집 화면(/zones). 같은 3D World 위에 편집 오버레이·가장자리 음영·미저장 Ghost 를 켠다. */
    zoneEditor?: boolean;
  }
}

// 서버가 아직 없는 API 는 개발 서버에서 msw 로 REST·STOMP 를 흉내 낸다. DEV 조건이라 빌드에는 들어가지 않는다.
// VITE_API_REAL 에 적은 경로(예: /api/auth)는 흉내 내지 않고 개발 서버 프록시로 실서버에 보낸다(vite.config.ts).
if (import.meta.env.DEV && env.VITE_API_MOCK) {
  const [
    { network },
    { handlers },
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
  // 토큰 모양만 본다: mock 로그인 토큰이나 sub·role 이 있는 JWT(혼합 모드, 서명·만료는 보지 않는다).
  // 로그아웃 뒤 토큰 없이 붙지 않는지 볼 수 있다.
  const broker = createStompBroker({
    url: env.VITE_STOMP_URL,
    authorize: (authorization) => readMockUser(authorization) !== undefined,
  });
  const events = createEventsMock({ broker });
  const clips = createClipsMock({ findEvent: events.find });
  // health 의 구성 묶음 상태(발행·ACK)는 구역 mock 이 정한다.
  const zones = createZonesMock();
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
  // simaiMock.rejectNextBundle()(다음 구성 묶음을 Tier 0 가 거부해 STALE)
  Object.assign(globalThis, {
    simaiMock: {
      ...realtimeMock,
      expireAuth: events.expireAuth,
      rejectNextBundle: zones.rejectNextBundle,
    },
  });
}

// 새로고침해도 로그인이 이어지게 갱신 쿠키로 세션을 되살린다. 서버가 늦으면 정한 시간만 기다리고 그린다
// (늦게 끝나면 그때 로그인되고 로그인 라우트 가드가 가려던 화면으로 보낸다. 그 사이 로그인을 시도했으면 버린다).
// 기다리기를 그만둬도 요청은 끊지 않는다(authSession.restore).
await Promise.race([
  authSession.restore(),
  new Promise((resolve) => setTimeout(resolve, RESTORE_WAIT_MS)),
]);

// 로그인·로그아웃·토큰 거부로 세션이 바뀌면 실시간 연결을 열고 닫고, 라우트 가드를 다시 돌리고,
// 사용자가 끝나면 이전 사용자의 데이터를 지운다. 되살린 세션이 있으면 바로 연결한다.
// 실시간 연결은 만료가 가까우면 먼저 갱신한 토큰으로 붙는다(STOMP 는 401 로 갱신할 길이 없다).
bindSession({
  sessionStore: useSessionStore,
  realtime,
  router,
  queryClient,
  getToken: authSession.freshToken,
  onUserChange: clearZoneDrafts,
});
// /topic/events 로 받은 이벤트를 경보 목록 캐시에 반영한다.
bindEventsCache(realtime, queryClient);
// 브라우저는 사용자가 누르기 전에는 소리를 막는다. 첫 클릭·키 입력(로그인 포함)에서 경보음을 깨운다.
installAudioUnlock();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
