import '@/index.css';
// 저장된 다크·화이트 모드를 화면을 그리기 전에 적용한다.
import '@/features/layout/theme.store';

// 환경변수가 틀리면 화면을 그리기 전에 멈춘다.
import { env } from '@/lib/env';
import { queryClient } from '@/lib/queryClient';
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

// 로그인·로그아웃·토큰 거부로 세션이 바뀌면 라우트 가드(beforeLoad)를 다시 돌린다.
// 세션이 끝나거나 다른 계정으로 바뀌면 이전 사용자의 서버 데이터 캐시를 먼저 지운다.
useSessionStore.subscribe((state, prev) => {
  if (prev.session && state.session?.user.id !== prev.session.user.id) {
    queryClient.clear();
    router.clearCache();
  }
  void router.invalidate();
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
  interface StaticDataRouteOption {
    /** 상단 바 아래까지 화면을 채우고 상단 바를 그 위에 띄운다(실시간 관제). */
    fullBleed?: boolean;
  }
}

// 서버(S15P31A101-61)가 생기기 전까지 개발 서버는 msw 로 REST 를 흉내 낸다. DEV 조건이라 빌드에는 들어가지 않는다.
if (import.meta.env.DEV && env.VITE_API_MOCK) {
  const [{ network }, { handlers }] = await Promise.all([
    import('virtual:msw'),
    import('@/mocks/handlers'),
  ]);
  network.configure({ handlers, onUnhandledFrame: 'bypass' });
  await network.enable();
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
