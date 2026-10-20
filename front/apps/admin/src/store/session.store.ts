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
}

// 토큰은 메모리에만 둔다. 새로고침하면 다시 로그인한다(갱신 방식은 서버와 정한 뒤 붙인다).
export const useSessionStore = create<SessionState>()((set) => ({
  session: undefined,
  signIn: (session) => set({ session }),
  signOut: () => set({ session: undefined }),
}));
