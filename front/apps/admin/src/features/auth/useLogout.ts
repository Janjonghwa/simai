import { useSessionStore } from '@/store/session.store';

// 세션만 끝낸다. 캐시 비우기와 로그인 화면 이동은 main.tsx 의 세션 구독이 한다(401 로 끝날 때와 같은 길).
// 서버 로그아웃(갱신 토큰 폐기)이 생기면 여기서 부른다.
export function useLogout() {
  return useSessionStore((s) => s.signOut);
}
