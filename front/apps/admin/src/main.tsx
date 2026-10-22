import '@/index.css';
// 저장된 다크·화이트 모드를 화면을 그리기 전에 적용한다.
import '@/features/layout/theme.store';

import { bindEventsCache } from '@/features/events/eventsCache';
import { installAudioUnlock } from '@/lib/alarmSound';
import { bindSession } from '@/lib/bindSession';
// 환경변수가 틀리면 화면을 그리기 전에 멈춘다.
import { env } from '@/lib/env';
import { queryClient } from '@/lib/queryClient';
import { realtime } from '@/lib/realtime';
import { routeTree } from '@/routeTree.gen';
import { useSessionStore } from '@/store/session.store';
import { QueryClientProvider } from '@tanstack/react-query';
import { createRouter, RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

const router = createRouter({
  routeTree,
  context: { sessionStore: useSessionStore, queryClient },
});

// 로그인·로그아웃·토큰 거부로 세션이 바뀌면 실시간 연결을 열고 닫고, 라우트 가드를 다시 돌리고,
// 사용자가 끝나면 이전 사용자의 데이터를 지운다.
bindSession({ sessionStore: useSessionStore, realtime, router, queryClient });
// /topic/events 로 받은 이벤트를 경보 목록 캐시에 반영한다.
bindEventsCache(realtime, queryClient);
// 브라우저는 사용자가 누르기 전에는 소리를 막는다. 첫 클릭·키 입력(로그인 포함)에서 경보음을 깨운다.
installAudioUnlock();

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
  interface StaticDataRouteOption {
    /** 상단 바 아래까지 화면을 채우고 상단 바를 그 위에 띄운다(실시간 관제). */
    fullBleed?: boolean;
    /** 오른쪽에 이벤트 패널이 있다(실시간 관제). 경보 배너가 그 자리를 비키고 「상세」를 이 화면에서 연다. */
    eventPanel?: boolean;
  }
}

// 서버(S15P31A101-61)가 생기기 전까지 개발 서버는 msw 로 REST·STOMP 를 흉내 낸다. DEV 조건이라 빌드에는 들어가지 않는다.
if (import.meta.env.DEV && env.VITE_API_MOCK) {
  const [
    { network },
    { handlers },
    { createStompBroker },
    { startRealtimeMock },
    { createEventsMock },
  ] = await Promise.all([
    import('virtual:msw'),
    import('@/mocks/handlers'),
    import('@/mocks/stompBroker'),
    import('@/mocks/realtime'),
    import('@/mocks/events'),
  ]);
  // mock 로그인이 주는 토큰만 받는다(로그아웃 뒤 이전 토큰으로 붙지 않는지 볼 수 있다).
  const broker = createStompBroker({
    url: env.VITE_STOMP_URL,
    authorize: (authorization) => authorization?.startsWith('Bearer mock-token.') ?? false,
  });
  const events = createEventsMock({ broker });
  network.configure({
    handlers: [...handlers, ...events.handlers, broker.handler],
    onUnhandledFrame: 'bypass',
    // msw 는 가로챈 요청·프레임마다 콘솔에 남긴다. world 가 초당 10번 와서 콘솔이 넘치므로 끈다(REST 기록도 같이 꺼진다).
    context: { quiet: true },
  });
  await network.enable();
  // 개발 도구 콘솔에서 끊김을 흉내 낸다: simaiMock.pauseHealth(5000), simaiMock.drop()
  Object.assign(globalThis, { simaiMock: startRealtimeMock(broker, events) });
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
