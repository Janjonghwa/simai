import type { Realtime } from '@/lib/stomp';
import type { Session } from '@/store/session.store';
import type { StoreApi } from 'zustand';

export interface BindSessionDeps {
  sessionStore: StoreApi<{ session?: Session }>;
  realtime: Pick<Realtime, 'start' | 'stop'>;
  router: { invalidate(): Promise<unknown>; clearCache(): void };
  queryClient: { clear(): void };
}

/**
 * 세션이 바뀔 때 할 일을 한 곳에서 한다. 로그인·로그아웃·토큰 거부(401) 모두 같은 길을 지난다.
 * - 로그인·토큰 교체: 그 토큰으로 실시간 연결을 연다.
 * - 로그아웃·다른 계정: 연결을 끊고 실시간 저장소를 비운 뒤, 라우트 가드를 다시 돌리고 서버 데이터 캐시를 지운다.
 *   캐시는 가드가 로그인 화면으로 보낸 뒤 지운다. 화면이 떠 있는 채 지우면 토큰 없이 다시 요청한다.
 * 해제 함수를 돌려준다.
 */
export function bindSession({ sessionStore, realtime, router, queryClient }: BindSessionDeps) {
  const initial = sessionStore.getState().session;
  if (initial) realtime.start({ token: initial.accessToken, userId: initial.user.id });

  return sessionStore.subscribe((state, prev) => {
    const before = prev.session;
    const after = state.session;
    if (before === after) return;
    const userEnded = before !== undefined && after?.user.id !== before.user.id;

    if (userEnded) void realtime.stop();
    if (after && after.accessToken !== before?.accessToken) {
      realtime.start({ token: after.accessToken, userId: after.user.id });
    }
    if (userEnded) router.clearCache();
    // 가드가 실패해도(로더 오류 등) 이전 사용자의 캐시는 지운다.
    void router
      .invalidate()
      .catch(() => undefined)
      .then(() => {
        if (userEnded) queryClient.clear();
      });
  });
}
