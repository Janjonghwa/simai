import { env } from '@/lib/env';
import { createHttp } from '@/lib/http';
import { useSessionStore } from '@/store/session.store';

// 앱의 REST 요청은 이 인스턴스를 거친다. 토큰이 거부되면(401) 세션을 끝내고, 라우트 가드가 로그인 화면으로 보낸다.
export const api = createHttp({
  baseUrl: env.VITE_API_BASE_URL,
  getToken: () => useSessionStore.getState().session?.accessToken,
  onUnauthorized: () => useSessionStore.getState().signOut(),
});
