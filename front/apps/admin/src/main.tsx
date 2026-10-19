import '@/index.css';
// 환경변수가 틀리면 화면을 그리기 전에 멈춘다.
import '@/lib/env';
// 저장된 다크·화이트 모드를 화면을 그리기 전에 적용한다.
import '@/features/layout/theme.store';

import { queryClient } from '@/lib/queryClient';
import { routeTree } from '@/routeTree.gen';
import { QueryClientProvider } from '@tanstack/react-query';
import { createRouter, RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

const router = createRouter({ routeTree });

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
  interface StaticDataRouteOption {
    /** 상단 바 아래까지 화면을 채우고 상단 바를 그 위에 띄운다(실시간 관제). */
    fullBleed?: boolean;
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
