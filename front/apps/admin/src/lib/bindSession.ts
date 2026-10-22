import type { Realtime, RealtimeSession } from '@/lib/stomp';
import type { Session } from '@/store/session.store';
import type { StoreApi } from 'zustand';

export interface BindSessionDeps {
  sessionStore: StoreApi<{ session?: Session }>;
  realtime: Pick<Realtime, 'start' | 'stop'>;
  router: { invalidate(): Promise<unknown>; clearCache(): void };
  queryClient: { clear(): void };
  /** 실시간 연결이 연결·재연결마다 읽는 토큰. 기본은 세션의 토큰(앱은 만료 전에 갱신하는 authSession.freshToken). */
  getToken?: RealtimeSession['getToken'];
}

/**
 * 세션이 바뀔 때 할 일을 한 곳에서 한다. 로그인·로그아웃·토큰 거부(401) 모두 같은 길을 지난다.
 * - 로그인·다른 계정: 그 사용자로 실시간 연결을 연다.
 * - 같은 사용자의 토큰 갱신: 연결을 다시 열지 않는다(world·이벤트·3D 입력이 비지 않게). 다음 재연결이 새 토큰을 읽는다.
 * - 로그아웃·다른 계정: 연결을 끊고 실시간 저장소를 비운 뒤, 라우트 가드를 다시 돌리고 서버 데이터 캐시를 지운다.
 *   캐시는 가드가 로그인 화면으로 보낸 뒤 지운다. 화면이 떠 있는 채 지우면 토큰 없이 다시 요청한다.
 * 해제 함수를 돌려준다.
 */
export function bindSession({
  sessionStore,
  realtime,
  router,
  queryClient,
  getToken = () => sessionStore.getState().session?.accessToken,
}: BindSessionDeps) {
  const initial = sessionStore.getState().session;
  if (initial) realtime.start({ userId: initial.user.id, getToken });

  return sessionStore.subscribe((state, prev) => {
    const before = prev.session;
    const after = state.session;
    if (before === after) return;
    // 같은 사용자의 토큰 갱신이면 할 일이 없다. 가드도 그대로 통과한다.
    if (before && after && before.user.id === after.user.id) return;
    const userEnded = before !== undefined;

    if (userEnded) void realtime.stop();
    if (after) realtime.start({ userId: after.user.id, getToken });
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
