import { create } from 'zustand';

/** 계정 역할(명세 14장): 관리자, 반장, 열람. */
export const ROLES = ['ADMIN', 'FOREMAN', 'VIEWER'] as const;
export type Role = (typeof ROLES)[number];

export interface SessionUser {
  id: string;
  role: Role;
}

export interface Session {
  accessToken: string;
  user: SessionUser;
}

interface SessionState {
  session?: Session;
  signIn: (session: Session) => void;
  signOut: () => void;
  /**
   * 서버가 거부한 토큰이 지금 세션의 토큰일 때만 세션을 끝낸다.
   * 응답을 기다리는 사이 다시 로그인했으면 새 세션은 그대로 둔다(REST 401·STOMP 인증 실패가 같이 쓴다).
   */
  signOutIfCurrent: (token: string) => void;
}

// 토큰은 메모리에만 둔다. 새로고침하면 다시 로그인한다(갱신 방식은 서버와 정한 뒤 붙인다).
export const useSessionStore = create<SessionState>()((set, get) => ({
  session: undefined,
  signIn: (session) => set({ session }),
  signOut: () => set({ session: undefined }),
  signOutIfCurrent: (token) => {
    if (get().session?.accessToken === token) set({ session: undefined });
  },
}));
